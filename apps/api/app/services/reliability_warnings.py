"""ARGUS Early Warning Engine (Phase 8 §39, §40, §51).

Turns elevated forecasts into a small number of warnings a human should look
at, and nothing more. No paging, no ticketing, no incident creation — the phase
stops at "warn, then a human decides" (§87).

Two mechanisms keep it from becoming a notification storm (§39, §40):

* **A warning is deduplicated by scope, not by forecast.** One warning exists
  per (component, environment, prediction type, horizon); a repeated sweep
  updates it — bumping ``occurrence_count`` and ``last_raised_at`` — instead of
  raising a duplicate. The fingerprint deliberately excludes the risk level so
  an escalation updates the same warning rather than forking it.
* **A cooldown suppresses repeats and records the suppression.** Inside the
  cooldown window the warning is touched but not counted as a new occurrence,
  and ``last_suppressed_at`` is written. Suppression that leaves no trace is
  indistinguishable from a warning that never fired.

A warning is only ever raised when the forecast's risk level is at least the
configured minimum *and* the level is not ``UNKNOWN``: an absence of evidence
never pages anyone (§39).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.reliability import (
    EarlyWarningStatus,
    ForecastHorizon,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionType,
    ReliabilityEarlyWarning,
    ReliabilityForecast,
)
from app.services.reliability_features import aware_utc
from app.services.reliability_risk import at_least, level_from_name, risk_statement

logger = logging.getLogger(__name__)
settings = get_settings()


def warning_fingerprint(
    *,
    project_id: Any,
    environment_id: Optional[Any],
    component_id: Optional[Any],
    prediction_type: PredictionType,
    horizon: ForecastHorizon,
) -> str:
    """Dedup key for one logical warning (§39).

    Excludes the risk level on purpose: escalating from HIGH to CRITICAL is the
    same warning getting worse, not a second event.
    """
    raw = "|".join(
        [
            str(project_id),
            str(environment_id or "-"),
            str(component_id or "-"),
            prediction_type.value,
            horizon.value,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def minimum_level() -> ForecastRiskLevel:
    """The configured floor for raising a warning."""
    return level_from_name(settings.RELIABILITY_WARNING_MIN_RISK_LEVEL)


class EarlyWarningService:
    """Raises, deduplicates and manages early warnings (§39, §40)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def evaluate(
        self,
        *,
        project_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        limit: int = 200,
    ) -> dict:
        """Consider live forecasts and raise or refresh warnings.

        Only forecasts that are currently live (``ACTIVE`` or ``CONFIRMED``)
        and unexpired are considered: warning about a horizon that already
        passed would be advice nobody can act on.
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        floor = minimum_level()
        clauses = [
            ReliabilityForecast.status.in_(
                [ForecastStatus.ACTIVE, ForecastStatus.CONFIRMED]
            ),
            ReliabilityForecast.valid_until > now,
            ReliabilityForecast.risk_level.notin_([ForecastRiskLevel.UNKNOWN]),
            ReliabilityForecast.risk_level.in_(
                [ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL]
            ),
        ]
        if project_id is not None:
            clauses.append(ReliabilityForecast.project_id == project_id)

        forecasts = (
            (
                await self.session.execute(
                    select(ReliabilityForecast)
                    .where(*clauses)
                    .order_by(ReliabilityForecast.generated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        summary = {
            "candidates": len(forecasts),
            "raised": 0,
            "updated": 0,
            "suppressed": 0,
            "below_floor": 0,
            "expired": 0,
        }
        for forecast in forecasts:
            if not at_least(forecast.risk_level, floor):
                summary["below_floor"] += 1
                continue
            outcome = await self._raise(forecast, now=now)
            summary[outcome] += 1
        summary["expired"] += await self.expire_due(now=now, project_id=project_id)
        await self.session.flush()
        return summary

    async def _raise(self, forecast: ReliabilityForecast, *, now: datetime) -> str:
        """Create, refresh or suppress the warning for one forecast."""
        fingerprint = warning_fingerprint(
            project_id=forecast.project_id,
            environment_id=forecast.environment_id,
            component_id=forecast.component_id,
            prediction_type=forecast.prediction_type,
            horizon=forecast.forecast_horizon,
        )
        existing = (
            (
                await self.session.execute(
                    select(ReliabilityEarlyWarning).where(
                        ReliabilityEarlyWarning.project_id == forecast.project_id,
                        ReliabilityEarlyWarning.fingerprint == fingerprint,
                    )
                )
            )
            .scalars()
            .first()
        )
        cooldown = timedelta(seconds=settings.RELIABILITY_WARNING_COOLDOWN_SECONDS)
        subject = await self._subject(forecast)

        if existing is None:
            self.session.add(
                ReliabilityEarlyWarning(
                    project_id=forecast.project_id,
                    environment_id=forecast.environment_id,
                    component_id=forecast.component_id,
                    forecast_id=forecast.id,
                    fingerprint=fingerprint,
                    title=risk_statement(forecast.risk_level, subject),
                    description=forecast.headline,
                    severity=forecast.risk_level,
                    status=EarlyWarningStatus.OPEN,
                    occurrence_count=1,
                    first_raised_at=now,
                    last_raised_at=now,
                    metadata_={
                        "prediction_type": forecast.prediction_type.value,
                        "forecast_horizon": forecast.forecast_horizon.value,
                        "risk_score": forecast.risk_score,
                        "dominant_signal": forecast.dominant_signal,
                    },
                )
            )
            return "raised"

        existing.forecast_id = forecast.id
        existing.title = risk_statement(forecast.risk_level, subject)
        existing.description = forecast.headline
        existing.severity = forecast.risk_level
        existing.metadata_ = {
            **(existing.metadata_ or {}),
            "prediction_type": forecast.prediction_type.value,
            "forecast_horizon": forecast.forecast_horizon.value,
            "risk_score": forecast.risk_score,
            "dominant_signal": forecast.dominant_signal,
        }
        if existing.status is EarlyWarningStatus.DISMISSED:
            #: A dismissed warning stays dismissed while the condition persists;
            #: re-raising it would ignore the human decision.
            existing.last_raised_at = now
            return "suppressed"
        if (now - aware_utc(existing.last_raised_at)) < cooldown:
            existing.last_suppressed_at = now
            return "suppressed"
        existing.occurrence_count += 1
        existing.last_raised_at = now
        existing.status = EarlyWarningStatus.OPEN
        return "updated"

    async def _subject(self, forecast: ReliabilityForecast) -> str:
        """A human-readable subject: the component name where one exists."""
        if forecast.component_id is None:
            return "this project"
        from app.models.system import SystemComponent

        name = (
            await self.session.execute(
                select(SystemComponent.name).where(
                    SystemComponent.id == forecast.component_id
                )
            )
        ).scalar_one_or_none()
        return str(name) if name else "this component"

    async def expire_due(
        self, *, now: Optional[datetime] = None, project_id: Optional[Any] = None
    ) -> int:
        """Close warnings whose underlying forecast has expired.

        A warning is advice about a window; once the window is gone the warning
        is stale, and leaving it OPEN would inflate every open-warning count.
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        clauses = [
            ReliabilityEarlyWarning.status.in_(
                [EarlyWarningStatus.OPEN, EarlyWarningStatus.ACKNOWLEDGED]
            ),
            ReliabilityEarlyWarning.forecast_id.is_not(None),
        ]
        if project_id is not None:
            clauses.append(ReliabilityEarlyWarning.project_id == project_id)
        warnings = (
            (
                await self.session.execute(
                    select(ReliabilityEarlyWarning).where(*clauses).limit(500)
                )
            )
            .scalars()
            .all()
        )
        if not warnings:
            return 0
        forecast_ids = [w.forecast_id for w in warnings if w.forecast_id]
        rows = (
            await self.session.execute(
                select(ReliabilityForecast.id, ReliabilityForecast.status).where(
                    ReliabilityForecast.id.in_(forecast_ids)
                )
            )
        ).all()
        status_by_id = {row[0]: row[1] for row in rows}
        expired = 0
        for warning in warnings:
            status = status_by_id.get(warning.forecast_id)
            if status in (
                ForecastStatus.EXPIRED,
                ForecastStatus.FALSE_POSITIVE,
                ForecastStatus.INCONCLUSIVE,
            ):
                warning.status = EarlyWarningStatus.EXPIRED
                expired += 1
        await self.session.flush()
        return expired

    async def acknowledge(
        self, *, warning_id: Any, actor: Optional[str] = None
    ) -> Optional[ReliabilityEarlyWarning]:
        """Record a human acknowledgement. The only state change a human makes."""
        warning = (
            await self.session.execute(
                select(ReliabilityEarlyWarning).where(
                    ReliabilityEarlyWarning.id == warning_id
                )
            )
        ).scalar_one_or_none()
        if warning is None:
            return None
        warning.status = EarlyWarningStatus.ACKNOWLEDGED
        warning.acknowledged_at = datetime.now(timezone.utc)
        warning.acknowledged_by = actor
        await self.session.flush()
        return warning

    async def dismiss(
        self,
        *,
        warning_id: Any,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Optional[ReliabilityEarlyWarning]:
        """Record a human dismissal, with the reason kept for the audit trail."""
        warning = (
            await self.session.execute(
                select(ReliabilityEarlyWarning).where(
                    ReliabilityEarlyWarning.id == warning_id
                )
            )
        ).scalar_one_or_none()
        if warning is None:
            return None
        warning.status = EarlyWarningStatus.DISMISSED
        warning.acknowledged_at = datetime.now(timezone.utc)
        warning.acknowledged_by = actor
        warning.metadata_ = {
            **(warning.metadata_ or {}),
            "dismissed_reason": reason,
        }
        await self.session.flush()
        return warning

    async def list_warnings(
        self,
        *,
        project_id: Optional[Any] = None,
        status: Optional[EarlyWarningStatus] = None,
        minimum_severity: Optional[ForecastRiskLevel] = None,
        limit: int = 100,
    ) -> list[ReliabilityEarlyWarning]:
        clauses = []
        if project_id is not None:
            clauses.append(ReliabilityEarlyWarning.project_id == project_id)
        if status is not None:
            clauses.append(ReliabilityEarlyWarning.status == status)
        if minimum_severity is not None:
            clauses.append(
                ReliabilityEarlyWarning.severity.in_(_levels_at_least(minimum_severity))
            )
        return list(
            (
                await self.session.execute(
                    select(ReliabilityEarlyWarning)
                    .where(*clauses)
                    .order_by(
                        ReliabilityEarlyWarning.last_raised_at.desc(),
                        ReliabilityEarlyWarning.severity.desc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )


def _levels_at_least(minimum: ForecastRiskLevel) -> list[ForecastRiskLevel]:
    """Risk levels meeting a floor, in ascending order."""
    order = [
        ForecastRiskLevel.LOW,
        ForecastRiskLevel.MEDIUM,
        ForecastRiskLevel.HIGH,
        ForecastRiskLevel.CRITICAL,
    ]
    if minimum is ForecastRiskLevel.UNKNOWN:
        return order
    return [level for level in order if at_least(level, minimum)]


__all__ = [
    "EarlyWarningService",
    "minimum_level",
    "warning_fingerprint",
]
