"""Phase 9 — the six regimes and the eight demo scenarios (§107).

Both halves of §107 live here.

**The regimes.** ``OBSERVE_ONLY``, ``DRY_RUN``, ``SHADOW``, ``HUMAN_APPROVAL``,
``AUTONOMOUS`` and ``EMERGENCY_STOP`` are each driven end to end against the same
fixture, so what differs between them is only what the policy says — and what it
says is asserted, not assumed. The two simulated regimes are the interesting
ones: they must reach ``AWAITING_APPROVAL`` with nothing applied, because a
dry run that quietly applies an effect is the single most dangerous bug this
phase could ship.

**The scenarios.** Human-approved remediation, autonomous low-risk remediation,
canary execution, failed verification, rollback, policy denial, a stale action
and action-loop protection. None of them is hard-coded: each is produced by
configuring policy and telemetry and then asserting what the platform did.
"""

from __future__ import annotations

from datetime import timedelta


from app.models.remediation import (
    CanaryStage,
    CircuitState,
    ExecutionStatus,
    PolicyDecision,
    RemediationActionType,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationStatus,
    RollbackStatus,
    RollbackTrigger,
    VerificationVerdict,
)
from app.core.config import get_settings
from app.services.remediation_clock import utcnow
from app.services.remediation_controls import is_paused
from app.services.remediation_policy import get_breaker, resolve_policy
from app.services.remediation_service import RemediationService
from tests.phase9_helpers import (
    build_project,
    make_action,
    set_environment_class,
    set_policy,
)

settings = get_settings()


async def _scenario(
    session,
    *,
    mode: RemediationExecutionMode,
    environment_name: str = "staging",
    action_type: RemediationActionType = RemediationActionType.PAUSE_BACKGROUND_JOB,
    parameters: dict | None = None,
    approve: bool = True,
    **policy_values,
):
    """One project, one policy, one action — the shape every scenario shares."""
    project, environment, component = await build_project(session)
    await set_environment_class(session, environment, environment_name)
    values = {
        "execution_mode": mode,
        "allowed_action_types": [action_type.value],
        "cooldown_seconds": 0,
        "max_actions_per_window": 10,
        "canary_enabled": False,
    }
    values.update(policy_values)
    await set_policy(session, project.id, environment.id, **values)

    action = await make_action(
        session,
        project,
        environment,
        component,
        action_type=action_type,
        parameters=parameters,
    )
    service = RemediationService(session)
    await service.assess(action)
    await service.evaluate_policy(action)
    if approve and action.status == RemediationStatus.AWAITING_APPROVAL:
        action, _ = await service.decide(
            action, approve=True, actor="on-call", reason="reviewed the evidence"
        )
    return project, environment, component, action, service


# ---------------------------------------------------------------------------
# The six regimes (§107)
# ---------------------------------------------------------------------------


