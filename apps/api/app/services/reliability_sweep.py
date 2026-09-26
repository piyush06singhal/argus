"""ARGUS Predictive Reliability Sweep (Phase 8 §57, §58, §77).

The scheduled side of the pipeline: for every active project it generates the
forecasts that are due, scores the ones whose windows have elapsed, refreshes
early warnings, and expires what has passed.

Why a sweep exists at all, when ingestion can enqueue a job: telemetry arrives
sparsely and late, a worker can die mid-run, and nothing in the ingest path
knows how to *evaluate* a forecast an hour later. A periodic pass is what makes
the lifecycle actually progress.

Three properties:

* **Bounded.** Projects, components and forecasts are all capped by settings, so
  one sweep can never scan a whole tenant or run unbounded (§59).
* **Idempotent.** Generation refreshes inside its window rather than
  duplicating, and evaluation skips forecasts that already have an outcome, so
  running the sweep twice changes nothing.
* **Reported, not silent.** Every phase of the pass returns counts; a sweep
  whose failures are invisible is indistinguishable from a quiet system (§77).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.project import Environment, ProjectStatus, SoftwareProject
from app.services.reliability_evaluation import PredictionEvaluationService
from app.services.reliability_features import aware_utc
from app.services.reliability_forecast_service import ReliabilityForecastService
from app.services.reliability_warnings import EarlyWarningService
from app.services.sweep_leader import sweep_lease

logger = logging.getLogger(__name__)
settings = get_settings()


async def sweep_reliability_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: Optional[datetime] = None,
    project_id: Optional[Any] = None,
) -> dict[str, Any]:
    """Run one full predictive-reliability pass. Returns what it acted on."""
    now = aware_utc(now or datetime.now(timezone.utc))
    summary: dict[str, Any] = {
        "now": now.isoformat(),
        "projects": 0,
        "forecasts_created": 0,
        "forecasts_updated": 0,
        "forecasts_revised": 0,
        "refusals": 0,
        "signals": 0,
        "scored": 0,
        "skipped_scores": 0,
        "warnings_raised": 0,
        "warnings_updated": 0,
        "warnings_suppressed": 0,
        "warnings_expired": 0,
        "forecasts_expired": 0,
        "errors": [],
    }
    if not settings.RELIABILITY_FORECASTING_ENABLED:
        summary["skipped"] = "RELIABILITY_FORECASTING_ENABLED is false"
        return summary

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

        #: Environments are resolved per project so a project with several
        #: environments gets a forecast per environment rather than one
        #: project-wide row that hides which one degraded.
        environments: dict[Any, list[Any]] = {}
        if project_ids:
            env_rows = (
                await session.execute(
                    select(Environment.project_id, Environment.id).where(
                        Environment.project_id.in_(project_ids)
                    )
                )
            ).all()
            for project, environment in env_rows:
                environments.setdefault(project, []).append(environment)

        forecast_service = ReliabilityForecastService(session)
        evaluation_service = PredictionEvaluationService(session)
        warning_service = EarlyWarningService(session)

        for pid in project_ids:
            # Phase 9 §10: the reliability pipeline is stoppable through ARGUS's
            # own control plane, so an operator (or an autonomous remediation) can
            # pause forecasting for one project without touching configuration.
            from app.services.remediation_controls import (
                safely_feature_enabled,
                safely_is_paused,
            )

            if await safely_is_paused(session, "reliability_sweep", project_id=pid):
                summary.setdefault("skipped_projects", []).append(str(pid))
                continue
            if not await safely_feature_enabled(
                session, "reliability_forecasting", project_id=pid
            ):
                summary.setdefault("skipped_projects", []).append(str(pid))
                continue
            targets: list[Optional[Any]] = environments.get(pid) or [None]
            for environment_id in targets:
                try:
                    result = await forecast_service.generate_for_project(
                        project_id=pid,
                        environment_id=environment_id,
                        now=now,
                    )
                except Exception as error:  # noqa: BLE001 - one project must not stop a run
                    logger.exception("forecast generation failed for project %s", pid)
                    summary["errors"].append(
                        f"project {pid}: {type(error).__name__}: {error}"
                    )
                    continue
                summary["forecasts_created"] += result.forecasts_created
                summary["forecasts_updated"] += result.forecasts_updated
                summary["forecasts_revised"] += result.forecasts_revised
                summary["refusals"] += result.refusals
                summary["signals"] += result.signals
                summary["errors"].extend(result.errors[:5])

        try:
            scored = await evaluation_service.evaluate_due(now=now)
            summary["scored"] = scored.get("scored", 0)
            summary["skipped_scores"] = scored.get("skipped", 0)
        except Exception as error:  # noqa: BLE001
            logger.exception("forecast evaluation sweep failed")
            summary["errors"].append(f"evaluation: {type(error).__name__}: {error}")

        try:
            warnings = await warning_service.evaluate(now=now)
            summary["warnings_raised"] = warnings.get("raised", 0)
            summary["warnings_updated"] = warnings.get("updated", 0)
            summary["warnings_suppressed"] = warnings.get("suppressed", 0)
            summary["warnings_expired"] = warnings.get("expired", 0)
        except Exception as error:  # noqa: BLE001
            logger.exception("early warning sweep failed")
            summary["errors"].append(f"warnings: {type(error).__name__}: {error}")

        #: Evaluation ran first, and it accepts EXPIRED forecasts, so ordering
        #: here only decides which label a scored forecast ends up carrying.
        try:
            summary["forecasts_expired"] = await forecast_service.expire_due(now=now)
        except Exception as error:  # noqa: BLE001
            logger.exception("forecast expiry sweep failed")
            summary["errors"].append(f"expiry: {type(error).__name__}: {error}")

        await session.commit()

    if any(
        summary[key]
        for key in (
            "forecasts_created",
            "scored",
            "warnings_raised",
            "forecasts_expired",
            "errors",
        )
    ):
        logger.info(
            "reliability sweep: projects=%s created=%s scored=%s warnings=%s "
            "expired=%s errors=%s",
            summary["projects"],
            summary["forecasts_created"],
            summary["scored"],
            summary["warnings_raised"],
            summary["forecasts_expired"],
            len(summary["errors"]),
        )
    return summary


async def sweep_reliability_forever(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run the sweep periodically until cancelled (§58).

    The interval comes from ``RELIABILITY_SWEEP_INTERVAL_SECONDS``, and the
    generation step deduplicates inside a refresh window of the same length, so
    a short interval changes responsiveness rather than producing more rows.
    """
    interval = max(60, settings.RELIABILITY_SWEEP_INTERVAL_SECONDS)
    while True:
        # One pass per interval across the fleet (see ``sweep_leader``).
        async with sweep_lease(session_factory, "reliability") as leader:
            if not leader:
                logger.debug("Reliability sweep: another worker holds the lease")
                await asyncio.sleep(interval)
                continue
            try:
                await sweep_reliability_once(session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the loop must survive
                logger.exception("reliability sweep failed: %s", error)
        await asyncio.sleep(interval)


__all__ = ["sweep_reliability_forever", "sweep_reliability_once"]
