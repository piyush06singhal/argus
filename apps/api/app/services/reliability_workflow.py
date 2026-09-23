"""ARGUS Reliability Workflow Engine (Phase 11 §11–§13).

A small, provider-neutral engine that drives one situation through the stages
the phase names, and — more importantly — *stops* when it should.

The lifecycle (§11):

    DETECTED → TRIAGED → ANALYZING → DIAGNOSED → REMEDIATION_READY
             → AUTHORIZED → EXECUTING → VERIFYING → RESOLVED → LEARNED

Not every situation needs every stage: something that resolves on its own goes
DETECTED → RESOLVED → LEARNED, and the engine records the stages it *skipped* so
absence is visible rather than ambiguous.

What this engine deliberately is not:

* **not a general workflow framework.** No arbitrary graphs, no user-authored
  step definitions, no expression language. The stages are a closed enum and the
  transitions are in this file. A framework would add a dependency, a second
  configuration surface and a class of bugs (`YAML that does something`) in
  exchange for flexibility the phase does not ask for.
* **not a scheduler.** It has a ``next_run_at`` and a deadline; the sweep wakes
  it. There are no busy loops and no in-process timers.
* **not an executor.** The EXECUTING and VERIFYING stages call Phase 9, which
  re-applies its own policy, safety and approval gates. The workflow cannot
  authorize anything: ``AUTHORIZED`` records that *Phase 9* authorized an action,
  never that the workflow decided to.

§13's safety stops are the load-bearing part of this module. Each one re-checks a
precondition and halts with a recorded reason rather than continuing to act on a
situation that has changed underneath the run:

* authorization expired, evidence stale, incident gone, material state change,
  policy change, kill switch, failed verification.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.platform import (
    CaseStatus,
    ReliabilityCase,
    ReliabilityWorkflow,
    WorkflowStage,
    WorkflowStatus,
    WorkflowStopReason,
)

logger = logging.getLogger(__name__)

#: The legal stage progression (§11). ``None`` means "any stage can be reached
#: from here" (the failure paths), which is why failure is not a stage: a run
#: that fails keeps its stage and records a stop reason.
STAGE_ORDER: tuple[WorkflowStage, ...] = (
    WorkflowStage.DETECTED,
    WorkflowStage.TRIAGED,
    WorkflowStage.ANALYZING,
    WorkflowStage.DIAGNOSED,
    WorkflowStage.REMEDIATION_READY,
    WorkflowStage.AUTHORIZED,
    WorkflowStage.EXECUTING,
    WorkflowStage.VERIFYING,
    WorkflowStage.RESOLVED,
    WorkflowStage.LEARNED,
)

#: Which stages a run may legally move to next.
STAGE_TRANSITIONS: dict[WorkflowStage, tuple[WorkflowStage, ...]] = {
    WorkflowStage.DETECTED: (
        WorkflowStage.TRIAGED,
        WorkflowStage.ANALYZING,
        WorkflowStage.RESOLVED,
    ),
    WorkflowStage.TRIAGED: (WorkflowStage.ANALYZING, WorkflowStage.RESOLVED),
    WorkflowStage.ANALYZING: (WorkflowStage.DIAGNOSED, WorkflowStage.RESOLVED),
    WorkflowStage.DIAGNOSED: (WorkflowStage.REMEDIATION_READY, WorkflowStage.RESOLVED),
    WorkflowStage.REMEDIATION_READY: (WorkflowStage.AUTHORIZED, WorkflowStage.RESOLVED),
    WorkflowStage.AUTHORIZED: (WorkflowStage.EXECUTING, WorkflowStage.RESOLVED),
    WorkflowStage.EXECUTING: (WorkflowStage.VERIFYING, WorkflowStage.DIAGNOSED),
    WorkflowStage.VERIFYING: (WorkflowStage.RESOLVED, WorkflowStage.EXECUTING),
    WorkflowStage.RESOLVED: (WorkflowStage.LEARNED,),
    WorkflowStage.LEARNED: (),
}

TERMINAL_WORKFLOW_STATUSES = (
    WorkflowStatus.COMPLETED,
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
    WorkflowStatus.TIMED_OUT,
)

#: The case status each stage implies, so the two objects cannot drift: moving a
#: workflow to a stage moves its case to the matching status.
STAGE_CASE_STATUS: dict[WorkflowStage, CaseStatus] = {
    WorkflowStage.DETECTED: CaseStatus.OPEN,
    WorkflowStage.TRIAGED: CaseStatus.TRIAGED,
    WorkflowStage.ANALYZING: CaseStatus.ANALYZING,
    WorkflowStage.DIAGNOSED: CaseStatus.DIAGNOSED,
    WorkflowStage.REMEDIATION_READY: CaseStatus.REMEDIATION_READY,
    WorkflowStage.AUTHORIZED: CaseStatus.AUTHORIZED,
    WorkflowStage.EXECUTING: CaseStatus.EXECUTING,
    WorkflowStage.VERIFYING: CaseStatus.VERIFYING,
    WorkflowStage.RESOLVED: CaseStatus.RESOLVED,
    WorkflowStage.LEARNED: CaseStatus.LEARNED,
}


class WorkflowError(ValueError):
    """An illegal workflow operation."""


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def stage_index(stage: WorkflowStage) -> int:
    return STAGE_ORDER.index(stage)


def can_advance(current: WorkflowStage, target: WorkflowStage) -> bool:
    """Whether a run may move from ``current`` to ``target``."""
    if current == target:
        return False
    return target in STAGE_TRANSITIONS.get(current, ())


@dataclass
class StopCheck:
    """The verdict of a §13 precondition check."""

    reason: Optional[WorkflowStopReason] = None
    detail: Optional[str] = None

    @property
    def should_stop(self) -> bool:
        return self.reason is not None


async def check_preconditions(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    case: Optional[ReliabilityCase],
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> StopCheck:
    """Evaluate every §13 stop condition for a run.

    Returns the *first* condition that has failed. Every check is a statement
    about stored rows, not a heuristic: an expired deadline, a case that is
    cancelled, an incident that is gone. Nothing here guesses.
    """
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)

    if workflow.status in TERMINAL_WORKFLOW_STATUSES:
        return StopCheck()

    # 1. deadline
    deadline = _aware(workflow.deadline_at)
    if deadline is not None and moment >= deadline:
        return StopCheck(
            reason=WorkflowStopReason.TIMED_OUT,
            detail=f"workflow deadline {deadline.isoformat()} passed",
        )

    # 2. the case itself
    if case is None:
        return StopCheck(
            reason=WorkflowStopReason.EVIDENCE_STALE,
            detail="the workflow's case no longer exists",
        )
    if case.status == CaseStatus.CANCELLED:
        return StopCheck(
            reason=WorkflowStopReason.CANCELLED,
            detail="the case was cancelled",
        )

    # 3. evidence staleness: the context the run was started with has aged out
    started = _aware(workflow.started_at) or _aware(workflow.created_at)
    stale_after = timedelta(seconds=settings.PLATFORM_WORKFLOW_EVIDENCE_MAX_AGE_SECONDS)
    if started is not None and moment - started > stale_after:
        return StopCheck(
            reason=WorkflowStopReason.EVIDENCE_STALE,
            detail=(
                f"the run's evidence is older than "
                f"{int(stale_after.total_seconds())}s"
            ),
        )

    # 4. the incident it is about
    if case.incident_id is not None:
        from app.models.incident import Incident

        incident = await session.get(Incident, case.incident_id)
        if incident is None:
            return StopCheck(
                reason=WorkflowStopReason.INCIDENT_GONE,
                detail="the incident this case is about was deleted",
            )
        incident_status = getattr(incident.status, "value", str(incident.status))
        if incident_status in ("RESOLVED", "CLOSED") and stage_index(
            workflow.stage
        ) < stage_index(WorkflowStage.RESOLVED):
            #: Not a failure: the situation ended before ARGUS had to act. The
            #: run stops at RESOLVED rather than continuing to remediate
            #: something that is no longer broken.
            return StopCheck(
                reason=WorkflowStopReason.STATE_CHANGED,
                detail=(
                    f"the incident is {incident_status}; nothing further needs to "
                    "be done"
                ),
            )

    # 5. emergency stop / kill switch (Phase 9 owns the control plane)
    try:
        from app.services.remediation_controls import is_paused

        if await is_paused(
            session, "remediation_sweep", project_id=workflow.project_id
        ):
            return StopCheck(
                reason=WorkflowStopReason.KILL_SWITCH,
                detail="remediation is paused by the ARGUS control plane",
            )
    except Exception:  # pragma: no cover - a control lookup must not break the run
        logger.warning("workflow kill-switch check failed", exc_info=True)

    return StopCheck()


async def start_workflow(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    case: Optional[ReliabilityCase] = None,
    context: Optional[dict[str, Any]] = None,
    stage: WorkflowStage = WorkflowStage.DETECTED,
    triggered_by: Optional[str] = None,
    deadline_seconds: Optional[int] = None,
    max_attempts: int = 1,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> ReliabilityWorkflow:
    """Start a run for a case (or for a project-level situation)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    workflow = ReliabilityWorkflow(
        project_id=project_id,
        case_id=case.id if case else None,
        stage=stage,
        status=WorkflowStatus.RUNNING,
        completed_stages=[],
        context=context or ({**case_context(case)} if case else None),
        attempt=0,
        max_attempts=max(1, max_attempts),
        started_at=moment,
        next_run_at=moment,
        deadline_at=moment
        + timedelta(
            seconds=deadline_seconds or settings.PLATFORM_WORKFLOW_DEADLINE_SECONDS
        ),
        triggered_by=triggered_by,
    )
    session.add(workflow)
    await session.flush()
    if case is not None:
        await record_stage(
            session,
            workflow=workflow,
            case=case,
            target=stage,
            actor=triggered_by,
            reason="workflow started",
            occurred_at=moment,
            allow_same=True,
        )
    return workflow


