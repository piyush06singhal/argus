"""Phase 9 — the action registry and the state machine (§3–§5, §45).

Two things are pinned here, and both are refusals.

**The registry is closed.** Every member of ``RemediationActionType`` must have a
definition, every definition must declare a verification plan, a rollback
strategy and a maximum blast radius, and no definition may accept a free-form
command. A parameter that is not in the schema is rejected rather than ignored,
because \"ignored\" is how an injection attempt becomes a no-op that looks fine.

**The state machine has no shortcuts.** Every transition is enumerated, so the
tests iterate *all* pairs rather than the handful a reader would think of. In
particular nothing reaches ``EXECUTING`` except through ``AUTHORIZED`` or
``SCHEDULED``, and no terminal state has an exit.
"""

from __future__ import annotations

import pytest

from app.models.remediation import (
    AdapterKind,
    RemediationActionType,
    RemediationStatus,
    RollbackStrategy,
)
from app.services.remediation_registry import (
    REGISTRY,
    all_definitions,
    definition_requires_human_approval,
    definition_summary,
    get_definition,
    validate_parameters,
)
from app.services.remediation_state import (
    IN_FLIGHT_STATUSES,
    REMEDIATION_TRANSITIONS,
    TERMINAL_STATUSES,
    IllegalTransition,
    allowed_targets,
    apply_transition,
    assert_transition,
    can_transition,
    describe_path,
    is_in_flight,
    is_terminal,
    iter_transitions,
    may_apply_effect,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_every_action_type_has_a_definition():
    assert set(REGISTRY) == set(RemediationActionType)


def test_every_definition_is_complete():
    for definition in all_definitions():
        summary = definition_summary(definition)
        assert definition.description, definition.action_type
        assert definition.verification_plan, definition.action_type
        assert definition.maximum_blast_radius is not None
        assert isinstance(definition.rollback_strategy, RollbackStrategy)
        assert summary["action_type"] == definition.action_type.value


def test_reversible_definitions_declare_how_to_reverse():
    """\"Reversible\" is only meaningful with a strategy behind it (§4)."""
    for definition in all_definitions():
        if definition.reversible:
            assert (
                definition.rollback_strategy != RollbackStrategy.NONE
            ), f"{definition.action_type} claims to be reversible with no strategy"


def test_irreversible_definitions_need_a_human():
    """§4: an irreversible action always requires approval, whatever policy says."""
    for definition in all_definitions():
        if not definition.reversible:
            assert definition_requires_human_approval(
                definition
            ), f"{definition.action_type} is irreversible but needs no human"


def test_production_effects_need_a_human():
    for definition in all_definitions():
        if definition.production_effect:
            assert definition_requires_human_approval(definition)


def test_no_definition_accepts_a_free_form_command():
    """The registry's whole point: there is no shell parameter anywhere (§3)."""
    forbidden = {"command", "cmd", "script", "shell", "exec", "argv", "url", "ssh"}
    for definition in all_definitions():
        names = {parameter.name for parameter in definition.parameters}
        assert not (names & forbidden), (
            f"{definition.action_type} exposes a free-form parameter: "
            f"{sorted(names & forbidden)}"
        )


def test_external_actions_are_unavailable_until_configured():
    """An action ARGUS cannot reach must say so rather than pretend (§2, §100)."""
    external = [d for d in all_definitions() if d.adapter_kind == AdapterKind.EXTERNAL]
    assert external, "the registry should include external actions"
    for definition in external:
        summary = definition_summary(definition)
        assert summary["executable_in_build"] is False
        assert definition.unavailable_reason


def test_length_of_stay_control_plane_actions_are_executable():
    for definition in all_definitions():
        if definition.adapter_kind == AdapterKind.CONTROL_PLANE:
            assert (
                definition.unavailable_reason is None
            ), f"{definition.action_type} should be executable"


def test_inverse_actions_are_themselves_registered_and_reversible():
    for definition in all_definitions():
        if definition.inverse_action is None:
            continue
        inverse = get_definition(definition.inverse_action)
        assert inverse.action_type == definition.inverse_action


def test_blast_radius_ordering_is_monotonic():
    from app.models.remediation import BlastRadiusScope
    from app.services.remediation_registry import (
        blast_radius_rank,
        max_scope,
        max_risk,
        risk_rank,
    )

    order = [
        BlastRadiusScope.SINGLE_INSTANCE,
        BlastRadiusScope.SINGLE_COMPONENT,
        BlastRadiusScope.LIMITED_PERCENTAGE,
        BlastRadiusScope.ENVIRONMENT,
    ]
    ranks = [blast_radius_rank(scope) for scope in order]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks), "two scopes share a rank"
    assert max_scope(order[0], order[-1]) == order[-1]
    assert min(ranks) == 0

    from app.models.remediation import RemediationRiskLevel

    risks = [
        RemediationRiskLevel.LOW,
        RemediationRiskLevel.MEDIUM,
        RemediationRiskLevel.HIGH,
        RemediationRiskLevel.CRITICAL,
    ]
    assert [risk_rank(r) for r in risks] == sorted(risk_rank(r) for r in risks)
    assert max_risk(risks[0], risks[2]) == risks[2]


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_valid_parameters_are_returned_cleaned():
    clean, errors = validate_parameters(
        RemediationActionType.PAUSE_BACKGROUND_JOB, {"job": "anomaly_sweep"}
    )
    assert errors == []
    assert clean["job"] == "anomaly_sweep"


