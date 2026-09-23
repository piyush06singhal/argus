"""ARGUS Learning Sweep (Phase 10 §29, §63, §80).

The scheduled side of the learning pipeline. It decides *when* a run happens,
never *what* it concludes: it calls the same
:func:`~app.services.learning_run.execute_learning_run` an operator triggers by
hand, and it respects the same control plane every other ARGUS sweep does — a
Phase 9 pause on ``intelligence_sweep`` stops learning without stopping the
platform.

Three jobs, all bounded and idempotent:

* **Run the pipeline** for projects that have waiting events or whose last run is
  older than the configured interval.
* **Decay knowledge** that no longer receives confirming evidence, via the
  lifecycle's staleness rule (§25).
* **Expire recommendations** past their own deadlines, so a stale card cannot
  keep being read as current advice.

Nothing here activates knowledge. Activation is the lifecycle's decision under
§71–§73, and the sweep has no path to it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models.project import ProjectStatus, SoftwareProject
from app.services.intelligence_state import coerce_enum
from app.services.knowledge_lifecycle import refresh_staleness
from app.services.learning_run import (
    execute_learning_run,
    pending_event_count,
    run_is_due,
)
from app.services.recommendation_engine import ReliabilityRecommendationEngine

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """What one sweep pass did."""

    projects_considered: int = 0
    projects_run: int = 0
    events_consumed: int = 0
    knowledge_deprecated: int = 0
    recommendations_expired: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)
    paused: bool = False
    disabled: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "projects_considered": self.projects_considered,
            "projects_run": self.projects_run,
            "events_consumed": self.events_consumed,
            "knowledge_deprecated": self.knowledge_deprecated,
            "recommendations_expired": self.recommendations_expired,
            "runs": list(self.runs),
            "paused": self.paused,
            "disabled": self.disabled,
            "errors": list(self.errors),
        }


async def run_learning_sweep(
    session: AsyncSession,
    *,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    project_id: Optional[Any] = None,
    force: bool = False,
) -> SweepResult:
    """One pass of the scheduled learning work."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    result = SweepResult()

    if not settings.INTELLIGENCE_LEARNING_ENABLED:
        result.disabled = True
        return result

    #: Phase 9's control plane. A pause is a *result*, not an error: the platform
    #: was told to stop, and it stopped.
    from app.services.remediation_controls import safely_is_paused

    if await safely_is_paused(session, "intelligence_sweep", project_id=project_id):
        result.paused = True
        return result

    if project_id is not None:
        targets = [project_id]
    else:
        stmt = (
            select(SoftwareProject.id)
            .where(SoftwareProject.status != ProjectStatus.ARCHIVED)
            .limit(200)
        )
        targets = list((await session.scalars(stmt)).all())

    for target in targets:
        result.projects_considered += 1
        waiting = await pending_event_count(session, project_id=target)
        if not force:
            due = await run_is_due(
                session,
                project_id=target,
                interval_seconds=settings.INTELLIGENCE_SWEEP_INTERVAL_SECONDS,
                now=moment,
            )
            if not due and waiting == 0:
                continue
        try:
            summary = await execute_learning_run(
                session,
                project_id=target,
                trigger="schedule",
                now=moment,
                cutoff=moment,
                settings=settings,
            )
        except Exception as exc:  # pragma: no cover - failure path exercised in tests
            logger.warning("learning run failed for project %s", target, exc_info=True)
            result.errors.append(f"{target}: {exc}")
            continue
        result.projects_run += 1
        result.events_consumed += summary.events_processed
        result.runs.append(summary.as_dict())

    retired = await refresh_staleness(
        session, project_id=project_id, settings=settings, now=moment
    )
    result.knowledge_deprecated = len(retired)

    engine = ReliabilityRecommendationEngine(settings)
    expired = await engine.expire_stale(session, now=moment)
    result.recommendations_expired = len(expired)
    return result


async def sweep_forever(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run the sweep on an interval until cancelled (the app's lifespan does this)."""
    settings = get_settings()
    interval = max(30, settings.INTELLIGENCE_SWEEP_INTERVAL_SECONDS)
    logger.info("learning sweep started (interval=%ss)", interval)
    while True:
        try:
            async with session_factory() as session:
                result = await run_learning_sweep(session, settings=settings)
                await session.commit()
                if result.projects_run or result.knowledge_deprecated:
                    logger.info(
                        "learning sweep: projects=%d events=%d deprecated=%d expired=%d",
                        result.projects_run,
                        result.events_consumed,
                        result.knowledge_deprecated,
                        result.recommendations_expired,
                    )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            logger.info("learning sweep stopping")
            raise
        except Exception:  # pragma: no cover - never let a timer kill the app
            logger.warning("learning sweep pass failed", exc_info=True)
        await asyncio.sleep(interval)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


#: Re-exported so callers of the sweep do not need to import the lifecycle module
#: for the one enum they compare against.
__all__ = [
    "SweepResult",
    "coerce_enum",
    "run_learning_sweep",
    "sweep_forever",
]
