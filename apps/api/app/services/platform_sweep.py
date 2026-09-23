"""ARGUS Platform Sweep (Phase 11 §10, §12, §35, §58, §88).

The control plane's clock. One pass does five things, each bounded and idempotent,
in the order that makes each one see the previous one's results:

1. **Correlate** pending platform events into case timelines (§10).
2. **Route** live high-severity incidents into cases, and mirror what the phases
   concluded onto existing cases (§14, §15).
3. **Advance or stop** due workflow runs (§12, §13) — the engine's own safety
   checks decide which, and a stop is recorded, not silently dropped.
4. **Recompute** SLOs and error budgets (§35) and record state transitions (§5).
5. **Check** cross-phase consistency (§88) and, when ARGUS itself is degraded,
   notify an operator (§55, §58).

Everything respects the Phase 9 control plane: a pause on ``platform_sweep``
stops the whole pass, and the pass reports *that* as its result rather than as an
error — the platform was told to stop and it stopped, which is a success.

Nothing here can fail the API: the sweep runs in the lifespan, every step is
failure-isolated, and a partial pass logs what it could not do.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models.platform import WorkflowStatus
from app.models.project import ProjectStatus, SoftwareProject

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _step(session: AsyncSession) -> AsyncIterator[None]:
    """Run one sweep step inside a savepoint, so it is genuinely isolated.

    This module promises that a step's failure is recorded in ``errors`` and the
    pass continues. A bare ``try/except`` does not deliver that: a failed INSERT
    or flush leaves the shared transaction aborted, and every *later* step then
    raises ``PendingRollbackError`` — which hides the real cause and, when the
    sweep is driven from the API, fails the whole request with a 500.

    A savepoint makes the promise true: the failing step's partial work is undone
    and the pass keeps the work of the steps that succeeded.

    If the pass's transaction is already unusable — the server aborted it, or the
    connection died — every later savepoint would fail with a confusing
    "closed transaction" error. Saying so plainly is better than a cascade of
    knock-on failures that hide the original one.
    """
    if not session.is_active:
        raise RuntimeError("the sweep's transaction is no longer usable")
    async with session.begin_nested():
        yield


@dataclass
class PlatformSweepResult:
    """What one pass did."""

    projects_considered: int = 0
    events_consumed: int = 0
    timeline_entries: int = 0
    cases_opened: int = 0
    workflows_advanced: int = 0
    workflows_stopped: int = 0
    state_transitions: int = 0
    slo_evaluations: int = 0
    slo_burning: int = 0
    quality_issues_opened: int = 0
    quality_issues_resolved: int = 0
    notifications: int = 0
    degraded: bool = False
    paused: bool = False
    disabled: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "projects_considered": self.projects_considered,
            "events_consumed": self.events_consumed,
            "timeline_entries": self.timeline_entries,
            "cases_opened": self.cases_opened,
            "workflows_advanced": self.workflows_advanced,
            "workflows_stopped": self.workflows_stopped,
            "state_transitions": self.state_transitions,
            "slo_evaluations": self.slo_evaluations,
            "slo_burning": self.slo_burning,
            "quality_issues_opened": self.quality_issues_opened,
            "quality_issues_resolved": self.quality_issues_resolved,
            "notifications": self.notifications,
            "degraded": self.degraded,
            "paused": self.paused,
            "disabled": self.disabled,
            "errors": list(self.errors),
        }


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def run_platform_sweep(
    session: AsyncSession,
    *,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    project_id: Optional[Any] = None,
) -> PlatformSweepResult:
    """One pass of the control plane's scheduled work."""
    settings = settings or get_settings()
    moment = _aware(now)
    result = PlatformSweepResult()

    if not settings.PLATFORM_ENABLED:
        result.disabled = True
        return result

    from app.services.remediation_controls import safely_is_paused

    if await safely_is_paused(session, "platform_sweep", project_id=project_id):
        result.paused = True
        return result

    if project_id is not None:
        targets = [project_id]
    else:
        #: Deterministic order, because the per-project steps take row locks: two
        #: sweeps must acquire them in the same sequence or they can deadlock on
        #: the mutex itself rather than on the data.
        stmt = (
            select(SoftwareProject.id)
            .where(SoftwareProject.status != ProjectStatus.ARCHIVED)
            .order_by(SoftwareProject.created_at)
            .limit(200)
        )
        targets = list((await session.scalars(stmt)).all())

    # -- 1. correlate events into timelines
    from app.services.control_plane import (
        correlate_events,
        route_incident_events,
        sync_case_with_phases,
    )

    try:
        async with _step(session):
            correlation = await correlate_events(
                session,
                limit=settings.PLATFORM_STATE_ACTIVE_LIMIT,
                now=moment,
                settings=settings,
            )
    except Exception as exc:
        logger.warning("platform event correlation failed", exc_info=True)
        result.errors.append(f"correlation: {exc}")
    else:
        result.events_consumed += correlation.consumed
        result.timeline_entries += correlation.timeline_entries
        result.cases_opened += correlation.cases_opened
        result.errors.extend(correlation.errors)

    # -- 2. route live incidents into cases and refresh existing ones
    try:
        async with _step(session):
            routing = await route_incident_events(session, limit=200)
    except Exception as exc:
        logger.warning("incident routing failed", exc_info=True)
        result.errors.append(f"routing: {exc}")
    else:
        result.cases_opened += routing.cases_opened

    # -- 3. advance or stop due workflows
    try:
        from app.services.reliability_case import get_case
        from app.services.reliability_workflow import (
            advance,
            claim_due,
            resume,
        )

        for workflow in await claim_due(
            session, limit=settings.PLATFORM_WORKFLOW_BATCH, now=moment
        ):
            case = (
                await get_case(session, case_id=workflow.case_id)
                if workflow.case_id
                else None
            )
            try:
                async with _step(session):
                    if workflow.status == WorkflowStatus.WAITING_APPROVAL:
                        tick = await resume(
                            session, workflow=workflow, case=case, now=moment
                        )
                    else:
                        target = _next_stage_for(session, workflow)
                        if target is None:
                            continue
                        tick = await advance(
                            session,
                            workflow=workflow,
                            target=target,
                            case=case,
                            actor="platform-sweep",
                            reason="scheduled progression",
                            now=moment,
                        )
                    if case is not None:
                        await sync_case_with_phases(session, case=case, now=moment)
            except Exception as exc:
                logger.warning(
                    "workflow %s failed to advance", workflow.id, exc_info=True
                )
                result.errors.append(f"workflow {workflow.id}: {exc}")
                continue
            if tick.action == "stopped":
                result.workflows_stopped += 1
            elif tick.action == "advanced":
                result.workflows_advanced += 1
    except Exception as exc:
        logger.warning("workflow progression failed", exc_info=True)
        result.errors.append(f"workflows: {exc}")

    # -- 4. per-project recomputation
    from app.services.data_quality_center import run_consistency_checks
    from app.services.slo_service import evaluate_project
    from app.services.system_state import build_system_state, record_transitions

    for target in targets:
        result.projects_considered += 1
        #: The same per-project mutex the deletion path takes (see
        #: ``project_lock``). A sweep writing an SLO snapshot or a data-quality
        #: issue while a project was being cascade-deleted deadlocked PostgreSQL;
        #: both sides now take the lock first, so one of them simply waits.
        from app.services.project_lock import lock_project

        await lock_project(session, project_id=target)

        try:
            async with _step(session):
                state = await build_system_state(
                    session,
                    project_id=target,
                    now=moment,
                    settings=settings,
                    include=("components",),
                )
                transitions = await record_transitions(
                    session,
                    project_id=target,
                    results=await _state_results(session, target, moment, settings),
                    environment_by_component={
                        uuid_of(component["id"]): (
                            uuid_of(component["environment_id"])
                            if component.get("environment_id")
                            else None
                        )
                        for component in state.components
                    },
                    now=moment,
                )
        except Exception as exc:
            logger.warning("state recomputation failed for %s", target, exc_info=True)
            result.errors.append(f"state {target}: {exc}")
        else:
            result.state_transitions += len(transitions)

        try:
            async with _step(session):
                slo = await evaluate_project(
                    session, project_id=target, now=moment, settings=settings
                )
        except Exception as exc:
            logger.warning("SLO evaluation failed for %s", target, exc_info=True)
            result.errors.append(f"slo {target}: {exc}")
        else:
            result.slo_evaluations += int(slo.get("evaluated", 0))
            result.slo_burning += int(slo.get("burning", 0))
            result.errors.extend(slo.get("errors", []))

        try:
            async with _step(session):
                quality = await run_consistency_checks(
                    session, project_id=target, now=moment, settings=settings
                )
        except Exception as exc:
            logger.warning("consistency checks failed for %s", target, exc_info=True)
            result.errors.append(f"quality {target}: {exc}")
        else:
            result.quality_issues_opened += quality.opened
            result.quality_issues_resolved += quality.resolved
            result.errors.extend(quality.errors)

    # -- 5. ARGUS's own health, and tell someone if it is degraded
    try:
        from app.services.platform_health import platform_health
        from app.services.platform_notifications import (
            raise_platform_degradation,
        )

        async with _step(session):
            report = await platform_health(session, settings=settings, now=moment)
            result.degraded = report.status != "OK"
            notification = None
            if targets and result.degraded:
                notification = await raise_platform_degradation(
                    session,
                    project_id=targets[0],
                    report=report,
                    now=moment,
                    settings=settings,
                )
    except Exception as exc:
        logger.warning("platform health check failed", exc_info=True)
        result.degraded = False
        result.errors.append(f"health: {exc}")
    else:
        if notification is not None:
            result.notifications += 1

    return result


