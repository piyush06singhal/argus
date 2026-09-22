"""Phase 9 — verification and rollback (§28–§33, §62, §64, §107).

The premise of this phase's verification is that *a green execution is not
success*. These tests hold it to that:

* a control that was applied and reads back correctly is ``VERIFIED``;
* an applied effect whose corroborating telemetry never arrives is
  ``PARTIALLY_VERIFIED`` or ``INCONCLUSIVE`` — never silently green;
* an applied effect that made things worse is ``FAILED`` and rolls back;
* a rollback restores the previous state and is itself verified;
* a rollback of something that was never applied is refused, not faked.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.remediation import (
    ExecutionStatus,
    RemediationActionType,
    RemediationExecutionMode,
    RemediationOutcome,
    RemediationStatus,
    RollbackStatus,
    RollbackTrigger,
    VerificationVerdict,
)
from app.services.remediation_clock import utcnow
from app.services.remediation_controls import is_paused
from app.services.remediation_service import RemediationService
from tests.phase9_helpers import (
    build_project,
    degraded_after_apply,
    healthy_then_recovered,
    make_action,
    set_environment_class,
    set_policy,
)


async def _run(
    session,
    *,
    action_type: RemediationActionType = RemediationActionType.PAUSE_BACKGROUND_JOB,
    parameters: dict | None = None,
) -> tuple:
    project, environment, component = await build_project(session)
    await set_environment_class(session, environment, "staging")
    await set_policy(
        session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=[action_type.value],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
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
    if action.status == RemediationStatus.AWAITING_APPROVAL:
        action, _ = await service.decide(
            action, approve=True, actor="operator", reason="approved by test"
        )
    return project, environment, component, action, service


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


async def test_an_applied_control_that_reads_back_verifies(db_session):
    project, environment, component, action, service = await _run(db_session)
    outcome = await service.run(action, actor="test")

    assert action.status == RemediationStatus.VERIFIED
    assert action.outcome in (
        RemediationOutcome.EFFECTIVE,
        RemediationOutcome.PARTIALLY_EFFECTIVE,
    )
    assert outcome["outcome"] in (
        RemediationOutcome.EFFECTIVE.value,
        RemediationOutcome.PARTIALLY_EFFECTIVE.value,
    )
    assert await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_verification_records_every_check_and_its_result(db_session):
    from sqlalchemy import select

    from app.models.remediation import RemediationVerification

    project, environment, component, action, service = await _run(db_session)
    await service.run(action, actor="test")

    row = (
        (
            await db_session.execute(
                select(RemediationVerification)
                .where(RemediationVerification.action_id == action.id)
                .order_by(RemediationVerification.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.checks, "a verdict without its checks is not auditable"
    assert row.passed_count >= 1
    assert row.verdict in (
        VerificationVerdict.VERIFIED,
        VerificationVerdict.PARTIALLY_VERIFIED,
    )


async def test_verification_needs_an_applied_effect(db_session):
    """A dry run has nothing to verify, and must not be scored as a success."""
    project, environment, component, action, service = await _run(db_session)
    outcome = await service.run(action, actor="test", dry_run=True)

    assert action.status == RemediationStatus.AWAITING_APPROVAL
    assert outcome["status"] == RemediationStatus.AWAITING_APPROVAL.value
    assert action.execution_status == ExecutionStatus.NOT_PERFORMED

    from sqlalchemy import select, func

    from app.models.remediation import RemediationVerification

    count = (
        await db_session.execute(
            select(func.count(RemediationVerification.id)).where(
                RemediationVerification.action_id == action.id
            )
        )
    ).scalar()
    assert count == 0, "nothing was applied, so nothing should be verified"


async def test_unobservable_corroborating_checks_do_not_produce_a_clean_pass(
    db_session,
):
    """§28: \"we could not check\" must never render as \"it worked\"."""
    project, environment, component, action, service = await _run(db_session)
    await service.run(action, actor="test")

    from sqlalchemy import select

    from app.models.remediation import RemediationVerification

    row = (
        (
            await db_session.execute(
                select(RemediationVerification)
                .where(RemediationVerification.action_id == action.id)
                .order_by(RemediationVerification.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row.not_observable_count:
        #: With corroborating checks missing, the verdict must say so.
        assert row.verdict in (
            VerificationVerdict.PARTIALLY_VERIFIED,
            VerificationVerdict.INCONCLUSIVE,
        )
        assert row.limitations


async def test_a_failed_check_fails_the_whole_verification(db_session, monkeypatch):
    """One contradiction is enough; verification does not average (§28)."""
    from app.models.remediation import VerificationCheckKind
    from app.services.remediation_verification import VerificationEngine

    project, environment, component, action, service = await _run(db_session)
    executor_outcome = await service.execute(action, actor="test")
    execution = executor_outcome.execution
    assert execution is not None and execution.effect_applied

    original = VerificationEngine._run_check

    async def sabotage(self, action_, execution_, kind, **kwargs):
        result = await original(self, action_, execution_, kind, **kwargs)
        if kind == VerificationCheckKind.CONTROL_STATE:
            result = {
                **result,
                "result": "FAIL",
                "detail": "the control reads back with the wrong state",
            }
        return result

    #: Patched on the class so the service's own engine instance is affected —
    #: the verdict *and* the transition it drives are what is under test.
    monkeypatch.setattr(VerificationEngine, "_run_check", sabotage)
    result = await service.verify(action, execution=execution, now=utcnow())
    assert result.verdict == VerificationVerdict.FAILED
    assert result.failed_count >= 1


async def test_verification_of_a_missing_effect_is_not_executed(db_session):
    from app.services.remediation_verification import VerificationEngine

    project, environment, component, action, service = await _run(db_session)
    result = await VerificationEngine(db_session).verify(action, execution=None)
    assert result.verdict == VerificationVerdict.NOT_EXECUTED
    assert result.confirms_success is False


async def test_degraded_telemetry_after_an_apply_is_detected(db_session):
    """The inverse fixture: if errors rise after a change, ARGUS says so."""
    project, environment, component, action, service = await _run(db_session)
    applied_at = utcnow()
    outcome = await service.execute(action, actor="test")
    assert outcome.execution is not None
    outcome.execution.completed_at = applied_at
    outcome.execution.started_at = applied_at - timedelta(seconds=5)
    action.started_at = applied_at
    action.completed_at = applied_at

    await degraded_after_apply(
        db_session, project, environment, component, applied_at=applied_at
    )
    #: The observation window must be far enough ahead for the samples to land
    #: inside it.
    verified = await service.verify(
        action, execution=outcome.execution, now=applied_at + timedelta(minutes=10)
    )
    assert verified.verdict in (
        VerificationVerdict.FAILED,
        VerificationVerdict.PARTIALLY_VERIFIED,
        VerificationVerdict.INCONCLUSIVE,
    )
    if verified.verdict == VerificationVerdict.FAILED:
        assert action.status == RemediationStatus.ROLLING_BACK


async def test_recovered_telemetry_after_an_apply_corroborates(db_session):
    project, environment, component, action, service = await _run(db_session)
    applied_at = utcnow()
    outcome = await service.execute(action, actor="test")
    assert outcome.execution is not None
    outcome.execution.completed_at = applied_at
    outcome.execution.started_at = applied_at - timedelta(seconds=5)
    action.started_at = applied_at
    action.completed_at = applied_at

    await healthy_then_recovered(
        db_session, project, environment, component, applied_at=applied_at
    )
    result = await service.verify(
        action, execution=outcome.execution, now=applied_at + timedelta(minutes=10)
    )
    assert result.verdict in (
        VerificationVerdict.VERIFIED,
        VerificationVerdict.PARTIALLY_VERIFIED,
    )
    assert result.confirms_success is True


async def test_an_inconclusive_verification_retries_then_escalates(
    db_session, monkeypatch
):
    """Bounded: an action that cannot be confirmed does not sit in VERIFYING."""
    from app.services.remediation_verification import VerificationEngine

    project, environment, component, action, service = await _run(db_session)
    executor_outcome = await service.execute(action, actor="test")
    execution = executor_outcome.execution
    assert execution is not None

    async def always_inconclusive(self, action_, execution_, kind, **kwargs):
        return {
            "check": kind.value,
            "result": "NOT_OBSERVABLE",
            "detail": "no telemetry exists for this check",
            "observed": None,
            "baseline": None,
        }

    monkeypatch.setattr(VerificationEngine, "_run_check", always_inconclusive)

    first = await service.verify(action, execution=execution, now=utcnow())
    assert first.verdict == VerificationVerdict.INCONCLUSIVE
    assert action.status == RemediationStatus.VERIFYING

    #: Retries are bounded by the policy's ``max_verification_attempts``; the
    #: second pass exhausts the allowance and escalates instead of looping.
    second = await service.verify(action, execution=execution, now=utcnow())
    assert second.verdict == VerificationVerdict.INCONCLUSIVE
    assert action.status == RemediationStatus.FAILED
    assert action.outcome == RemediationOutcome.INCONCLUSIVE
    assert "inconclusive" in (action.failure_detail or "").lower()


async def test_execution_alone_is_not_success(db_session):
    """The claim under test: ``SUCCEEDED`` + ``effect_applied`` is not ``VERIFIED``."""
    project, environment, component, action, service = await _run(db_session)
    outcome = await service.execute(action, actor="test")
    assert outcome.status == ExecutionStatus.SUCCEEDED
    assert outcome.effect_applied is True
    #: The action is *verifying*, not verified, until a verdict exists.
    assert action.status == RemediationStatus.VERIFYING
    assert action.outcome is None


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


async def test_a_rollback_restores_the_previous_state(db_session):
    project, environment, component, action, service = await _run(db_session)
    await service.run(action, actor="test")
    assert await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )

    result = await service.rollback(
        action, trigger=RollbackTrigger.HUMAN_REQUEST, requested_by="operator"
    )
    assert result.status == RollbackStatus.SUCCEEDED
    assert action.status == RemediationStatus.ROLLED_BACK
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )


async def test_a_rollback_is_recorded_with_its_own_verification(db_session):
    from sqlalchemy import select

    from app.models.remediation import RemediationRollback

    project, environment, component, action, service = await _run(db_session)
    await service.run(action, actor="test")
    await service.rollback(
        action, trigger=RollbackTrigger.HUMAN_REQUEST, requested_by="operator"
    )

    row = (
        (
            await db_session.execute(
                select(RemediationRollback).where(
                    RemediationRollback.action_id == action.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.trigger == RollbackTrigger.HUMAN_REQUEST
    assert row.status == RollbackStatus.SUCCEEDED
    assert row.completed_at is not None


async def test_rolling_back_something_never_applied_is_refused(db_session):
    """A rollback is not a status change; it reverses an effect that exists."""
    project, environment, component, action, service = await _run(db_session)
    with pytest.raises(ValueError):
        await service.rollback(
            action, trigger=RollbackTrigger.HUMAN_REQUEST, requested_by="operator"
        )


async def test_an_irreversible_action_reports_that_it_cannot_be_reverted(db_session):
    """The refusal is a value with a reason, not an exception to swallow (§32)."""
    from app.services.remediation_executor import build_adapter

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        action_type=RemediationActionType.RESTART_SERVICE,
    )
    adapter = build_adapter(db_session, action.action_type)
    from app.models.remediation import RemediationExecution

    execution = RemediationExecution(
        action_id=action.id,
        project_id=action.project_id,
        action_type=action.action_type,
        mode=RemediationExecutionMode.HUMAN_APPROVAL,
        adapter_kind=adapter.kind,
        adapter_name=adapter.name,
        attempt=1,
        status=ExecutionStatus.SUCCEEDED,
        effect_applied=True,
        idempotency_key="fixture",
    )
    result = await adapter.revert(action, execution)
    assert result.effect_applied is False
    assert result.status == ExecutionStatus.REFUSED


async def test_rollback_is_not_available_for_an_irreversible_action(db_session):
    from app.models.remediation import RollbackStrategy
    from app.services.remediation_registry import get_definition

    definition = get_definition(RemediationActionType.RESTART_SERVICE)
    assert definition.reversible is False
    assert definition.rollback_strategy == RollbackStrategy.NONE


async def test_a_failed_verification_rolls_back_automatically(db_session, monkeypatch):
    """§30: a harmful change is undone rather than left in place."""
    from app.models.remediation import VerificationCheckKind
    from app.services.remediation_verification import VerificationEngine

    project, environment, component, action, service = await _run(db_session)
    executor_outcome = await service.execute(action, actor="test")
    execution = executor_outcome.execution
    assert execution is not None

    original = VerificationEngine._run_check

    async def sabotage(self, action_, execution_, kind, **kwargs):
        result = await original(self, action_, execution_, kind, **kwargs)
        if kind == VerificationCheckKind.CONTROL_STATE:
            result = {**result, "result": "FAIL", "detail": "the control is wrong"}
        return result

    monkeypatch.setattr(VerificationEngine, "_run_check", sabotage)
    await service.verify(action, execution=execution, now=utcnow())
    assert action.status == RemediationStatus.ROLLING_BACK

    result = await service.rollback(
        action,
        trigger=RollbackTrigger.VERIFICATION_FAILED,
        requested_by="system",
    )
    assert result.status == RollbackStatus.SUCCEEDED
    assert action.status == RemediationStatus.ROLLED_BACK
    assert action.outcome == RemediationOutcome.HARMFUL
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )


async def test_post_analysis_is_recorded_after_every_outcome(db_session):
    """§36/§37: what actually happened, from stored rows, including the limits."""
    project, environment, component, action, service = await _run(db_session)
    await service.run(action, actor="test")
    assert action.post_analysis is not None
    assert action.post_analysis["verdict"] is not None
    assert "limitations" in action.post_analysis
    assert "not proof" in action.post_analysis["note"]


async def test_post_analysis_admits_missing_telemetry(db_session):
    """An unconfirmed improvement is stated as unconfirmed."""
    project, environment, component, action, service = await _run(db_session)
    await service.run(action, actor="test")
    analysis = action.post_analysis
    assert analysis is not None
    #: No telemetry was written by this fixture, so the analysis must say so
    #: rather than implying the action was confirmed to help.
    assert analysis["health_after"]["total"] == 0
    assert analysis["error_rate_after"]["samples"] == 0
    assert any("unconfirmed" in limit for limit in analysis["limitations"])