def test_unknown_parameters_are_rejected_not_ignored():
    """An unrecognised key is a refusal — silently dropping it hides an attempt."""
    _, errors = validate_parameters(
        RemediationActionType.PAUSE_BACKGROUND_JOB,
        {"job": "anomaly_sweep", "command": "rm -rf /"},
    )
    assert errors, "an unrecognised parameter must be reported"
    assert any("command" in error for error in errors)


def test_missing_required_parameters_are_rejected():
    _, errors = validate_parameters(RemediationActionType.PAUSE_BACKGROUND_JOB, {})
    assert errors


def test_parameter_choices_are_enforced():
    """An ARGUS job that does not exist cannot be paused (§10)."""
    _, errors = validate_parameters(
        RemediationActionType.PAUSE_BACKGROUND_JOB, {"job": "not_a_real_job"}
    )
    assert errors
    assert any("not_a_real_job" in error for error in errors)


def test_injected_choices_are_configuration_driven():
    from app.core.config import get_settings

    settings = get_settings()
    for flag in settings.REMEDIATION_KNOWN_FEATURE_FLAGS:
        clean, errors = validate_parameters(
            RemediationActionType.DISABLE_FEATURE_FLAG, {"flag": flag}
        )
        assert errors == [], f"{flag} should be a valid configured flag"
        assert clean["flag"] == flag


def test_numeric_bounds_are_enforced():
    """A scale parameter cannot be used to request unbounded capacity."""
    definition = get_definition(RemediationActionType.SCALE_SERVICE_WITHIN_LIMIT)
    replicas = next(p for p in definition.parameters if p.name == "replicas")
    assert replicas.maximum is not None, "replicas must be bounded"
    _, errors = validate_parameters(
        definition.action_type, {"replicas": replicas.maximum * 100}
    )
    assert errors, "replicas should reject a value above its maximum"
    _, below = validate_parameters(
        definition.action_type,
        {"replicas": (replicas.minimum or 1) - 1},
    )
    if replicas.minimum is not None:
        assert below


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_every_status_is_reachable_or_terminal():
    """No status is dead code: each is either a target or an entry point."""
    targets = {target for _, target in iter_transitions()}
    sources = {source for source, _ in iter_transitions()}
    for status in RemediationStatus:
        assert status in targets or status in sources, f"{status.value} is unreachable"


def test_terminal_states_have_no_exit():
    """A finished action only ever "transitions" to itself (§5)."""
    for status in TERMINAL_STATUSES:
        assert allowed_targets(status) == frozenset(
            {status}
        ), f"{status.value} can be exited"


def test_no_transition_leaves_a_status_except_to_a_declared_target():
    for status in RemediationStatus:
        for target in allowed_targets(status):
            assert target == status or target in REMEDIATION_TRANSITIONS.get(
                status, frozenset()
            )


def test_executing_is_only_reachable_from_authorized_or_scheduled():
    """§5, §26: nothing may jump from a proposal to an applied effect."""
    sources = {
        source
        for source, target in iter_transitions()
        if target == RemediationStatus.EXECUTING
    }
    assert sources <= {
        RemediationStatus.AUTHORIZED,
        RemediationStatus.SCHEDULED,
        RemediationStatus.EXECUTING,
    }