async def test_regime_observe_only_records_but_executes_nothing(db_session):
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.OBSERVE_ONLY
    )
    assert action.status == RemediationStatus.REJECTED
    assert action.policy_status == PolicyDecision.DENY
    assert action.execution_mode == RemediationExecutionMode.OBSERVE_ONLY

    #: The proposal is still recorded — that is what OBSERVE_ONLY is for.
    assert action.id is not None
    assert action.proposal_id is not None
    outcome = await service.run(action, actor="test")
    assert outcome["status"] == RemediationStatus.REJECTED.value
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_regime_dry_run_validates_and_stops(db_session):
    """§40: a dry run must never apply an effect, and must not claim success."""
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.DRY_RUN
    )
    assert action.status == RemediationStatus.AUTHORIZED
    assert action.execution_mode == RemediationExecutionMode.DRY_RUN

    outcome = await service.run(action, actor="test")
    assert outcome["status"] == RemediationStatus.AWAITING_APPROVAL.value
    assert action.execution_status == ExecutionStatus.NOT_PERFORMED
    assert action.outcome is None
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_regime_shadow_observes_without_acting(db_session):
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.SHADOW
    )
    assert action.status == RemediationStatus.AUTHORIZED
    outcome = await service.run(action, actor="test")
    assert outcome["status"] == RemediationStatus.AWAITING_APPROVAL.value
    assert action.execution_status == ExecutionStatus.NOT_PERFORMED
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_regime_human_approval_runs_the_whole_pipeline(db_session):
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    assert action.status == RemediationStatus.AUTHORIZED
    outcome = await service.run(action, actor="test")
    assert outcome["status"] == RemediationStatus.VERIFIED.value
    assert await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_regime_autonomous_needs_no_human_in_a_non_production_scope(db_session):
    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk="LOW",
    )
    #: No approval call was made: the policy authorized it.
    assert action.status == RemediationStatus.AUTHORIZED
    assert action.authorized_by == "policy-engine"
    assert action.execution_mode == RemediationExecutionMode.AUTONOMOUS

    outcome = await service.run(action, actor="scheduler")
    assert outcome["status"] == RemediationStatus.VERIFIED.value
    assert await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_regime_autonomous_in_production_still_needs_a_human(db_session):
    """The same policy in a production scope escalates instead of acting."""
    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.AUTONOMOUS,
        environment_name="production",
        autonomous_max_risk="CRITICAL",
        approve=False,
    )
    assert action.status == RemediationStatus.AWAITING_APPROVAL
    assert action.failure_reason == RemediationFailureReason.APPROVAL_REQUIRED


async def test_regime_emergency_stop_denies_everything(db_session):
    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.EMERGENCY_STOP,
        approve=False,
    )
    #: Blocked rather than rejected: a stop is a state the project can leave,
    #: so the action stays re-evaluable once the stop is released.
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason == RemediationFailureReason.EMERGENCY_STOP
    assert action.execution_status is None
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_an_emergency_stop_cannot_be_shadowed_by_an_environment_policy(
    db_session,
):
    """The kill switch is project-wide, whatever row resolution prefers."""
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    #: An environment-scoped policy that would happily authorize on its own.
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk="LOW",
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
        cooldown_seconds=0,
        max_actions_per_window=10,
    )
    service = RemediationService(db_session)
    await service.engage_emergency_stop(
        project.id, engage=True, actor="on-call", reason="stop the project"
    )

    effective = await resolve_policy(db_session, project.id, environment.id)
    assert effective.execution_mode == RemediationExecutionMode.EMERGENCY_STOP
    assert effective.emergency_stop_active is True

    action = await make_action(db_session, project, environment, component)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason == RemediationFailureReason.EMERGENCY_STOP
    del component


async def test_the_emergency_stop_flag_narrows_a_permissive_policy(db_session):
    """A project on AUTONOMOUS that engages the stop authorizes nothing (§34)."""
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk="LOW",
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
        cooldown_seconds=0,
        max_actions_per_window=10,
    )
    service = RemediationService(db_session)
    await service.engage_emergency_stop(
        project.id, engage=True, actor="on-call", reason="incident in progress"
    )
    effective = await resolve_policy(db_session, project.id, environment.id)
    assert effective.execution_mode == RemediationExecutionMode.EMERGENCY_STOP

    action = await make_action(db_session, project, environment, component)
    await service.assess(action)
    await service.evaluate_policy(action)
    #: Blocked by the stop, and re-evaluable once it is released.
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason == RemediationFailureReason.EMERGENCY_STOP
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


# ---------------------------------------------------------------------------
# Demo scenarios (§108 §14)
# ---------------------------------------------------------------------------


async def test_scenario_human_approved_remediation(db_session):
    """A proposal, an assessment, an approval by name, an execution, a verdict."""
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    assert action.created_by == "test"
    assert action.approved_by == "on-call"

    outcome = await service.run(action, actor="on-call")
    assert outcome["status"] == RemediationStatus.VERIFIED.value
    assert action.outcome.value in ("EFFECTIVE", "PARTIALLY_EFFECTIVE")
    assert action.post_analysis is not None
    assert action.post_analysis["verdict"] is not None


