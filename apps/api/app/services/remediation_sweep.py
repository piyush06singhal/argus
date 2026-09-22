"""ARGUS Remediation Sweep (Phase 9 §39, §27, §25).

The scheduled side of the pipeline. Three jobs, all of them bounded and
idempotent:

* **Progress the pipeline.** Plan proposals for open incidents and elevated
  forecasts, and drive through the steps that nothing else will: an
  autonomous action that reached ``AUTHORIZED`` but whose process died before
  execution, or a ``SCHEDULED`` action waiting on a worker.
* **Retry verification.** An action whose verification was inconclusive is
  re-verified while attempts remain, then escalated to a human. An action that sat
  in ``VERIFYING`` forever would be indistinguishable from a successful one in
  every dashboard.
* **Clean up.** Expire stale approvals and actions, close executions a dead
  process left ``RUNNING``, cool breakers down, and retire controls past their
  own deadlines so a temporary pause cannot become permanent by accident.

The sweep never *authorizes* anything. It executes only what already cleared its
gates, and it can only ever move an action toward a terminal state it has
evidence for.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.incident import Incident, IncidentStatus
from app.models.project import ProjectStatus, SoftwareProject
from app.models.reliability import (
    ForecastRiskLevel,
    ForecastStatus,
    ReliabilityForecast,
)
from app.models.remediation import (
    CircuitState,
    ExecutionStatus,
    RemediationAction,
    RemediationCircuitBreaker,
    RemediationApproval,
    RemediationApprovalStatus,
    RemediationAuditEventType,
    RemediationControl,
    RemediationExecution,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationStatus,
    VerificationVerdict,
)
from app.services.remediation_audit import AuditTrail
from app.services.remediation_clock import aware, is_expired, utcnow
from app.services.remediation_policy import resolve_policy
from app.services.remediation_controls import safely_is_paused
from app.services.remediation_planner import RemediationPlanner
from app.services.remediation_service import RemediationService
from app.services.remediation_state import (
    IN_FLIGHT_STATUSES,
    apply_transition,
    is_terminal,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Risk levels that justify planning a remediation from a forecast.
_PLANNING_RISK_LEVELS = (ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL)


async def sweep_remediations_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: Optional[datetime] = None,
    project_id: Optional[Any] = None,
    plan: bool = True,
) -> dict[str, Any]:
    """One full remediation pass. Returns what it acted on."""
    now = aware(now or utcnow())
    summary: dict[str, Any] = {
        "now": now.isoformat(),
        "projects": 0,
        "proposals_created": 0,
        "executed": 0,
        "verified": 0,
        "retried": 0,
        "expired_approvals": 0,
        "expired_actions": 0,
        "stuck_executions": 0,
        "controls_retired": 0,
        "breakers_cooled": 0,
        "blocked": 0,
        "paused_projects": 0,
        "errors": [],
    }

    async with session_factory() as session:
        if project_id is not None:
            project_ids = [project_id]
        else:
            project_ids = list(
                (
                    await session.execute(
                        select(SoftwareProject.id)
                        .where(SoftwareProject.status == ProjectStatus.ACTIVE)
                        .order_by(SoftwareProject.created_at)
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
        summary["projects"] = len(project_ids)

        for pid in project_ids:
            try:
                # §10: the remediation sweep can also be the target of its own
                # control plane. Paused for this project means this project's
                # actions are left exactly as they are — not expired, not
                # executed, not planned — until the control is reverted or expires.
                if await safely_is_paused(
                    session, "remediation_sweep", project_id=pid, now=now
                ):
                    summary["paused_projects"] += 1
                    continue
                if plan and settings.REMEDIATION_PLANNER_ENABLED:
                    created = await _plan_project(session, pid, now=now)
                    summary["proposals_created"] += created
                counts = await _progress_project(session, pid, now=now)
                for key, value in counts.items():
                    if key in summary and isinstance(value, int):
                        summary[key] += value
            except Exception as error:  # noqa: BLE001 - one project cannot stop the sweep
                logger.error("remediation sweep failed for project %s: %s", pid, error)
                summary["errors"].append(
                    f"{pid}: {type(error).__name__}: {error}"[:300]
                )
                await session.rollback()

        # Housekeeping that is not project-scoped.
        summary["controls_retired"] += await _retire_expired_controls(session, now=now)
        summary["breakers_cooled"] += await _cool_breakers(session, now=now)
        summary["stuck_executions"] += await _close_stuck_executions(
            session, now=now, project_ids=project_ids
        )
        await session.commit()

    return summary


async def _plan_project(
    session: AsyncSession, project_id: Any, *, now: datetime
) -> int:
    """Generate proposals for open incidents and elevated forecasts."""
    planner = RemediationPlanner(session)
    service = RemediationService(session)
    created = 0

    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(Incident.project_id == project_id)
                .where(
                    Incident.status.in_(
                        [
                            IncidentStatus.OPEN,
                            IncidentStatus.ACKNOWLEDGED,
                            IncidentStatus.INVESTIGATING,
                        ]
                    )
                )
                .order_by(Incident.detected_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for incident in incidents:
        drafts = await planner.plan_for_incident(incident, now=now)
        if not drafts:
            continue
        actions = await service.propose(
            drafts, created_by="remediation-planner", now=now
        )
        created += len(actions)

    forecasts = list(
        (
            await session.execute(
                select(ReliabilityForecast)
                .where(ReliabilityForecast.project_id == project_id)
                .where(ReliabilityForecast.risk_level.in_(_PLANNING_RISK_LEVELS))
                .where(ReliabilityForecast.status == ForecastStatus.ACTIVE)
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for forecast in forecasts:
        drafts = await planner.plan_for_forecast(forecast, now=now)
        if not drafts:
            continue
        actions = await service.propose(
            drafts, created_by="remediation-planner", now=now
        )
        created += len(actions)

    if created:
        await session.flush()
    return created


async def _pending_approvals(
    session: AsyncSession, action_id: Any
) -> Sequence[RemediationApproval]:
    """Every approval request for this action that is still waiting on a person."""
    return list(
        (
            await session.execute(
                select(RemediationApproval)
                .where(RemediationApproval.action_id == action_id)
                .where(RemediationApproval.status == RemediationApprovalStatus.PENDING)
            )
        )
        .scalars()
        .all()
    )


async def _progress_project(
    session: AsyncSession, project_id: Any, *, now: datetime
) -> dict[str, int]:
    """Move this project's actions forward where it is safe to do so."""
    counts = {
        "executed": 0,
        "verified": 0,
        "retried": 0,
        "expired_approvals": 0,
        "expired_actions": 0,
        "blocked": 0,
    }
    service = RemediationService(session)

    # 1. Expire approvals that were never decided.
    pending = list(
        (
            await session.execute(
                select(RemediationApproval)
                .where(RemediationApproval.project_id == project_id)
                .where(RemediationApproval.status == RemediationApprovalStatus.PENDING)
                .limit(settings.REMEDIATION_SWEEP_BATCH * 4)
            )
        )
        .scalars()
        .all()
    )
    for approval in pending:
        if not is_expired(approval.expires_at, now=now):
            continue
        approval.status = RemediationApprovalStatus.EXPIRED
        action = await session.get(RemediationAction, approval.action_id)
        if action is not None and action.status == RemediationStatus.AWAITING_APPROVAL:
            action.failure_reason = RemediationFailureReason.APPROVAL_EXPIRED
            action.failure_detail = "the approval request expired before a decision"
            apply_transition(action, RemediationStatus.EXPIRED)
            await AuditTrail(session).record(
                action,
                RemediationAuditEventType.ACTION_EXPIRED,
                summary="expired: no decision was recorded in time",
                to_status=RemediationStatus.EXPIRED,
                now=now,
            )
            counts["expired_approvals"] += 1

    # 2. Expire actions nobody acted on.
    #
    # ``AWAITING_APPROVAL`` is in this list deliberately. Its approval has its
    # own, shorter TTL and step 1 above normally expires it first — but the
    # action is what actually carries the §107 guarantee ("a stale action
    # expires instead of executing later"), so its own deadline has to hold on
    # its own. Otherwise an action past its deadline whose approval is still
    # pending (a longer ``REMEDIATION_APPROVAL_TTL_SECONDS``, an approval row
    # written later, an operator's clock) could still be approved and executed
    # hours later.
    stale = list(
        (
            await session.execute(
                select(RemediationAction)
                .where(RemediationAction.project_id == project_id)
                .where(
                    RemediationAction.status.in_(
                        [
                            RemediationStatus.PROPOSED,
                            RemediationStatus.VALIDATING,
                            RemediationStatus.POLICY_REVIEW,
                            RemediationStatus.AWAITING_APPROVAL,
                            RemediationStatus.AUTHORIZED,
                            RemediationStatus.SCHEDULED,
                            RemediationStatus.BLOCKED,
                        ]
                    )
                )
                .limit(settings.REMEDIATION_SWEEP_BATCH * 4)
            )
        )
        .scalars()
        .all()
    )
    for action in stale:
        expires_at = action.expires_at
        if expires_at is None:
            created = action.created_at
            if created is None:
                continue
            expires_at = aware(created) + timedelta(
                seconds=settings.REMEDIATION_ACTION_EXPIRY_SECONDS
            )
        if not is_expired(expires_at, now=now):
            continue
        action.failure_reason = RemediationFailureReason.STALE_ACTION
        action.failure_detail = "the action expired before it was executed"
        apply_transition(action, RemediationStatus.EXPIRED)
        #: A decision on a deadline that has passed is not a decision. Leaving
        #: the approval PENDING would offer an approver a button that can no
        #: longer do anything, so it is closed with the action.
        for approval in await _pending_approvals(session, action.id):
            approval.status = RemediationApprovalStatus.EXPIRED
            approval.decided_at = now
            approval.reason = "the action expired before a decision was recorded"
        await AuditTrail(session).record(
            action,
            RemediationAuditEventType.ACTION_EXPIRED,
            summary="expired before execution",
            to_status=RemediationStatus.EXPIRED,
            now=now,
        )
        counts["expired_actions"] += 1

    # 3. Execute only what already cleared its gates.
    runnable = list(
        (
            await session.execute(
                select(RemediationAction)
                .where(RemediationAction.project_id == project_id)
                .where(
                    RemediationAction.status.in_(
                        [RemediationStatus.AUTHORIZED, RemediationStatus.SCHEDULED]
                    )
                )
                .order_by(RemediationAction.created_at)
                .limit(settings.REMEDIATION_SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for action in runnable:
        if (
            action.status == RemediationStatus.AUTHORIZED
            and action.execution_mode != RemediationExecutionMode.AUTONOMOUS
        ):
            # A human-approved action is executed by the caller that approved it;
            # the sweep does not decide that a human's window has arrived.
            continue
        result = await service.run(action, actor="remediation-sweep", now=now)
        if result.get("status") == RemediationStatus.VERIFIED.value:
            counts["verified"] += 1
        elif result.get("status") == RemediationStatus.BLOCKED.value:
            counts["blocked"] += 1
        else:
            counts["executed"] += 1

    # 4. Re-verify what is still waiting on evidence.
    verifying = list(
        (
            await session.execute(
                select(RemediationAction)
                .where(RemediationAction.project_id == project_id)
                .where(RemediationAction.status == RemediationStatus.VERIFYING)
                .order_by(RemediationAction.completed_at)
                .limit(settings.REMEDIATION_SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for action in verifying:
        completed = action.completed_at or action.started_at
        if completed is None:
            continue
        policy = await resolve_policy(session, action.project_id, action.environment_id)
        elapsed = (now - aware(completed)).total_seconds()
        if (
            elapsed
            < policy.verification_window_seconds + policy.verification_grace_seconds
        ):
            # Too early: the observation window has not filled yet.
            continue
        try:
            verdict = await service.verify(action, now=now, actor="remediation-sweep")
        except ValueError:
            continue
        if verdict.verdict == VerificationVerdict.INCONCLUSIVE:
            counts["retried"] += 1
        else:
            counts["verified"] += 1

    await session.flush()
    return counts


async def _retire_expired_controls(session: AsyncSession, *, now: datetime) -> int:
    """Retire controls whose own deadline has passed.

    A control that expires on read (the control plane already honours that) still
    needs to stop being *labelled* current, or a later audit would show a pause
    that had ended as though it were still in force.
    """
    rows = list(
        (
            await session.execute(
                select(RemediationControl)
                .where(RemediationControl.is_current.is_(True))
                .where(RemediationControl.expires_at.is_not(None))
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    retired = 0
    for row in rows:
        if not is_expired(row.expires_at, now=now):
            continue
        row.is_current = False
        row.reverted_at = now
        retired += 1
    if retired:
        await session.flush()
    return retired


async def _cool_breakers(session: AsyncSession, *, now: datetime) -> int:
    """Move cooled-down breakers to HALF_OPEN so one probe is allowed."""
    rows = list(
        (
            await session.execute(
                select(RemediationCircuitBreaker)
                .where(RemediationCircuitBreaker.state == CircuitState.OPEN)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    cooled = 0
    for row in rows:
        if row.opened_until is None:
            continue
        if aware(row.opened_until) <= now:
            row.state = CircuitState.HALF_OPEN
            cooled += 1
    if cooled:
        await session.flush()
    return cooled


async def _close_stuck_executions(
    session: AsyncSession, *, now: datetime, project_ids: list[Any]
) -> int:
    """Close attempts a dead process left RUNNING, and their actions.

    Only attempts older than the execution timeout are closed, so a live attempt
    running right now is never clobbered.
    """
    if not project_ids:
        return 0
    cutoff = now - timedelta(
        seconds=settings.REMEDIATION_HARD_EXECUTION_TIMEOUT_SECONDS * 3
    )
    rows = list(
        (
            await session.execute(
                select(RemediationExecution)
                .where(RemediationExecution.status == ExecutionStatus.RUNNING)
                .where(RemediationExecution.started_at <= cutoff)
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for execution in rows:
        if await safely_is_paused(
            session,
            "remediation_sweep",
            project_id=execution.project_id,
            now=now,
        ):
            continue
        execution.status = ExecutionStatus.FAILED
        execution.failure_reason = RemediationFailureReason.TIMEOUT
        execution.error = (
            "the attempt never reported a result; it is assumed to have been "
            "abandoned by a dead process"
        )
        execution.completed_at = now
        closed += 1
        action = await session.get(RemediationAction, execution.action_id)
        if action is None or is_terminal(action.status):
            continue
        if action.status in IN_FLIGHT_STATUSES:
            action.failure_reason = RemediationFailureReason.TIMEOUT
            action.failure_detail = execution.error
            apply_transition(action, RemediationStatus.FAILED)
            await AuditTrail(session).record(
                action,
                RemediationAuditEventType.EXECUTION_FAILED,
                summary="the attempt was abandoned by a dead process",
                to_status=RemediationStatus.FAILED,
                now=now,
            )
    if closed:
        await session.flush()
    return closed


async def sweep_remediations_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: Optional[int] = None,
) -> None:
    """Run the remediation sweep on an interval until cancelled."""
    interval = interval_seconds or settings.REMEDIATION_SWEEP_INTERVAL_SECONDS
    while True:
        try:
            summary = await sweep_remediations_once(session_factory)
            acted = sum(
                value
                for key, value in summary.items()
                if isinstance(value, int) and key != "projects"
            )
            if acted:
                logger.info("remediation sweep: %s", summary)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as error:  # noqa: BLE001 - the loop must survive
            logger.error("remediation sweep iteration failed: %s", error)
        await asyncio.sleep(interval)


__all__ = [
    "sweep_remediations_forever",
    "sweep_remediations_once",
]
