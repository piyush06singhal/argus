"""ARGUS Fix Workspace Reaper (Phase 7 §48, §54).

A periodic sweep with one job that exists because a process can die: a patch
verification interrupted between workspace creation and cleanup leaves a
directory on disk and a ``PatchWorkspace`` row stuck in ``PATCH_APPLIED`` —
the same failure shape the Phase 5 reaper handles for sandboxes.

The sweep destroys workspaces whose row is past the grace period (default one
hour — long enough that a *slow but alive* verification is never reaped) and
marks the rows ``DESTROYED`` with the reason. It reports what it did rather
than logging quietly, because a reaper whose failures are invisible is
indistinguishable from a disk leak (§54).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.fix import PatchWorkspace, WorkspaceStatus
from app.services.sweep_leader import sweep_lease

logger = logging.getLogger(__name__)
settings = get_settings()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Normalise a stored timestamp to aware UTC for comparison.

    PostgreSQL returns aware datetimes; SQLite's CURRENT_TIMESTAMP returns
    naive ones (in UTC). Comparing them directly raises, so every stored
    value goes through this before the cutoff arithmetic.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def sweep_fix_workspaces_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one reaper pass. Returns the summary it acted on."""
    now = now or _utcnow()
    grace = timedelta(seconds=settings.FIX_WORKSPACE_GRACE_SECONDS)
    cutoff = now - grace

    summary: dict[str, Any] = {
        "stale_candidates": 0,
        "destroyed": 0,
        "destroy_errors": 0,
        "marked_failed": 0,
        "paused_skipped": 0,
        "errors": [],
    }

    async with session_factory() as session:
        #: Phase 9 §10: a pause on fix_sweep is honoured per row, so pausing it
        #: for one project never withholds reaping from another.
        from app.services.remediation_controls import safely_paused_scope_ids

        global_pause, paused_projects = await safely_paused_scope_ids(
            session, "fix_sweep", now=now
        )
        if global_pause:
            logger.info("fix workspace sweep skipped: paused for every scope")
            summary["paused_skipped"] = -1
            return summary

        rows = (
            (
                await session.execute(
                    select(PatchWorkspace)
                    .where(
                        PatchWorkspace.status.in_(
                            [WorkspaceStatus.CREATING, WorkspaceStatus.PATCH_APPLIED]
                        )
                    )
                    .order_by(PatchWorkspace.created_at.asc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )

        for row in rows:
            if row.project_id is not None and row.project_id in paused_projects:
                summary["paused_skipped"] += 1
                continue
            anchor = row.created_at_workspace or row.created_at
            if anchor is None or _aware(anchor) > cutoff:
                continue
            summary["stale_candidates"] += 1

            root = row.root_path
            if root:
                root_path = Path(root)
                if root_path.exists():
                    try:
                        shutil.rmtree(root_path)
                        summary["destroyed"] += 1
                    except OSError as error:
                        summary["destroy_errors"] += 1
                        summary["errors"].append(
                            f"workspace {row.id}: {type(error).__name__}: {error}"
                        )
                        continue

            row.status = WorkspaceStatus.DESTROYED
            row.destroyed_at = now
            row.workspace_metadata = {
                **(row.workspace_metadata or {}),
                "reaped": True,
                "reaped_reason": "stale workspace past grace period (§48)",
                "reaped_at": now.isoformat(),
            }

        await session.commit()

    if summary["stale_candidates"] or summary["destroy_errors"]:
        logger.info(
            "fix workspace sweep: %s stale, %s destroyed, %s errors",
            summary["stale_candidates"],
            summary["destroyed"],
            summary["destroy_errors"],
        )
    return summary


async def sweep_fix_forever(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run the reaper periodically until cancelled (§54)."""
    interval = max(30, settings.FIX_SWEEP_INTERVAL_SECONDS)
    while True:
        # One pass per interval across the fleet (see ``sweep_leader``).
        async with sweep_lease(session_factory, "fix") as leader:
            if not leader:
                logger.debug("Fix workspace sweep: another worker holds the lease")
                await asyncio.sleep(interval)
                continue
            try:
                await sweep_fix_workspaces_once(session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the loop must survive
                logger.exception("fix workspace sweep failed: %s", error)
        await asyncio.sleep(interval)


__all__ = ["sweep_fix_forever", "sweep_fix_workspaces_once"]
