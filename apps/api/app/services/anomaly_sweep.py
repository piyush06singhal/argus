"""ARGUS Scheduled Detection Sweep (Phase 3 §20).

The ingest hook makes detection responsive, but telemetry that arrives late,
sparsely, or through a path without a hook would otherwise never be evaluated.
This sweep closes that gap: every interval it evaluates each active project —
**per environment** — so environments are never pooled together.

Scope discipline:

* Detection always runs inside an environment, so production and staging
  telemetry can never contaminate each other's baselines. A project with no
  environments is evaluated once at project scope.
* Each scope runs in its own session/transaction: one failing project cannot
  roll back or block the rest.
* Bounded: at most ``project_limit`` projects and ``environment_limit``
  environments per project per pass.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.project import Environment, ProjectStatus, SoftwareProject
from app.services.sweep_leader import sweep_lease

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class SweepResult:
    """Summary of one sweep pass."""

    projects: int = 0
    scopes: int = 0
    anomalies_opened: int = 0
    anomalies_updated: int = 0
    suppressed: int = 0
    incidents_created: int = 0
    incidents_updated: int = 0
    skipped: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "projects": self.projects,
            "scopes": self.scopes,
            "anomalies_opened": self.anomalies_opened,
            "anomalies_updated": self.anomalies_updated,
            "suppressed": self.suppressed,
            "incidents_created": self.incidents_created,
            "incidents_updated": self.incidents_updated,
            "skipped": self.skipped,
            "errors": self.errors,
        }


async def _discover_scopes(
    session: AsyncSession,
    *,
    project_limit: int,
    environment_limit: int,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[uuid.UUID]]]:
    """Return active project ids and their active environments (bounded)."""
    project_stmt = (
        select(SoftwareProject.id)
        .where(SoftwareProject.status == ProjectStatus.ACTIVE)
        .order_by(SoftwareProject.created_at)
        .limit(project_limit)
    )
    project_ids = list((await session.execute(project_stmt)).scalars().all())
    if not project_ids:
        return [], {}

    env_stmt = (
        select(Environment.project_id, Environment.id)
        .where(
            Environment.project_id.in_(project_ids),
            Environment.is_active.is_(True),
        )
        .order_by(Environment.created_at)
    )
    envs: dict[uuid.UUID, list[uuid.UUID]] = {}
    for project_id, environment_id in (await session.execute(env_stmt)).all():
        bucket = envs.setdefault(project_id, [])
        if len(bucket) < environment_limit:
            bucket.append(environment_id)
    return project_ids, envs


async def run_detection_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_limit: int = 100,
    environment_limit: int = 200,
    now=None,
) -> SweepResult:
    """Evaluate every active project/environment scope once."""
    from app.services.anomaly_detection import AnomalyDetectionService
    from app.services.incident_manager import IncidentManager

    result = SweepResult()
    if not settings.ANOMALY_DETECTION_ENABLED:
        result.skipped = True
        return result

    from app.services.remediation_controls import safely_is_paused

    async with session_factory() as session:
        project_ids, envs_by_project = await _discover_scopes(
            session,
            project_limit=project_limit,
            environment_limit=environment_limit,
        )
    result.projects = len(project_ids)

    for project_id in project_ids:
        environments: list[Optional[uuid.UUID]] = []
        environments.extend(envs_by_project.get(project_id) or [])
        if not environments:
            # A project with no environments is evaluated once at project scope.
            environments.append(None)
        for environment_id in environments:
            result.scopes += 1
            # Own session per scope: isolation between projects/environments.
            async with session_factory() as session:
                # A PAUSE_BACKGROUND_JOB remediation on this job is a real effect,
                # so the sweep has to honour it — a pause the detector ignored
                # would be the one thing this phase must not ship (Phase 9 §10).
                if await safely_is_paused(
                    session,
                    "anomaly_sweep",
                    project_id=project_id,
                    environment_id=environment_id,
                    now=now,
                ):
                    logger.info(
                        "detection sweep paused for project %s environment %s",
                        project_id,
                        environment_id,
                    )
                    result.suppressed += 1
                    continue
                try:
                    #: One writer per project: the worker and this sweep run the
                    #: same detect+correlate pass, and interleaved writes deadlocked
                    #: PostgreSQL. Project ids are walked in ``created_at`` order
                    #: (above), so two sweeps take the same locks in the same order.
                    from app.services.project_lock import lock_project

                    await lock_project(session, project_id=project_id)
                    service = AnomalyDetectionService(session, now=now)
                    run_result = await service.run(
                        project_id=project_id, environment_id=environment_id
                    )
                    # Correlation follows detection so incidents always see the
                    # anomalies written in the same pass.
                    correlation_result = await IncidentManager(
                        session, now=now
                    ).process_scope(
                        project_id=project_id, environment_id=environment_id
                    )
                    await session.commit()
                except Exception as e:  # one scope must not stop the pass
                    logger.exception(
                        "Detection sweep failed for project=%s env=%s",
                        project_id,
                        environment_id,
                    )
                    result.errors.append(
                        f"{project_id}/{environment_id}: {type(e).__name__}: {e}"
                    )
                    continue
            result.anomalies_opened += run_result.anomalies_opened
            result.anomalies_updated += run_result.anomalies_updated
            result.suppressed += run_result.suppressed
            result.incidents_created += correlation_result.incidents_created
            result.incidents_updated += correlation_result.incidents_updated

    return result


async def sweep_forever(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: Optional[int] = None,
) -> None:
    """Run the sweep on an interval until cancelled.

    The loop is deliberately defensive: an exception in one pass is logged and
    the loop continues, so a transient database error cannot silently disable
    detection for the lifetime of the process.
    """
    interval = int(
        interval_seconds
        if interval_seconds is not None
        else settings.ANOMALY_SWEEP_INTERVAL_SECONDS
    )
    interval = max(5, interval)
    while True:
        # One pass per interval across the fleet (see ``sweep_leader``): with N
        # worker processes only the lease holder sweeps, so replicas add
        # throughput instead of duplicating this work.
        async with sweep_lease(session_factory, "anomaly") as leader:
            if not leader:
                logger.debug("Detection sweep: another worker holds the lease")
                await asyncio.sleep(interval)
                continue
            try:
                result = await run_detection_sweep(session_factory)
                if result.anomalies_opened or result.anomalies_updated:
                    logger.info(
                        "Detection sweep: scopes=%d opened=%d updated=%d",
                        result.scopes,
                        result.anomalies_opened,
                        result.anomalies_updated,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive
                logger.error("Detection sweep pass failed: %s", e)
        await asyncio.sleep(interval)


__all__ = ["SweepResult", "run_detection_sweep", "sweep_forever"]