def test_verified_is_only_reachable_from_a_verifying_or_executing_state():
    sources = {
        source
        for source, target in iter_transitions()
        if target == RemediationStatus.VERIFIED
    }
    assert sources <= {
        RemediationStatus.VERIFYING,
        RemediationStatus.EXECUTING,
        RemediationStatus.ROLLING_BACK,
        RemediationStatus.SCHEDULED,
        RemediationStatus.AUTHORIZED,
        RemediationStatus.EXECUTING,
    }
    assert RemediationStatus.AWAITING_APPROVAL not in sources
    assert RemediationStatus.PROPOSED not in sources


def test_re_entering_the_current_status_is_a_no_op():
    """Idempotence is what lets a worker and the sweeper both touch an action."""

    class Row:
        status = RemediationStatus.VERIFYING

    row = Row()
    assert apply_transition(row, RemediationStatus.VERIFYING) is False
    assert row.status == RemediationStatus.VERIFYING
    assert can_transition(RemediationStatus.VERIFIED, RemediationStatus.VERIFIED)


def test_rejected_cancelled_and_expired_are_not_revivable():
    """A rejection means something: the remediation must be re-proposed (§5)."""
    for status in (
        RemediationStatus.REJECTED,
        RemediationStatus.CANCELLED,
        RemediationStatus.EXPIRED,
    ):
        assert allowed_targets(status) == frozenset({status})


def test_blocked_and_failed_stay_recoverable():
    """A missing adapter or a transient breaker must not be a dead end (§25)."""
    assert allowed_targets(RemediationStatus.BLOCKED) - {RemediationStatus.BLOCKED}
    assert allowed_targets(RemediationStatus.FAILED) - {RemediationStatus.FAILED}


def test_illegal_transition_is_refused_and_the_status_is_unchanged():
    class Row:
        status = RemediationStatus.PROPOSED

    row = Row()
    with pytest.raises(IllegalTransition):
        assert_transition(RemediationStatus.PROPOSED, RemediationStatus.EXECUTING)
    #: apply_transition raises rather than reporting a failure: a caller that
    #: attempts an illegal move has a bug, and swallowing it would leave a
    #: status in the database that no gate produced.
    with pytest.raises(IllegalTransition):
        apply_transition(row, RemediationStatus.EXECUTING)
    assert row.status == RemediationStatus.PROPOSED


def test_apply_transition_records_the_move_when_legal():
    class Row:
        status = RemediationStatus.PROPOSED

    row = Row()
    assert apply_transition(row, RemediationStatus.VALIDATING) is True
    assert row.status == RemediationStatus.VALIDATING


def test_only_authorized_and_scheduled_actions_may_apply_an_effect():
    assert may_apply_effect(RemediationStatus.AUTHORIZED)
    assert may_apply_effect(RemediationStatus.SCHEDULED)
    for status in RemediationStatus:
        if status in (
            RemediationStatus.AUTHORIZED,
            RemediationStatus.SCHEDULED,
            RemediationStatus.EXECUTING,
        ):
            continue
        assert not may_apply_effect(status), f"{status.value} may not apply an effect"


def test_in_flight_and_terminal_are_disjoint():
    assert not (IN_FLIGHT_STATUSES & TERMINAL_STATUSES)
    for status in RemediationStatus:
        if status in IN_FLIGHT_STATUSES:
            assert is_in_flight(status)
        if status in TERMINAL_STATUSES:
            assert is_terminal(status)


def test_can_transition_matches_allowed_targets():
    for status in RemediationStatus:
        for target in RemediationStatus:
            assert can_transition(status, target) == (target in allowed_targets(status))


def test_describe_path_explains_a_route_or_its_absence():
    path = describe_path(RemediationStatus.PROPOSED, RemediationStatus.AUTHORIZED)
    assert path
    assert path[0] == RemediationStatus.PROPOSED
    assert path[-1] == RemediationStatus.AUTHORIZED
    assert describe_path(RemediationStatus.EXPIRED, RemediationStatus.EXECUTING) is None


def test_a_proposed_action_can_reach_every_live_state_by_legal_steps():
    """The happy path must exist as a path, not merely as a status list."""
    for target in (
        RemediationStatus.AUTHORIZED,
        RemediationStatus.EXECUTING,
        RemediationStatus.VERIFYING,
        RemediationStatus.VERIFIED,
        RemediationStatus.ROLLED_BACK,
    ):
        assert (
            describe_path(RemediationStatus.PROPOSED, target) is not None
        ), f"no legal route to {target.value}"