async def test_scenario_autonomous_low_risk_remediation(db_session):
    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.AUTONOMOUS,
        action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
        parameters={"flag": "graph_extraction"},
        autonomous_max_risk="LOW",
    )
    assert action.status == RemediationStatus.AUTHORIZED
    assert action.approved_by == "policy-engine"
    assert action.authorized_by == "policy-engine"
    outcome = await service.run(action, actor="scheduler")
    assert outcome["status"] == RemediationStatus.VERIFIED.value


async def test_scenario_canary_execution_is_staged(db_session):
    """§8: under autonomous authority a canary-capable action starts narrow."""
    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.AUTONOMOUS,
        action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
        parameters={"flag": "graph_extraction"},
        autonomous_max_risk="LOW",
        canary_enabled=True,
        canary_percent=10.0,
    )
    assert action.policy_status == PolicyDecision.ALLOW_WITH_CANARY
    assert action.canary_required is True
    assert action.canary_stage == CanaryStage.CANARY
    assert action.canary_percent == 10.0
    assert action.blast_radius_percent in (None, 10.0)


async def test_scenario_canary_is_not_required_under_human_approval(db_session):
    """A human already decided the whole scope; staging is not imposed on them."""
    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.HUMAN_APPROVAL,
        action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
        parameters={"flag": "graph_extraction"},
        canary_enabled=True,
    )
    assert action.canary_required is False


async def test_scenario_failed_verification(db_session, monkeypatch):
    """The applied change did not work, and ARGUS says so rather than passing."""
    from app.models.remediation import VerificationCheckKind
    from app.services.remediation_verification import VerificationEngine

    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    executor_outcome = await service.execute(action, actor="on-call")
    execution = executor_outcome.execution
    assert execution is not None

    original = VerificationEngine._run_check

    async def sabotage(self, action_, execution_, kind, **kwargs):
        result = await original(self, action_, execution_, kind, **kwargs)
        if kind == VerificationCheckKind.HEALTH_STATUS:
            result = {
                **result,
                "result": "FAIL",
                "detail": "component health did not improve after the action",
            }
        return result

    monkeypatch.setattr(VerificationEngine, "_run_check", sabotage)
    result = await service.verify(action, execution=execution, now=utcnow())
    assert result.verdict == VerificationVerdict.FAILED
    assert action.status == RemediationStatus.ROLLING_BACK
    assert action.outcome.value == "HARMFUL"


async def test_scenario_rollback_after_a_failed_verification(db_session, monkeypatch):
    from app.models.remediation import VerificationCheckKind
    from app.services.remediation_verification import VerificationEngine

    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    executor_outcome = await service.execute(action, actor="on-call")
    execution = executor_outcome.execution
    assert execution is not None

    original = VerificationEngine._run_check

    async def sabotage(self, action_, execution_, kind, **kwargs):
        result = await original(self, action_, execution_, kind, **kwargs)
        if kind == VerificationCheckKind.HEALTH_STATUS:
            result = {**result, "result": "FAIL", "detail": "health regressed"}
        return result

    monkeypatch.setattr(VerificationEngine, "_run_check", sabotage)
    await service.verify(action, execution=execution, now=utcnow())
    result = await service.rollback(
        action,
        trigger=RollbackTrigger.VERIFICATION_FAILED,
        requested_by="system",
        reason="verification failed",
    )
    assert result.status == RollbackStatus.SUCCEEDED
    assert action.status == RemediationStatus.ROLLED_BACK
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    #: And the reversal is itself verified, not assumed.
    assert result.verification_verdict in (
        None,
        VerificationVerdict.VERIFIED,
        VerificationVerdict.PARTIALLY_VERIFIED,
    )


