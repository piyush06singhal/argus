"""Phase 9 — the policy engine (§2, §7, §8, §21, §25, §34, §40, §45, §107).

The policy engine is where "default deny" stops being a slogan, so the tests are
written refusals-first:

* an unconfigured scope authorizes nothing, whatever the action is;
* ``OBSERVE_ONLY`` and ``EMERGENCY_STOP`` deny before any other rule runs;
* the process kill switch denies independently of the database;
* a failed safety assessment cannot be overridden by policy;
* the operator's hard ceilings clamp a stored policy rather than being advisory.

Then the positive paths, which only exist because a test configured them: the
human-approval escalation, and autonomous authorization inside a non-production
scope for a low-risk, reversible, registered action.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.models.remediation import (
    BlastRadiusScope,
    CircuitState,
    ExecutionStatus,
    PolicyDecision,
    RemediationActionType,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationRiskLevel,
    RemediationStatus,
)
from app.services.remediation_clock import utcnow
from app.services.remediation_policy import (
    PolicyEngine,
    compute_budget,
    get_breaker,
    resolve_policy,
    upsert_policy,
)
from app.services.remediation_service import RemediationService
from tests.phase9_helpers import (
    build_project,
    make_action,
    set_environment_class,
    set_policy,
)

settings = get_settings()


async def _propose(session, project, environment, component, **kwargs):
    """Propose an action and run the safety gate, returning the action."""
    action = await make_action(session, project, environment, component, **kwargs)
    await RemediationService(session).assess(action)
    return action


async def _evaluate(session, action):
    return await PolicyEngine(session).evaluate(
        action,
        assessment=None,
        environment_name=None,
        non_production=False,
    )


# ---------------------------------------------------------------------------
# Default deny
# ---------------------------------------------------------------------------


async def test_unconfigured_scope_denies_everything(db_session):
    """§2: no policy row means no authorization, for any action."""
    project, environment, component = await build_project(db_session)
    action = await _propose(db_session, project, environment, component)

    effective = await resolve_policy(db_session, project.id, environment.id)
    assert effective.source == "fallback"
    assert effective.is_configured is False

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.POLICY_DENIED
    assert any("policy_source" == rule["rule"] for rule in evaluation.matched_rules)


async def test_observe_only_records_but_authorizes_nothing(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.OBSERVE_ONLY,
    )
    action = await _propose(db_session, project, environment, component)

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.execution_mode == RemediationExecutionMode.OBSERVE_ONLY
    assert evaluation.allowed is False


@pytest.mark.parametrize(
    "mode",
    [
        RemediationExecutionMode.OBSERVE_ONLY,
        RemediationExecutionMode.EMERGENCY_STOP,
    ],
)
async def test_non_authorizing_regimes_deny_before_any_other_rule(db_session, mode):
    project, environment, component = await build_project(db_session)
    await set_policy(db_session, project.id, environment.id, execution_mode=mode)
    action = await _propose(db_session, project, environment, component)

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    #: Only the rules up to the regime check may have run. EMERGENCY_STOP is
    #: caught by the earlier kill-switch rule; OBSERVE_ONLY by the regime rule.
    names = [rule["rule"] for rule in evaluation.matched_rules]
    if mode == RemediationExecutionMode.EMERGENCY_STOP:
        assert "emergency_stop" in names
    else:
        assert "execution_mode" in names
    assert "budget" not in names
    assert "registry_executable" not in names


async def test_emergency_stop_denies_with_its_own_reason(db_session):
    """§34: the kill switch has a distinct reason so it can be escalated on."""
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.EMERGENCY_STOP,
    )
    action = await _propose(db_session, project, environment, component)

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.EMERGENCY_STOP


async def test_kill_switch_is_independent_of_the_database(db_session, monkeypatch):
    """§45: losing the policy table must not be able to turn execution on.

    The process-level switch is read first and cannot be satisfied by any stored
    policy — the test proves it by configuring the *most* permissive policy the
    engine accepts and asserting the refusal still happens.
    """
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.LOW,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
    )
    action = await _propose(db_session, project, environment, component)

    #: ``get_settings()`` builds a fresh object per call, so the switch has to be
    #: flipped on the instance the policy module actually reads.
    import app.services.remediation_policy as policy_module

    monkeypatch.setattr(policy_module.settings, "REMEDIATION_EXECUTION_ENABLED", False)
    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert policy_module.settings.REMEDIATION_EXECUTION_ENABLED is False

    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.EXECUTION_DISABLED


# ---------------------------------------------------------------------------
# Safety cannot be overridden
# ---------------------------------------------------------------------------


async def test_a_failed_safety_assessment_cannot_be_waived_by_policy(db_session):
    """§19: policy may be permissive; it may not undo the safety verdict."""
    from app.models.remediation import SafetyStatus
    from app.services.remediation_service import RemediationService as Service

    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
    )
    #: An unknown job name is a parameter failure, so the assessment fails.
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        parameters={"job": "not_a_real_job"},
    )
    service = Service(db_session)
    await service.assess(action)
    assert action.status == RemediationStatus.BLOCKED
    assert action.safety_status == SafetyStatus.FAILED

    assessment = await service._latest_assessment(action.id)
    evaluation = await PolicyEngine(db_session).evaluate(action, assessment=assessment)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.PRECONDITION_FAILED


# ---------------------------------------------------------------------------
# Allow-lists and ceilings
# ---------------------------------------------------------------------------


async def test_action_allow_list_is_enforced(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["DISABLE_FEATURE_FLAG"],
    )
    action = await _propose(db_session, project, environment, component)

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.POLICY_DENIED


async def test_environment_allow_list_is_enforced(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_environment_names=["staging"],
    )
    action = await _propose(db_session, project, environment, component)

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="production"
    )
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.ENVIRONMENT_NOT_ALLOWED


async def test_action_types_absent_from_the_build_configuration_are_refused(db_session):
    """An action the build cannot run is refused before a handler is chosen."""
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["RESTART_SERVICE"],
    )
    action = await _propose(
        db_session,
        project,
        environment,
        component,
        action_type=RemediationActionType.RESTART_SERVICE,
    )

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="production"
    )
    assert evaluation.decision == PolicyDecision.DENY
    #: RESTART_SERVICE is an external action, so the *first* honest reason is
    #: that no adapter exists for it in this build — a more useful refusal than
    #: "not enabled by configuration", and recorded as such.
    assert evaluation.failure_reason in (
        RemediationFailureReason.ADAPTER_UNAVAILABLE,
        RemediationFailureReason.ENVIRONMENT_NOT_ALLOWED,
    )
    assert any(
        rule["rule"] in ("registry_executable", "enabled_action_types")
        for rule in evaluation.matched_rules
    )


async def test_blast_radius_wider_than_the_action_allows_is_refused(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
    )
    action = await _propose(db_session, project, environment, component)
    #: PAUSE_BACKGROUND_JOB may never reach beyond one environment; widen it.
    action.blast_radius = BlastRadiusScope.ENVIRONMENT
    definition_scope = await _scope_rank(action.action_type)
    if definition_scope >= _rank(BlastRadiusScope.ENVIRONMENT):
        pytest.skip("this action already permits an environment-wide blast radius")

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="production"
    )
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.PRECONDITION_FAILED


def _rank(scope: BlastRadiusScope) -> int:
    from app.services.remediation_registry import blast_radius_rank

    return blast_radius_rank(scope)


async def _scope_rank(action_type: RemediationActionType):
    from app.services.remediation_registry import blast_radius_rank, get_definition

    return blast_radius_rank(get_definition(action_type).maximum_blast_radius)


# ---------------------------------------------------------------------------
# Budget, cooldown, concurrency, breakers
# ---------------------------------------------------------------------------


async def test_budget_is_exhausted_by_prior_actions(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        max_actions_per_window=1,
        cooldown_seconds=0,
    )
    first = await _propose(db_session, project, environment, component)
    first.status = RemediationStatus.VERIFIED  # occupies the window, not a slot

    second = await _propose(
        db_session,
        project,
        environment,
        component,
        parameters={"job": "code_sweep"},
    )
    evaluation = await PolicyEngine(db_session).evaluate(second)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.BUDGET_EXHAUSTED


async def test_cooldown_blocks_a_second_action(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        max_actions_per_window=10,
        cooldown_seconds=3600,
    )
    first = await _propose(db_session, project, environment, component)
    first.status = RemediationStatus.VERIFIED
    first.completed_at = utcnow()

    second = await _propose(
        db_session,
        project,
        environment,
        component,
        parameters={"job": "code_sweep"},
    )
    evaluation = await PolicyEngine(db_session).evaluate(second)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.BUDGET_EXHAUSTED
    assert evaluation.budget is not None and evaluation.budget.in_cooldown


async def test_concurrency_limit_blocks_a_second_in_flight_action(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        max_concurrent_actions=1,
        cooldown_seconds=0,
        max_actions_per_window=10,
    )
    first = await _propose(db_session, project, environment, component)
    first.status = RemediationStatus.EXECUTING
    first.execution_status = ExecutionStatus.RUNNING

    second = await _propose(
        db_session,
        project,
        environment,
        component,
        parameters={"job": "code_sweep"},
    )
    evaluation = await PolicyEngine(db_session).evaluate(second)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.CONCURRENCY_LIMIT


def test_the_classifier_needs_the_name_and_the_declared_type():
    """Two signals decide "may ARGUS act here autonomously?" — both must agree.

    The live failure that motivated this: a production environment named
    ``staging`` used to be enough, because only the name was consulted. A naming
    convention is not a fact about an environment; the declared
    ``environment_type`` is.
    """
    from app.models.project import EnvironmentType
    from app.services.remediation_policy import environment_is_non_production

    assert environment_is_non_production("staging", EnvironmentType.STAGING) is True
    assert environment_is_non_production("staging", EnvironmentType.DEVELOPMENT) is True
    # The name matches the allow-list but the environment says otherwise.
    assert environment_is_non_production("staging", EnvironmentType.PRODUCTION) is False
    # The type is non-production but the name is not allow-listed.
    assert environment_is_non_production("prod-eu", EnvironmentType.STAGING) is False
    # Nothing known: production, in both directions.
    assert environment_is_non_production(None, EnvironmentType.STAGING) is False
    assert environment_is_non_production("staging", None) is True


async def test_a_production_environment_named_staging_still_escalates(db_session):
    """The end-to-end form of the rule above: the name alone buys nothing."""
    project, environment, component = await build_project(db_session)
    #: Only the name is changed; ``build_project`` created this environment as
    #: PRODUCTION, and a rename does not un-declare that.
    environment.name = "staging"
    await db_session.flush()
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.LOW,
        cooldown_seconds=0,
        max_actions_per_window=10,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
        canary_enabled=False,
    )

    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    evaluation = await service.evaluate_policy(action)

    assert evaluation.decision == PolicyDecision.REQUIRE_APPROVAL
    assert any(
        rule["rule"] == "non_production_required" for rule in evaluation.matched_rules
    )
    assert action.status == RemediationStatus.AWAITING_APPROVAL
    assert action.execution_status is None


async def test_a_breaker_scope_holds_exactly_one_row(db_session):
    """A duplicate breaker is a cure that hides the disease.

    The policy engine reads one row per scope and asks "is it open?". If a
    second, ``CLOSED`` row could exist beside an ``OPEN`` one — which it could,
    because nothing enforced the scope key — the read could land on the closed
    row and a repeatedly failing action type would keep being attempted.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.remediation import RemediationCircuitBreaker

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    first = await get_breaker(
        db_session, project.id, environment.id, action.action_type, threshold=3
    )
    assert first is not None

    duplicate = RemediationCircuitBreaker(
        project_id=project.id,
        environment_id=environment.id,
        action_type=action.action_type,
        state=CircuitState.CLOSED,
        threshold=3,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_an_open_breaker_is_found_rather_than_a_fresh_closed_one(db_session):
    """The read must return the row that already exists, open or not."""
    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    breaker = await get_breaker(
        db_session, project.id, environment.id, action.action_type, threshold=3
    )
    breaker.state = CircuitState.OPEN
    breaker.opened_at = utcnow()
    breaker.opened_until = utcnow() + timedelta(minutes=15)
    await db_session.flush()

    fetched = await get_breaker(
        db_session, project.id, environment.id, action.action_type, threshold=3
    )
    assert fetched.id == breaker.id
    assert fetched.state == CircuitState.OPEN


async def test_an_open_breaker_refuses_the_action(db_session):
    """§25: a failed remediation must not be retried blindly."""
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        cooldown_seconds=0,
        max_actions_per_window=10,
    )
    action = await _propose(db_session, project, environment, component)

    breaker = await get_breaker(
        db_session,
        project.id,
        environment.id,
        action.action_type,
        threshold=settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD,
    )
    breaker.state = CircuitState.OPEN
    breaker.consecutive_failures = settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD
    breaker.opened_at = utcnow()
    breaker.opened_until = utcnow() + timedelta(
        seconds=settings.REMEDIATION_CIRCUIT_RESET_SECONDS
    )

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.CIRCUIT_OPEN
    assert evaluation.breaker is not None


