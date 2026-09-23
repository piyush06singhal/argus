"""ARGUS Component Reliability Profiles (Phase 10 §21, §22).

Historical facts about one component, over an explicit window: how many
incidents, how many anomalies, how often it was remediated, how often that was
rolled back, how long recovery took, how its forecasts scored.

Two design points:

* **Counts, not scores.** A profile is a tally of rows that exist. There is no
  hidden reliability score that could be mistaken for a measurement, and the
  window it covers is stored on the row so a stale profile is visibly stale.
* **The chronic signal recommends investigation.** ``chronic_signal`` is a
  boolean with human-readable ``chronic_reasons``. Nothing in this module
  disables, throttles or modifies a component (§22) — that decision belongs to a
  human with Phase 9's controls in front of them.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.anomaly import Anomaly
from app.models.fix import FixHypothesis, Patch, PatchVerificationRun
from app.models.incident import Incident
from app.models.intelligence import ComponentReliabilityProfile, ReliabilityExperience
from app.models.reliability import ForecastOutcome, ReliabilityForecast
from app.models.remediation import RemediationAction, RemediationStatus
from app.models.system import SystemComponent

logger = logging.getLogger(__name__)

#: Forecast outcomes that count as the forecast having been right. The column is
#: a plain string (Phase 8 stores ``PredictionOutcomeType`` values); "confirmed"
#: is a *status* in Phase 8, not an outcome, so it is deliberately absent here.
_TRUE_POSITIVE_OUTCOMES = frozenset({"TRUE_POSITIVE"})


async def compute_profile(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: uuid.UUID,
    window_days: int,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> ComponentReliabilityProfile:
    """Compute (or refresh) one component's profile for one window."""
    settings = settings or get_settings()
    moment = now or datetime.now(timezone.utc)
    window_start = moment - timedelta(days=window_days)

    incident_count = await _count(
        session,
        select(func.count(Incident.id))
        .where(Incident.project_id == project_id)
        .where(Incident.primary_component_id == component_id)
        .where(Incident.detected_at >= window_start)
        .where(Incident.detected_at <= moment),
    )
    anomaly_count = await _count(
        session,
        select(func.count(Anomaly.id))
        .where(Anomaly.project_id == project_id)
        .where(Anomaly.component_id == component_id)
        .where(Anomaly.detected_at >= window_start)
        .where(Anomaly.detected_at <= moment),
    )
    remediation_count = await _count(
        session,
        select(func.count(RemediationAction.id))
        .where(RemediationAction.project_id == project_id)
        .where(RemediationAction.component_id == component_id)
        .where(RemediationAction.created_at >= window_start)
        .where(RemediationAction.created_at <= moment),
    )
    rollback_count = await _count(
        session,
        select(func.count(RemediationAction.id))
        .where(RemediationAction.project_id == project_id)
        .where(RemediationAction.component_id == component_id)
        .where(RemediationAction.created_at >= window_start)
        .where(RemediationAction.created_at <= moment)
        .where(
            RemediationAction.status.in_(
                [RemediationStatus.ROLLED_BACK, RemediationStatus.ROLLING_BACK]
            )
        ),
    )
    regression_count = await _count(
        session,
        select(func.count(PatchVerificationRun.id))
        .join(Patch, Patch.id == PatchVerificationRun.patch_id)
        .join(FixHypothesis, FixHypothesis.id == Patch.fix_hypothesis_id)
        .join(Incident, Incident.id == FixHypothesis.incident_id)
        .where(Incident.primary_component_id == component_id)
        .where(PatchVerificationRun.completed_at >= window_start)
        .where(PatchVerificationRun.completed_at <= moment)
        .where(PatchVerificationRun.regression_detected.is_(True)),
    )
    mean_recovery = await session.scalar(
        select(func.avg(ReliabilityExperience.recovery_seconds))
        .where(ReliabilityExperience.project_id == project_id)
        .where(ReliabilityExperience.primary_component_id == component_id)
        .where(ReliabilityExperience.end_time >= window_start)
        .where(ReliabilityExperience.end_time <= moment)
    )
    forecast_outcome_count = await _count(
        session,
        select(func.count(ForecastOutcome.id))
        .join(
            ReliabilityForecast, ReliabilityForecast.id == ForecastOutcome.forecast_id
        )
        .where(ReliabilityForecast.project_id == project_id)
        .where(ForecastOutcome.component_id == component_id)
        .where(ForecastOutcome.evaluated_at >= window_start)
        .where(ForecastOutcome.evaluated_at <= moment),
    )
    forecast_true_positive = await _count(
        session,
        select(func.count(ForecastOutcome.id))
        .join(
            ReliabilityForecast, ReliabilityForecast.id == ForecastOutcome.forecast_id
        )
        .where(ReliabilityForecast.project_id == project_id)
        .where(ForecastOutcome.component_id == component_id)
        .where(ForecastOutcome.evaluated_at >= window_start)
        .where(ForecastOutcome.evaluated_at <= moment)
        .where(
            ForecastOutcome.outcome.in_([value for value in _TRUE_POSITIVE_OUTCOMES])
        ),
    )

    breakdown = await _breakdown(
        session,
        project_id=project_id,
        component_id=component_id,
        window_start=window_start,
        moment=moment,
    )
    chronic_reasons = _chronic_reasons(
        settings=settings,
        incident_count=incident_count,
        remediation_count=remediation_count,
        rollback_count=rollback_count,
        regression_count=regression_count,
        mean_recovery=float(mean_recovery) if mean_recovery is not None else None,
        window_days=window_days,
    )

    existing = await session.scalar(
        select(ComponentReliabilityProfile)
        .where(ComponentReliabilityProfile.project_id == project_id)
        .where(ComponentReliabilityProfile.component_id == component_id)
        .where(ComponentReliabilityProfile.window_days == window_days)
    )
    if existing is None:
        existing = ComponentReliabilityProfile(
            project_id=project_id,
            component_id=component_id,
            window_days=window_days,
            computed_at=moment,
        )
        session.add(existing)

    existing.computed_at = moment
    existing.incident_count = incident_count
    existing.anomaly_count = anomaly_count
    existing.remediation_count = remediation_count
    existing.rollback_count = rollback_count
    existing.regression_count = regression_count
    existing.mean_recovery_seconds = (
        float(mean_recovery) if mean_recovery is not None else None
    )
    existing.forecast_outcome_count = forecast_outcome_count
    existing.forecast_true_positive_count = forecast_true_positive
    existing.chronic_signal = bool(chronic_reasons)
    existing.chronic_reasons = chronic_reasons
    existing.breakdown = breakdown
    await session.flush()
    return existing