def uuid_of(value: Any) -> Any:
    import uuid as _uuid

    if value is None:
        return None
    if isinstance(value, _uuid.UUID):
        return value
    return _uuid.UUID(str(value))


async def _state_results(
    session: AsyncSession,
    project_id: Any,
    moment: datetime,
    settings: Settings,
) -> list[Any]:
    """Derived component states for every component in a project."""
    from app.models.system import SystemComponent
    from app.services.system_state import derive_component_states

    component_ids = list(
        (
            await session.scalars(
                select(SystemComponent.id)
                .where(SystemComponent.project_id == project_id)
                .limit(settings.PLATFORM_STATE_COMPONENT_LIMIT)
            )
        ).all()
    )
    if not component_ids:
        return []
    return await derive_component_states(
        session,
        project_id=project_id,
        component_ids=component_ids,
        now=moment,
        settings=settings,
    )


def _next_stage_for(session: AsyncSession, workflow: Any) -> Optional[Any]:
    """The stage the workflow should be at next, given what exists now.

    The progression is *evidence-driven*, not time-driven: the sweep only moves a
    run to a stage whose precondition is actually satisfied by stored rows. A run
    whose analysis is not finished stays where it is, rather than advancing to
    ``DIAGNOSED`` because a timer fired.
    """
    from app.models.platform import WorkflowStage

    stages = list(WorkflowStage)
    index = stages.index(workflow.stage)
    if index + 1 >= len(stages):
        return None
    state = workflow.state or {}
    target = stages[index + 1]
    #: ANALYSIS and DIAGNOSIS advance when the phase that produces them reports —
    #: which happens through the event stream, so the sweep does not push past
    #: ANALYZING on its own.
    if workflow.stage in (WorkflowStage.ANALYZING, WorkflowStage.AUTHORIZED):
        if not state.get("ready_for"):
            return None
    return target


async def sweep_forever(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run the sweep on an interval until cancelled (the lifespan does this)."""
    settings = get_settings()
    interval = max(30, settings.PLATFORM_SWEEP_INTERVAL_SECONDS)
    logger.info("platform sweep started (interval=%ss)", interval)
    while True:
        try:
            async with session_factory() as session:
                result = await run_platform_sweep(session, settings=settings)
                await session.commit()
                if (
                    result.events_consumed
                    or result.cases_opened
                    or result.state_transitions
                    or result.quality_issues_opened
                    or result.degraded
                ):
                    logger.info(
                        "platform sweep: events=%d cases=%d transitions=%d "
                        "slo=%d quality=%d degraded=%s",
                        result.events_consumed,
                        result.cases_opened,
                        result.state_transitions,
                        result.slo_evaluations,
                        result.quality_issues_opened,
                        result.degraded,
                    )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            logger.info("platform sweep stopping")
            raise
        except Exception:  # pragma: no cover - never let a timer kill the app
            logger.warning("platform sweep pass failed", exc_info=True)
        await asyncio.sleep(interval)


__all__ = [
    "PlatformSweepResult",
    "run_platform_sweep",
    "sweep_forever",
]