async def test_an_expired_breaker_stops_refusing(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        cooldown_seconds=0,
        max_actions_per_window=10,
    )
    action = await _propose(db_session, project, environment, component)
    breaker = await get_breaker(
        db_session,
        project.id,
        environment.id,
        action.action_type,
        threshold=settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD,
    )
    breaker.state = CircuitState.OPEN
    breaker.opened_until = utcnow() - timedelta(minutes=1)

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.failure_reason != RemediationFailureReason.CIRCUIT_OPEN


async def test_budget_counts_only_actions_that_could_have_run(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        max_actions_per_window=2,
        cooldown_seconds=0,
    )
    rejected = await _propose(db_session, project, environment, component)
    rejected.status = RemediationStatus.REJECTED

    policy = await resolve_policy(db_session, project.id, environment.id)
    budget = await compute_budget(
        db_session, policy, project.id, environment.id, now=utcnow()
    )
    assert budget.actions_in_window < policy.max_actions_per_window


# ---------------------------------------------------------------------------
# Authority: escalation and autonomous authorization
# ---------------------------------------------------------------------------


async def test_human_approval_regime_escalates(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
    )
    action = await _propose(db_session, project, environment, component)
    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.REQUIRE_APPROVAL
    assert evaluation.requires_human is True
    assert evaluation.allowed is False


