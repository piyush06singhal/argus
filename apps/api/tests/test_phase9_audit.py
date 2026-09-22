"""Phase 9 — the audit trail (§12, §35, §44, §57).

The audit trail is not a log. It is a hash chain whose purpose is that a
*deletion or an edit is detectable*, so the tests attack it: they remove an
event, they rewrite a field, and they check that the chain reports where it
broke. A trail that only recorded happy paths would pass a weaker test suite and
fail its actual job.

The other half is completeness: every gate — including every refusal — has to
leave an event, because \"why did ARGUS do that?\" is mostly asked about the things
it declined to do.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from app.models.remediation import (
    RemediationAuditEvent,
    RemediationExecutionMode,
    RemediationStatus,
    RollbackTrigger,
)
from app.services.remediation_audit import AuditTrail, compute_hash
from app.services.remediation_clock import utcnow
from app.services.remediation_service import RemediationService
from tests.phase9_helpers import (
    build_project,
    make_action,
    set_environment_class,
    set_policy,
)


async def _action_with_history(session, *, approve: bool = True):
    project, environment, component = await build_project(session)
    await set_environment_class(session, environment, "staging")
    await set_policy(
        session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(session, project, environment, component)
    service = RemediationService(session)
    await service.assess(action)
    await service.evaluate_policy(action)
    if approve and action.status == RemediationStatus.AWAITING_APPROVAL:
        action, _ = await service.decide(
            action, approve=True, actor="operator", reason="approved by test"
        )
    return project, environment, component, action, service


async def test_the_chain_verifies_after_a_full_run(db_session):
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.run(action, actor="test")

    chain = await AuditTrail(db_session).verify_chain(action.id)
    assert chain["intact"] is True
    assert chain["events"] >= 5


async def test_every_gate_leaves_an_event(db_session):
    """Proposal, safety, policy, approval, authorization, execution, verdict."""
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.run(action, actor="test")
    events = await AuditTrail(db_session).history(action.id)
    types = {event.event_type.value for event in events}

    assert "ACTION_PROPOSED" in types
    assert "SAFETY_ASSESSED" in types
    assert "POLICY_EVALUATED" in types
    assert "APPROVED" in types
    assert "EXECUTION_STARTED" in types
    assert "EXECUTION_SUCCEEDED" in types
    assert "VERIFICATION_COMPLETED" in types


async def test_a_refusal_leaves_an_event_too(db_session):
    """The interesting audit question is usually about a refusal (§35)."""
    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session, project, environment, component, parameters={"job": "nope"}
    )
    service = RemediationService(db_session)
    await service.assess(action)
    assert action.status == RemediationStatus.BLOCKED

    events = await AuditTrail(db_session).history(action.id)
    types = {event.event_type.value for event in events}
    assert "VALIDATION_FAILED" in types
    #: The refusal carries the transition it caused, so the block is visible in
    #: the chain rather than only on the action row.
    refusal = next(e for e in events if e.event_type.value == "VALIDATION_FAILED")
    assert refusal.to_status == RemediationStatus.BLOCKED
    assert refusal.detail and refusal.detail.get("errors")


async def test_removing_an_event_breaks_the_chain_detectably(db_session):
    """The reason the chain exists: a deleted row must not look like a clean log."""
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.run(action, actor="test")
    trail = AuditTrail(db_session)
    events = await trail.history(action.id)
    assert len(events) >= 4

    #: Drop the middle event and keep the (now renumbered) remainder.
    victim = events[2]
    await db_session.execute(
        delete(RemediationAuditEvent).where(RemediationAuditEvent.id == victim.id)
    )
    await db_session.flush()

    chain = await trail.verify_chain(action.id)
    assert chain["intact"] is False
    assert chain["broken_at"] is not None
    assert chain["reason"]


async def test_editing_an_event_breaks_the_chain_detectably(db_session):
    """An edit to the stored content must not survive verification."""
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.run(action, actor="test")
    trail = AuditTrail(db_session)
    events = await trail.history(action.id)
    victim = events[1]

    await db_session.execute(
        update(RemediationAuditEvent)
        .where(RemediationAuditEvent.id == victim.id)
        .values(summary="a summary that was never written by ARGUS")
    )
    await db_session.flush()

    chain = await trail.verify_chain(action.id)
    assert chain["intact"] is False
    assert chain["broken_at"] == victim.sequence
    assert "digest" in chain["reason"]


async def test_the_chain_is_ordered_and_linked(db_session):
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.run(action, actor="test")
    events = await AuditTrail(db_session).history(action.id)

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    previous = None
    for event in events:
        assert event.prev_hash == previous
        previous = event.entry_hash


async def test_the_digest_covers_the_action_scope(db_session):
    """Two identical events for different actions must not share a digest."""
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    other_project, other_environment, other_component = await build_project(
        db_session, name="Audit Other"
    )
    payload = {"sequence": 1, "summary": "same"}
    first = compute_hash({**payload, "action_id": str(action.id)})
    second = compute_hash({**payload, "action_id": str(other_component.id)})
    assert first != second


async def test_project_level_events_are_recorded_without_an_action(db_session):
    """The emergency stop is recorded even though no action owns it."""
    project, environment, component = await build_project(db_session)
    service = RemediationService(db_session)
    await service.engage_emergency_stop(
        project.id, engage=True, actor="operator", reason="incident in progress"
    )
    rows = (
        (
            await db_session.execute(
                select(RemediationAuditEvent).where(
                    RemediationAuditEvent.project_id == project.id,
                    RemediationAuditEvent.action_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows
    assert rows[0].event_type.value == "EMERGENCY_STOP_ENGAGED"


async def test_the_emergency_stop_blocks_every_open_action(db_session):
    """§34: a stop that leaves authorized work queued is not a stop."""
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    assert action.status == RemediationStatus.AUTHORIZED

    await service.engage_emergency_stop(
        project.id, engage=True, actor="operator", reason="stop everything"
    )
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason is not None

    #: The block is visible in the action's own trail, not only at project level.
    events = await AuditTrail(db_session).history(action.id)
    assert any(event.event_type.value == "ACTION_BLOCKED" for event in events)


async def test_releasing_the_emergency_stop_authorizes_nothing_by_itself(db_session):
    """It removes the block; each action must clear its gates again."""
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.engage_emergency_stop(
        project.id, engage=True, actor="operator", reason="stop"
    )
    await service.engage_emergency_stop(
        project.id, engage=False, actor="operator", reason="resume"
    )
    assert action.status == RemediationStatus.BLOCKED

    from app.services.remediation_policy import resolve_policy

    policy = await resolve_policy(db_session, project.id, environment.id)
    assert policy.emergency_stop_active is False


async def test_audit_events_are_append_only_by_convention(db_session):
    """Nothing in the service mutates or deletes an audit row."""
    import pathlib

    source = pathlib.Path("app/services/remediation_audit.py").read_text()
    assert "delete(" not in source, "the audit trail must not delete events"
    assert ".update(" not in source, "the audit trail must not rewrite events"


async def test_action_expiry_is_audited(db_session):
    """A stale approval is recorded as an expiry, not silently dropped."""
    project, environment, component, action, service = await _action_with_history(
        db_session, approve=False
    )
    assert action.status == RemediationStatus.AWAITING_APPROVAL
    from app.services.remediation_service import RemediationService as Service

    approval = await Service(db_session).pending_approval(action.id)
    assert approval is not None
    approval.expires_at = utcnow() - timedelta(seconds=1)

    action, evaluation = await Service(db_session).decide(
        action, approve=True, actor="late-approver", reason="too late"
    )
    assert action.status == RemediationStatus.EXPIRED
    assert evaluation is None

    events = await AuditTrail(db_session).history(action.id)
    assert any(event.event_type.value == "ACTION_EXPIRED" for event in events)


async def test_the_approval_scope_snapshot_is_frozen(db_session):
    """§22: what the approver saw is stored, so drift is visible later."""
    project, environment, component, action, service = await _action_with_history(
        db_session, approve=False
    )
    service = RemediationService(db_session)
    approval = await service.pending_approval(action.id)
    assert approval is not None
    snapshot = approval.scope_snapshot
    assert snapshot is not None
    assert snapshot["action_type"] == action.action_type.value
    assert "verification_plan" in snapshot
    assert snapshot["blast_radius"] == action.blast_radius.value


async def test_a_rejection_is_terminal_and_recorded(db_session):
    project, environment, component, action, service = await _action_with_history(
        db_session, approve=False
    )
    action, evaluation = await service.decide(
        action, approve=False, actor="operator", reason="not the right fix"
    )
    assert action.status == RemediationStatus.REJECTED
    assert evaluation is None

    events = await AuditTrail(db_session).history(action.id)
    rejection = [event for event in events if event.event_type.value == "REJECTED"]
    assert rejection
    assert "not the right fix" in rejection[0].summary

    #: And it cannot be revived.
    from app.services.remediation_state import allowed_targets

    assert allowed_targets(action.status) == frozenset({action.status})


async def test_an_approval_requires_a_named_actor(db_session):
    """§1: no anonymous approvals, and no approvals by an AI proposal."""
    project, environment, component, action, service = await _action_with_history(
        db_session, approve=False
    )
    with pytest.raises(ValueError):
        await service.decide(action, approve=True, actor="   ", reason="nobody")
    assert action.status == RemediationStatus.AWAITING_APPROVAL


async def test_the_authorizing_actor_is_recorded_on_the_action(db_session):
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    assert action.approved_by is not None
    assert action.authorized_by is not None
    assert action.authorized_at is not None


async def test_a_policy_denial_is_audited_with_its_rule(db_session):
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["DISABLE_FEATURE_FLAG"],
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.REJECTED

    events = await AuditTrail(db_session).history(action.id)
    denied = [event for event in events if event.event_type.value == "POLICY_DENIED"]
    assert denied
    assert any("allow-list" in event.summary for event in denied)


async def test_control_application_and_reversal_are_audited(db_session):
    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    await service.run(action, actor="test")
    await service.rollback(
        action, trigger=RollbackTrigger.HUMAN_REQUEST, requested_by="operator"
    )
    events = await AuditTrail(db_session).history(action.id)
    types = [event.event_type.value for event in events]
    assert "ROLLBACK_STARTED" in types
    assert "ROLLBACK_COMPLETED" in types
    #: The rollback events join the same chain, so verification still holds.
    assert (await AuditTrail(db_session).verify_chain(action.id))["intact"] is True


async def test_audit_events_carry_the_actor_type(db_session):
    """\"Who did this\" must distinguish a person from an autonomous rule."""
    from app.models.remediation import RemediationActorType

    project, environment, component, action, service = await _action_with_history(
        db_session
    )
    events = await AuditTrail(db_session).history(action.id)
    actor_types = {event.actor_type for event in events}
    assert RemediationActorType.HUMAN in actor_types
