"""ARGUS Code-Intelligence Reaper (Phase 6 §55–§57, §64).

Phase 6's indexing and AI analysis run inside API requests. A process killed
mid-request leaves rows that claim work is still happening:

* a ``DebugAnalysisRun`` stuck in ``RUNNING``/``PENDING``;
* its ``DebugSession`` stuck in an active status (``ANALYZING``,
  ``CONTEXT_BUILDING``, ``WAITING_FOR_VALIDATION``);
* a ``CodeRepository`` whose ``index_status`` is ``INDEXING`` forever.

Those rows are lies. The session list would offer "analysing…" entries nobody
is driving, and a repository stuck in ``INDEXING`` reads as busy when no work
exists. The sweep marks them with the honest terminal states — the same
argument as the Phase 5 reaper, applied to the code-intelligence tables.

Analysis runs store their own ``started_at``, and the stale threshold is
``DEBUG_MAX_ANALYSIS_SECONDS``: an honest run can never legitimately outlive
its own budget by more than a grace period, because the manager aborts at the
budget. Anything older than the grace is by definition un-driven.

The sweep reports what it did rather than logging quietly, because a reaper
whose failures are invisible is indistinguishable from a leak.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.code import (
    DebugAnalysisRun,
    DebugAnalysisStatus,
    DebugSession,
    DebugSessionStatus,
)
from app.models.deployment import CodeRepository, RepositoryIndexStatus

logger = logging.getLogger(__name__)
settings = get_settings()

#: How long past its budget an un-driven run is given before the reaper acts.
#: The analysis manager enforces ``DEBUG_MAX_ANALYSIS_SECONDS`` itself; this
#: grace absorbs clock skew between the writer and the sweeper and the gap
#: between ``started_at`` (set before the model call) and the timeout check.
_STALE_GRACE_MULTIPLIER = 2

_SESSION_STALE_STATUSES = (
    DebugSessionStatus.CONTEXT_BUILDING,
    DebugSessionStatus.ANALYZING,
    DebugSessionStatus.WAITING_FOR_VALIDATION,
)

_RUN_STALE_STATUSES = (DebugAnalysisStatus.PENDING, DebugAnalysisStatus.RUNNING)


async def sweep_code_intelligence_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Run one sweep pass. Returns the summary it acted on."""
    try:
        async with session_factory() as session:
            summary = await _sweep(session)
            await session.commit()
            return summary
    except Exception as exc:  # noqa: BLE001 - the sweep must survive its own bugs
        logger.error("Code-intelligence sweep failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _sweep(session: AsyncSession) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    #: A run is stale when it has outlived its own budget times the grace
    #: multiplier. The manager's internal timeout makes any older RUNNING row
    #: un-driven, because the driver would have aborted and finalised it.
    stale_before = now - timedelta(
        seconds=settings.DEBUG_MAX_ANALYSIS_SECONDS * _STALE_GRACE_MULTIPLIER
    )

    stale_runs = list(
        (
            await session.execute(
                select(DebugAnalysisRun)
                .where(
                    DebugAnalysisRun.status.in_(_RUN_STALE_STATUSES),
                    DebugAnalysisRun.started_at < stale_before,
                )
                .limit(settings.CODE_SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )

    timed_out: list[str] = []
    for run in stale_runs:
        run.status = DebugAnalysisStatus.FAILED
        run.error = (
            "analysis abandoned: the process driving it stopped responding and the "
            "run outlived its budget; closed by the code-intelligence sweep"
        )
        run.completed_at = now
        timed_out.append(str(run.id))

    #: Sessions whose active status is older than the same threshold. ``updated_at``
    #: is bumped by every transition the manager makes, so an old ``updated_at``
    #: together with an active status means no driver has touched it since.
    stale_sessions = list(
        (
            await session.execute(
                select(DebugSession)
                .where(
                    DebugSession.status.in_(_SESSION_STALE_STATUSES),
                    DebugSession.updated_at < stale_before,
                )
                .limit(settings.CODE_SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )

    sessions_closed: list[str] = []
    for debug_session in stale_sessions:
        debug_session.status = DebugSessionStatus.FAILED
        debug_session.summary = (
            "investigation abandoned: the process driving it stopped responding; "
            "closed by the code-intelligence sweep"
        )
        sessions_closed.append(str(debug_session.id))

    #: Repositories stuck in INDEXING: same shape, older threshold (indexing has
    #: no per-run budget of its own, so the grace is the sweep interval itself).
    index_before = now - timedelta(
        seconds=max(60, settings.CODE_SWEEP_INTERVAL_SECONDS)
    )
    stuck_repos = list(
        (
            await session.execute(
                select(CodeRepository)
                .where(
                    CodeRepository.index_status == RepositoryIndexStatus.INDEXING,
                    CodeRepository.updated_at < index_before,
                )
                .limit(settings.CODE_SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )

    repos_reset: list[str] = []
    for repository in stuck_repos:
        repository.index_status = RepositoryIndexStatus.FAILED
        repos_reset.append(str(repository.id))

    if timed_out or sessions_closed or repos_reset:
        logger.warning(
            "Code-intelligence sweep: runs_failed=%d sessions_failed=%d repos_reset=%d",
            len(timed_out),
            len(sessions_closed),
            len(repos_reset),
        )
    return {
        "runs_failed": timed_out,
        "sessions_failed": sessions_closed,
        "repos_reset": repos_reset,
    }


async def sweep_code_intelligence_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int | None = None,
) -> None:
    """Run the sweep on a fixed interval until cancelled."""
    interval = max(5, int(interval_seconds or settings.CODE_SWEEP_INTERVAL_SECONDS))
    while True:
        await sweep_code_intelligence_once(session_factory)
        await asyncio.sleep(interval)


__all__ = [
    "sweep_code_intelligence_forever",
    "sweep_code_intelligence_once",
]