async def recompute_profiles(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_ids: Optional[Sequence[uuid.UUID]] = None,
    windows: Optional[Sequence[int]] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> list[ComponentReliabilityProfile]:
    """Recompute profiles for every component that has any history (§21)."""
    settings = settings or get_settings()
    resolved_windows = list(windows or settings.INTELLIGENCE_PROFILE_WINDOWS)

    if component_ids is None:
        stmt = select(SystemComponent.id).where(
            SystemComponent.project_id == project_id
        )
        component_ids = list((await session.scalars(stmt)).all())

    profiles: list[ComponentReliabilityProfile] = []
    for component_id in component_ids:
        for window_days in resolved_windows:
            profiles.append(
                await compute_profile(
                    session,
                    project_id=project_id,
                    component_id=component_id,
                    window_days=int(window_days),
                    now=now,
                    settings=settings,
                )
            )
    return profiles


async def _breakdown(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: uuid.UUID,
    window_start: datetime,
    moment: datetime,
) -> dict[str, Any]:
    """Raw counts behind the profile, so the UI can show where they came from."""
    anomaly_rows = (
        await session.execute(
            select(Anomaly.anomaly_type)
            .where(Anomaly.project_id == project_id)
            .where(Anomaly.component_id == component_id)
            .where(Anomaly.detected_at >= window_start)
            .where(Anomaly.detected_at <= moment)
            .limit(5000)
        )
    ).all()
    anomaly_types = Counter(
        str(getattr(row.anomaly_type, "value", row.anomaly_type))
        for row in anomaly_rows
    )

    outcome_rows = (
        await session.execute(
            select(
                ReliabilityExperience.outcome,
                ReliabilityExperience.data_quality,
            )
            .where(ReliabilityExperience.project_id == project_id)
            .where(ReliabilityExperience.primary_component_id == component_id)
            .where(ReliabilityExperience.end_time >= window_start)
            .where(ReliabilityExperience.end_time <= moment)
            .limit(5000)
        )
    ).all()
    outcomes = Counter(row.outcome for row in outcome_rows)
    quality = Counter(row.data_quality for row in outcome_rows)

    return {
        "anomaly_types": dict(sorted(anomaly_types.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "data_quality": dict(sorted(quality.items())),
        "episodes": len(outcome_rows),
    }


def _chronic_reasons(
    *,
    settings: Settings,
    incident_count: int,
    remediation_count: int,
    rollback_count: int,
    regression_count: int,
    mean_recovery: Optional[float],
    window_days: int,
) -> list[str]:
    """Why this component counts as chronically unreliable (§22).

    Every reason is a countable fact with its threshold, so the UI can show the
    rule that fired rather than an opaque verdict.
    """
    reasons: list[str] = []
    if incident_count >= settings.INTELLIGENCE_CHRONIC_INCIDENT_THRESHOLD:
        reasons.append(
            f"{incident_count} incidents in {window_days}d "
            f"(threshold {settings.INTELLIGENCE_CHRONIC_INCIDENT_THRESHOLD})"
        )
    if remediation_count >= settings.INTELLIGENCE_CHRONIC_REMEDIATION_THRESHOLD:
        reasons.append(
            f"{remediation_count} remediations in {window_days}d "
            f"(threshold {settings.INTELLIGENCE_CHRONIC_REMEDIATION_THRESHOLD})"
        )
    if rollback_count >= 2:
        reasons.append(f"{rollback_count} remediations were rolled back")
    if regression_count >= 2:
        reasons.append(f"{regression_count} verified patches regressed afterwards")
    if mean_recovery is not None and mean_recovery >= 3600:
        reasons.append(
            f"mean time to recovery is {mean_recovery / 3600:.1f}h, above the 1h threshold"
        )
    return reasons


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(stmt) or 0)


__all__ = [
    "compute_profile",
    "recompute_profiles",
]