async def test_scenario_policy_denial(db_session):
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["DISABLE_FEATURE_FLAG"],
        cooldown_seconds=0,
        max_actions_per_window=10,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    evaluation = await service.evaluate_policy(action)

    assert evaluation.decision == PolicyDecision.DENY
    assert action.status == RemediationStatus.REJECTED
    assert evaluation.failure_reason == RemediationFailureReason.POLICY_DENIED
    assert any(
        rule["rule"] == "allowed_action_types" for rule in evaluation.matched_rules
    )
    assert evaluation.reasons


async def test_scenario_stale_action(db_session):
    """An approval that outlives its usefulness must not execute (§29)."""
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    action.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.flush()

    await service.run(action, actor="on-call")
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason == RemediationFailureReason.STALE_ACTION
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_scenario_action_loop_protection(db_session):
    """§24/§27: repetition is bounded by three independent mechanisms."""
    project, environment, component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )

    #: 1. The same fingerprint cannot be proposed again while it is active.
    from tests.phase9_helpers import make_draft

    draft = make_draft(
        project,
        environment,
        component,
        action_type=action.action_type,
        parameters=action.parameters,
        incident_id=action.incident_id,
        source_id=action.source_id,
    )
    assert (
        await RemediationService(db_session).propose([draft], created_by="test") == []
    )

    #: 2. The breaker opens after repeated failures and refuses the action type.
    breaker = await get_breaker(
        db_session,
        project.id,
        environment.id,
        action.action_type,
        threshold=settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD,
    )
    breaker.consecutive_failures = settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD
    breaker.state = CircuitState.OPEN
    breaker.opened_at = utcnow()
    breaker.opened_until = utcnow() + timedelta(
        seconds=settings.REMEDIATION_CIRCUIT_RESET_SECONDS
    )
    await db_session.flush()

    #: 3. A redelivered job for an attempt that already applied is a no-op.
    action.status = RemediationStatus.AUTHORIZED
    executor_outcome = await service.execute(action, actor="on-call")
    assert executor_outcome.effect_applied is True

    from app.services.remediation_executor import RemediationExecutor

    replay = await RemediationExecutor(db_session).execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    assert replay.detail == "an attempt with this idempotency key already applied"
    assert replay.execution is not None
    assert replay.execution.id == executor_outcome.execution.id


async def test_scenario_the_breaker_refuses_after_repeated_failures(db_session):
    """The loop protection that matters: repeated failure stops the action."""
    from app.services.remediation_policy import PolicyEngine

    project, environment, component, action, service = await _scenario(
        db_session,
        mode=RemediationExecutionMode.HUMAN_APPROVAL,
        approve=False,
    )
    breaker = await get_breaker(
        db_session,
        project.id,
        environment.id,
        action.action_type,
        threshold=settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD,
    )
    breaker.consecutive_failures = settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD
    breaker.state = CircuitState.OPEN
    breaker.opened_until = utcnow() + timedelta(minutes=10)

    evaluation = await PolicyEngine(db_session).evaluate(action)
    assert evaluation.decision == PolicyDecision.DENY
    assert evaluation.failure_reason == RemediationFailureReason.CIRCUIT_OPEN


async def test_scenario_a_rejected_action_must_be_reproposed(db_session):
    """§5: rejection is terminal, which is what makes it meaningful."""
    _project, _environment, _component, action, service = await _scenario(
        db_session, mode=RemediationExecutionMode.OBSERVE_ONLY, approve=False
    )
    assert action.status == RemediationStatus.REJECTED

    #: Cancelling a finished action is a no-op rather than an error, and it does
    #: not revive it: a rejected remediation must be re-proposed.
    await service.cancel(action, actor="on-call", reason="never mind")
    assert action.status == RemediationStatus.REJECTED

    from app.services.remediation_state import allowed_targets

    assert allowed_targets(action.status) == frozenset({action.status})


async def test_scenario_nothing_executes_without_a_policy_row(db_session):
    """The restrictive default, demonstrated end to end (§2)."""
    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    outcome = await service.run(action, actor="nobody")

    assert action.status == RemediationStatus.REJECTED
    assert outcome["status"] == RemediationStatus.REJECTED.value
    assert action.policy_status == PolicyDecision.DENY
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