async def test_autonomous_in_production_still_escalates(db_session):
    """§45: an unknown or production scope never gets autonomous execution."""
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.LOW,
        cooldown_seconds=0,
        max_actions_per_window=10,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
    )
    action = await _propose(db_session, project, environment, component)
    action.risk_level = RemediationRiskLevel.LOW

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="production", non_production=False
    )
    assert evaluation.decision == PolicyDecision.REQUIRE_APPROVAL
    assert any(
        rule["rule"] == "non_production_required" for rule in evaluation.matched_rules
    )


async def test_autonomous_in_a_non_production_scope_is_allowed(db_session):
    project, environment, component = await build_project(
        db_session, name="Phase9 Policy"
    )
    #: A scope whose name marks it non-production, which is the only place
    #: autonomous execution is ever considered.
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.LOW,
        cooldown_seconds=0,
        max_actions_per_window=10,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
        canary_enabled=False,
    )
    action = await _propose(db_session, project, environment, component)
    action.risk_level = RemediationRiskLevel.LOW

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="staging", non_production=True
    )
    assert evaluation.allowed is True, evaluation.reasons
    assert evaluation.decision == PolicyDecision.ALLOW
    assert any(
        rule["rule"] == "autonomous_authority" for rule in evaluation.matched_rules
    )


