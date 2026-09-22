"""ARGUS Reproduction Reaper (Phase 5 §39, §54, §55).

A periodic sweep with two jobs, both of which exist because a worker can die:

* **Close abandoned experiments.** A worker that crashes mid-experiment leaves a
  row in ``RUNNING`` with a ``timeout_at`` in the past and nobody driving it.
  The reaper marks it ``TIMED_OUT``, which is honest: the experiment stopped, and
  the reason is a budget, not an observation.
* **Destroy orphaned sandboxes.** Every sandbox belonging to a terminal
  experiment — or whose owning experiment is gone — is destroyed. This is the
  belt to the orchestrator's braces: the orchestrator's ``finally`` handles the
  normal path, and the reaper handles the path where the process died between
  provisioning and cleanup.

It reports what it did rather than logging quietly, because a reaper whose
failures are invisible is indistinguishable from a resource leak (§54).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.services.reproduction_orchestrator import ReproductionOrchestrator

logger = logging.getLogger(__name__)
settings = get_settings()


async def sweep_reproductions_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Run one reaper pass. Returns the summary it acted on.

    Phase 9 §10: a ``PAUSE_BACKGROUND_JOB`` remediation on ``reproduction_sweep``
    is honoured here, per project. The reaper has no project loop of its own, so
    the paused scopes are read once and passed down rather than the whole pass
    being skipped — that would let one project's pause withhold reaping from
    every other, which is not what the action claimed.
    """
    from app.services.remediation_controls import safely_paused_scope_ids

    async with session_factory() as session:
        global_pause, paused_projects = await safely_paused_scope_ids(
            session, "reproduction_sweep"
        )
    if global_pause:
        logger.info("reproduction sweep skipped: paused for every scope")
        return {
            "timed_out": [],
            "sandboxes_cleaned": [],
            "cleanup_failures": [],
            "paused_skipped": True,
        }

    orchestrator = ReproductionOrchestrator(session_factory)
    try:
        summary = await orchestrator.sweep_stale(paused_project_ids=paused_projects)
    except Exception as exc:  # noqa: BLE001 - the sweep must survive its own bugs
        logger.error("Reproduction sweep failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    if summary.get("timed_out") or summary.get("sandboxes_cleaned"):
        logger.info(
            "Reproduction sweep: timed_out=%d sandboxes_cleaned=%d cleanup_failures=%d",
            len(summary.get("timed_out") or []),
            len(summary.get("sandboxes_cleaned") or []),
            len(summary.get("cleanup_failures") or []),
        )
    if summary.get("cleanup_failures"):
        # Surfaced loudly: leaked sandboxes are exactly what §55 forbids.
        logger.error(
            "Reproduction cleanup failed for %s; they will be retried on the next pass",
            ", ".join(summary["cleanup_failures"]),
        )
    return summary


async def sweep_reproductions_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int | None = None,
) -> None:
    """Run the reaper on a fixed interval until cancelled."""
    interval = max(5, int(interval_seconds or settings.REPRO_SWEEP_INTERVAL_SECONDS))
    while True:
        await sweep_reproductions_once(session_factory)
        await asyncio.sleep(interval)


__all__ = ["sweep_reproductions_forever", "sweep_reproductions_once"]