def case_context(case: Optional[ReliabilityCase]) -> dict[str, Any]:
    """The §7 references a case carries into a workflow context."""
    if case is None:
        return {}
    return {
        "case_id": str(case.id),
        "reference": case.reference,
        "project_id": str(case.project_id),
        "environment_id": str(case.environment_id) if case.environment_id else None,
        "incident_id": str(case.incident_id) if case.incident_id else None,
        "primary_component_id": str(case.primary_component_id)
        if case.primary_component_id
        else None,
        "component_ids": list(case.component_ids or []),
    }


async def get_workflow(
    session: AsyncSession,
    *,
    workflow_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
) -> Optional[ReliabilityWorkflow]:
    row = await session.get(ReliabilityWorkflow, workflow_id)
    if row is None:
        return None
    if project_id is not None and row.project_id != project_id:
        return None
    return row


async def active_workflow_for_case(
    session: AsyncSession, *, case_id: uuid.UUID
) -> Optional[ReliabilityWorkflow]:
    stmt = (
        select(ReliabilityWorkflow)
        .where(
            ReliabilityWorkflow.case_id == case_id,
            ReliabilityWorkflow.status.notin_(list(TERMINAL_WORKFLOW_STATUSES)),
        )
        .order_by(ReliabilityWorkflow.created_at.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def record_stage(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    case: Optional[ReliabilityCase],
    target: WorkflowStage,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    enforce: bool = True,
    allow_same: bool = False,
) -> ReliabilityWorkflow:
    """Move a run to a stage, mirroring the case status (§11–§12).

    ``enforce=False`` is used when the engine is reflecting a stage that already
    happened elsewhere (a case moving because Phase 9 authorized an action);
    skipping a stage is allowed there and is recorded as a skip.
    """
    moment = _aware(occurred_at) or datetime.now(timezone.utc)
    if workflow.stage == target:
        if not allow_same:
            return workflow
    elif enforce and not can_advance(workflow.stage, target):
        raise WorkflowError(
            f"illegal workflow transition: {workflow.stage.value} -> {target.value}"
        )

    previous = workflow.stage
    skipped: list[str] = []
    if stage_index(target) > stage_index(previous) + 1:
        #: Record what was skipped rather than implying it happened.
        skipped = [
            stage.value
            for stage in STAGE_ORDER[stage_index(previous) + 1 : stage_index(target)]
        ]
    workflow.stage = target
    completed = list(workflow.completed_stages or [])
    if previous.value not in completed:
        completed.append(previous.value)
    if target.value not in completed:
        completed.append(target.value)
    workflow.completed_stages = completed
    workflow.state = {
        **(workflow.state or {}),
        "last_stage_change": moment.isoformat(),
        "last_skipped": skipped,
        "last_reason": reason,
    }
    if target == WorkflowStage.LEARNED:
        workflow.status = WorkflowStatus.COMPLETED
        workflow.completed_at = moment
    await session.flush()

    if case is not None:
        from app.services.reliability_case import CaseStateError, transition_case

        desired = STAGE_CASE_STATUS.get(target)
        if desired is not None and case.status != desired:
            try:
                await transition_case(
                    session,
                    case=case,
                    target=desired,
                    actor=actor,
                    reason=reason or f"workflow advanced to {target.value}",
                    evidence={
                        "stage": target.value,
                        "skipped": skipped,
                        **(evidence or {}),
                    },
                    occurred_at=moment,
                    enforce=enforce,
                )
            except CaseStateError:
                #: The case lifecycle is narrower than the workflow's in some
                #: failure paths. The workflow records its own move and the case
                #: keeps its status — the divergence is visible, never silent.
                logger.info(
                    "case %s stayed at %s while workflow moved to %s",
                    case.reference,
                    case.status.value,
                    target.value,
                )
        from app.services.reliability_case import append_timeline
        from app.models.platform import TimelineEntryKind

        await append_timeline(
            session,
            case=case,
            kind=(
                TimelineEntryKind.LEARNING
                if target == WorkflowStage.LEARNED
                else TimelineEntryKind.STATE_CHANGE
            ),
            event_type="WORKFLOW_STAGE_CHANGED",
            title=f"Workflow advanced to {target.value}",
            detail=reason,
            source="reliability_workflow",
            evidence={
                "previous_stage": previous.value,
                "skipped": skipped,
                **(evidence or {}),
            },
            actor=actor,
            system_action=actor is None,
            result=target.value,
            occurred_at=moment,
            dedup_key=f"stage:{previous.value}->{target.value}:{moment.isoformat()}",
        )
    return workflow


async def stop_workflow(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    reason: WorkflowStopReason,
    detail: Optional[str] = None,
    case: Optional[ReliabilityCase] = None,
    status: Optional[WorkflowStatus] = None,
    occurred_at: Optional[datetime] = None,
) -> ReliabilityWorkflow:
    """Halt a run with its reason recorded (§13).

    A stop is not a failure: ``KILL_SWITCH``, ``EVIDENCE_STALE`` and
    ``STATE_CHANGED`` all mean the platform correctly declined to continue.
    Only ``ERROR`` sets ``last_error``.
    """
    moment = _aware(occurred_at) or datetime.now(timezone.utc)
    workflow.stop_reason = reason
    workflow.stop_detail = detail
    workflow.completed_at = moment
    if status is None:
        status = (
            WorkflowStatus.TIMED_OUT
            if reason in (WorkflowStopReason.TIMED_OUT,)
            else WorkflowStatus.FAILED
            if reason == WorkflowStopReason.ERROR
            else WorkflowStatus.CANCELLED
            if reason == WorkflowStopReason.CANCELLED
            else WorkflowStatus.BLOCKED
        )
    workflow.status = status
    workflow.next_run_at = None
    if reason == WorkflowStopReason.ERROR and detail:
        workflow.last_error = detail
    await session.flush()

    if case is not None:
        from app.models.platform import TimelineEntryKind
        from app.services.reliability_case import append_timeline

        await append_timeline(
            session,
            case=case,
            kind=TimelineEntryKind.STATE_CHANGE,
            event_type="WORKFLOW_STOPPED",
            title=f"Workflow stopped: {reason.value}",
            detail=detail,
            source="reliability_workflow",
            evidence={"stage": workflow.stage.value, "reason": reason.value},
            system_action=True,
            result=reason.value,
            occurred_at=moment,
            dedup_key=f"stop:{reason.value}:{moment.isoformat()}",
        )
    return workflow


@dataclass
class WorkflowTick:
    """What one pass of the engine did for one run."""

    workflow_id: uuid.UUID
    action: str
    stage: Optional[str] = None
    stop_reason: Optional[str] = None
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": str(self.workflow_id),
            "action": self.action,
            "stage": self.stage,
            "stop_reason": self.stop_reason,
            "detail": self.detail,
        }


async def advance(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    target: WorkflowStage,
    case: Optional[ReliabilityCase] = None,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    enforce: bool = True,
) -> WorkflowTick:
    """Check the §13 preconditions, then advance — or stop with the reason."""
    moment = _aware(now) or datetime.now(timezone.utc)
    if workflow.status in TERMINAL_WORKFLOW_STATUSES:
        #: A finished run is finished. ``check_preconditions`` reports "no
        #: precondition violated" for a terminal run — which is true, and which
        #: must not be read as permission to continue, or a stopped workflow
        #: would be resurrectable by the next tick.
        return WorkflowTick(
            workflow_id=workflow.id,
            action="already_finished",
            stage=workflow.stage.value,
            stop_reason=(workflow.stop_reason.value if workflow.stop_reason else None),
            detail=(
                f"the run is {workflow.status.value} and cannot be advanced; "
                "resume or start a new run instead"
            ),
        )
    check = await check_preconditions(session, workflow=workflow, case=case, now=moment)
    if check.should_stop:
        if (
            check.reason == WorkflowStopReason.STATE_CHANGED
            and target != WorkflowStage.LEARNED
        ):
            #: The situation ended on its own: land the run where it belongs
            #: instead of halting it in a frozen mid-stage.
            await record_stage(
                session,
                workflow=workflow,
                case=case,
                target=WorkflowStage.RESOLVED,
                actor=actor,
                reason=check.detail,
                evidence=evidence,
                occurred_at=moment,
                enforce=False,
            )
            return WorkflowTick(
                workflow_id=workflow.id,
                action="resolved_externally",
                stage=workflow.stage.value,
                detail=check.detail,
            )
        status = (
            WorkflowStatus.TIMED_OUT
            if check.reason == WorkflowStopReason.TIMED_OUT
            else WorkflowStatus.CANCELLED
            if check.reason
            in (WorkflowStopReason.CANCELLED, WorkflowStopReason.INCIDENT_GONE)
            else WorkflowStatus.BLOCKED
        )
        await stop_workflow(
            session,
            workflow=workflow,
            reason=check.reason or WorkflowStopReason.ERROR,
            detail=check.detail,
            case=case,
            status=status,
            occurred_at=moment,
        )
        return WorkflowTick(
            workflow_id=workflow.id,
            action="stopped",
            stage=workflow.stage.value,
            stop_reason=workflow.stop_reason.value if workflow.stop_reason else None,
            detail=check.detail,
        )

    await record_stage(
        session,
        workflow=workflow,
        case=case,
        target=target,
        actor=actor,
        reason=reason,
        evidence=evidence,
        occurred_at=moment,
        enforce=enforce,
    )
    return WorkflowTick(
        workflow_id=workflow.id,
        action="advanced",
        stage=workflow.stage.value,
        detail=reason,
    )


async def record_failure(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    detail: str,
    case: Optional[ReliabilityCase] = None,
    retryable: bool = True,
    now: Optional[datetime] = None,
) -> WorkflowTick:
    """Record an attempt failure, retrying while attempts remain (§12)."""
    moment = _aware(now) or datetime.now(timezone.utc)
    workflow.attempt += 1
    workflow.last_error = detail
    if retryable and workflow.attempt < workflow.max_attempts:
        workflow.next_run_at = moment + timedelta(
            seconds=min(300, 15 * (2 ** (workflow.attempt - 1)))
        )
        workflow.status = WorkflowStatus.RUNNING
        await session.flush()
        return WorkflowTick(
            workflow_id=workflow.id,
            action="retried",
            stage=workflow.stage.value,
            detail=f"attempt {workflow.attempt}/{workflow.max_attempts}: {detail}",
        )
    await stop_workflow(
        session,
        workflow=workflow,
        reason=WorkflowStopReason.ERROR,
        detail=detail,
        case=case,
        status=WorkflowStatus.FAILED,
        occurred_at=moment,
    )
    return WorkflowTick(
        workflow_id=workflow.id,
        action="failed",
        stage=workflow.stage.value,
        stop_reason=WorkflowStopReason.ERROR.value,
        detail=detail,
    )


async def wait_for_approval(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    case: Optional[ReliabilityCase] = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> ReliabilityWorkflow:
    """Park a run at AUTHORIZED-awaiting-approval (§12 human approval).

    The workflow does not *ask* for approval — Phase 9 owns the approval record —
    it simply records that it is waiting, and the approval itself is a Phase 9 row
    that policy reads.
    """
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    workflow.status = WorkflowStatus.WAITING_APPROVAL
    workflow.next_run_at = moment + timedelta(
        seconds=settings.PLATFORM_WORKFLOW_APPROVAL_POLL_SECONDS
    )
    workflow.state = {
        **(workflow.state or {}),
        "waiting_for": "approval",
        "waiting_since": moment.isoformat(),
        "waiting_reason": reason,
    }
    await session.flush()
    return workflow


async def resume(
    session: AsyncSession,
    *,
    workflow: ReliabilityWorkflow,
    case: Optional[ReliabilityCase] = None,
    now: Optional[datetime] = None,
) -> WorkflowTick:
    """Wake a parked run whose reason for waiting has been cleared (§12).

    Only a *parked* run can be woken. Resuming a timed-out or cancelled run would
    be restarting it, which is a new decision and not this function's to make.
    """
    moment = _aware(now) or datetime.now(timezone.utc)
    if workflow.status in TERMINAL_WORKFLOW_STATUSES:
        return WorkflowTick(
            workflow_id=workflow.id,
            action="already_finished",
            stage=workflow.stage.value,
            stop_reason=(workflow.stop_reason.value if workflow.stop_reason else None),
            detail=(
                f"the run is {workflow.status.value}; a finished run is not resumed"
            ),
        )
    check = await check_preconditions(session, workflow=workflow, case=case, now=moment)
    if check.should_stop:
        await stop_workflow(
            session,
            workflow=workflow,
            reason=check.reason or WorkflowStopReason.ERROR,
            detail=check.detail,
            case=case,
            occurred_at=moment,
        )
        return WorkflowTick(
            workflow_id=workflow.id,
            action="stopped",
            stage=workflow.stage.value,
            stop_reason=workflow.stop_reason.value if workflow.stop_reason else None,
            detail=check.detail,
        )
    workflow.status = WorkflowStatus.RUNNING
    workflow.next_run_at = moment
    workflow.state = {**(workflow.state or {}), "waiting_for": None}
    await session.flush()
    return WorkflowTick(
        workflow_id=workflow.id, action="resumed", stage=workflow.stage.value
    )


async def claim_due(
    session: AsyncSession, *, limit: int = 50, now: Optional[datetime] = None
) -> list[ReliabilityWorkflow]:
    """Runs that are due for a pass (bounded, oldest first)."""
    moment = _aware(now) or datetime.now(timezone.utc)
    stmt = (
        select(ReliabilityWorkflow)
        .where(
            ReliabilityWorkflow.status.in_(
                [WorkflowStatus.RUNNING, WorkflowStatus.WAITING_APPROVAL]
            ),
            ReliabilityWorkflow.next_run_at.is_not(None),
            ReliabilityWorkflow.next_run_at <= moment,
        )
        .order_by(ReliabilityWorkflow.next_run_at)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def workflow_history(
    session: AsyncSession, *, case_id: uuid.UUID, limit: int = 25
) -> list[ReliabilityWorkflow]:
    stmt = (
        select(ReliabilityWorkflow)
        .where(ReliabilityWorkflow.case_id == case_id)
        .order_by(ReliabilityWorkflow.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


__all__ = [
    "STAGE_CASE_STATUS",
    "STAGE_ORDER",
    "STAGE_TRANSITIONS",
    "TERMINAL_WORKFLOW_STATUSES",
    "StopCheck",
    "WorkflowError",
    "WorkflowTick",
    "active_workflow_for_case",
    "advance",
    "can_advance",
    "case_context",
    "check_preconditions",
    "claim_due",
    "get_workflow",
    "record_failure",
    "record_stage",
    "resume",
    "stage_index",
    "start_workflow",
    "stop_workflow",
    "wait_for_approval",
    "workflow_history",
]