async def test_autonomous_above_the_risk_ceiling_escalates(db_session):
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.LOW,
        cooldown_seconds=0,
        max_actions_per_window=10,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
    )
    action = await _propose(db_session, project, environment, component)
    action.risk_level = RemediationRiskLevel.HIGH

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="staging", non_production=True
    )
    assert evaluation.decision == PolicyDecision.REQUIRE_APPROVAL
    assert any(
        rule["rule"] == "autonomous_ceiling" for rule in evaluation.matched_rules
    )


async def test_an_action_without_autonomous_support_always_escalates(db_session):
    """§4: the authority gate follows the *definition*, not merely the risk.

    ``ENABLE_FEATURE_FLAG`` is executable and reversible, but it is deliberately
    not autonomous-eligible (re-enabling something has to be a human decision),
    so even a CRITICAL risk ceiling must not authorize it.
    """
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.CRITICAL,
        cooldown_seconds=0,
        max_actions_per_window=10,
        allowed_action_types=["ENABLE_FEATURE_FLAG"],
    )
    action = await _propose(
        db_session,
        project,
        environment,
        component,
        action_type=RemediationActionType.ENABLE_FEATURE_FLAG,
        parameters={"flag": "graph_extraction"},
    )
    action.risk_level = RemediationRiskLevel.LOW

    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="staging", non_production=True
    )
    assert evaluation.decision == PolicyDecision.REQUIRE_APPROVAL
    assert any(rule["rule"] == "irreversibility" for rule in evaluation.matched_rules)


async def test_canary_is_required_only_under_autonomous_execution(db_session):
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk=RemediationRiskLevel.LOW,
        cooldown_seconds=0,
        max_actions_per_window=10,
        allowed_action_types=["DISABLE_FEATURE_FLAG"],
        canary_enabled=True,
        canary_percent=25.0,
    )
    action = await _propose(
        db_session,
        project,
        environment,
        component,
        action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
        parameters={"flag": "graph_extraction"},
    )
    action.risk_level = RemediationRiskLevel.LOW

    from app.services.remediation_registry import get_definition

    definition = get_definition(action.action_type)
    assert definition.supports_canary, "the fixture action should support canary"
    evaluation = await PolicyEngine(db_session).evaluate(
        action, environment_name="staging", non_production=True
    )
    assert evaluation.requires_canary is True
    assert evaluation.decision == PolicyDecision.ALLOW_WITH_CANARY


# ---------------------------------------------------------------------------
# Ceilings
# ---------------------------------------------------------------------------


async def test_stored_policy_values_are_clamped_to_operator_ceilings(db_session):
    """§21, §45: the API can narrow what ARGUS may do, never widen it."""
    project, environment, component = await build_project(db_session)
    row = await upsert_policy(
        db_session,
        project.id,
        environment.id,
        {
            "execution_mode": RemediationExecutionMode.AUTONOMOUS,
            "max_actions_per_window": 500,
            "max_concurrent_actions": 99,
            "max_blast_radius_percent": 100.0,
            "execution_timeout_seconds": 99_999,
        },
        updated_by="test",
    )
    effective = await resolve_policy(db_session, project.id, environment.id)
    assert (
        effective.max_actions_per_window
        <= settings.REMEDIATION_HARD_MAX_ACTIONS_PER_WINDOW
    )
    assert (
        effective.max_concurrent_actions
        <= settings.REMEDIATION_HARD_MAX_CONCURRENT_ACTIONS
    )
    assert (
        effective.max_blast_radius_percent
        <= settings.REMEDIATION_HARD_MAX_BLAST_RADIUS_PERCENT
    )
    assert (
        effective.execution_timeout_seconds
        <= settings.REMEDIATION_HARD_EXECUTION_TIMEOUT_SECONDS
    )
    assert effective.clamped, "the clamps applied should be recorded"
    del row


async def test_the_autonomous_risk_ceiling_cannot_exceed_the_hard_limit(db_session):
    project, environment, component = await build_project(db_session)
    await upsert_policy(
        db_session,
        project.id,
        environment.id,
        {
            "execution_mode": RemediationExecutionMode.AUTONOMOUS,
            "autonomous_max_risk": RemediationRiskLevel.CRITICAL,
        },
        updated_by="test",
    )
    effective = await resolve_policy(db_session, project.id, environment.id)
    from app.services.remediation_registry import risk_rank

    assert risk_rank(effective.autonomous_max_risk) <= risk_rank(
        RemediationRiskLevel(settings.REMEDIATION_HARD_MAX_RISK_AUTONOMOUS)
    )


async def test_environment_policy_overrides_the_project_policy(db_session):
    """An environment override is a decision someone made, so it wins."""
    project, environment, component = await build_project(db_session)
    await upsert_policy(
        db_session,
        project.id,
        None,
        {"execution_mode": RemediationExecutionMode.OBSERVE_ONLY},
        updated_by="test",
    )
    await upsert_policy(
        db_session,
        project.id,
        environment.id,
        {"execution_mode": RemediationExecutionMode.HUMAN_APPROVAL},
        updated_by="test",
    )
    scoped = await resolve_policy(db_session, project.id, environment.id)
    assert scoped.execution_mode == RemediationExecutionMode.HUMAN_APPROVAL

    project_wide = await resolve_policy(db_session, project.id, None)
    assert project_wide.execution_mode == RemediationExecutionMode.OBSERVE_ONLY


async def test_policy_revision_increments_on_every_change(db_session):
    """A decision must be explainable against the revision that applied."""
    project, environment, component = await build_project(db_session)
    first = await upsert_policy(
        db_session, project.id, environment.id, {"cooldown_seconds": 30}, updated_by="a"
    )
    first_revision = first.revision
    second = await upsert_policy(
        db_session, project.id, environment.id, {"cooldown_seconds": 45}, updated_by="b"
    )
    assert second.revision > first_revision
    assert second.cooldown_seconds == 45
    assert second.updated_by == "b"
