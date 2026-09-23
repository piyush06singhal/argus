"""ARGUS Platform Metrics (Phase 11 §73–§76, §85, §86).

The platform's own reliability numbers, with every definition written down.

§74 is the reason this module exists as more than a scalar: "MTTR = 20 minutes"
hides the fact that 14 of those minutes were spent waiting for a fix to be
verified, which is the only part an engineering team can act on. So the
recovery time is broken into the stages ARGUS can actually measure, each from
stored timestamps:

==============  ==========================================================
detection       first anomaly evidence → incident detected
triage          incident detected → acknowledged by a person
diagnosis       incident detected → last completed causal analysis
remediation     first remediation action created → first terminal outcome
verification    remediation verification started → verdict recorded
recovery        incident resolved → closed (or "still open")
total           incident detected → resolved
==============  ==========================================================

Two rules:

* **A stage with no evidence is reported as ``None``, never as zero.** A zero
  would read as "instant" and quietly improve the average.
* **Definitions are returned with the numbers.** Every metric here carries its
  methodology, because a metric without one becomes an argument instead of a
  measurement.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import aware as _aware
from app.models.anomaly import Anomaly
from app.models.causal import AnalysisStatus, CausalAnalysis
from app.models.incident import Incident
from app.models.remediation import (
    RemediationAction,
    RemediationStatus,
    RemediationVerification,
)

logger = logging.getLogger(__name__)

#: Terminal remediation states that count as "an outcome was reached".
TERMINAL_REMEDIATION_STATUSES = (
    RemediationStatus.VERIFIED,
    RemediationStatus.FAILED,
    RemediationStatus.ROLLED_BACK,
    RemediationStatus.REJECTED,
    RemediationStatus.CANCELLED,
    RemediationStatus.EXPIRED,
    RemediationStatus.BLOCKED,
)

MTTD_DEFINITION = (
    "mean time to detect = for each incident, (detected_at - earliest contributing "
    "anomaly detected_at). Incidents with no contributing anomaly are excluded, "
    "because a hand-reported incident has no detection latency to measure."
)

MTTR_DEFINITION = (
    "mean time to recover = (resolved_at - detected_at), over incidents resolved "
    "inside the window. Incidents still open are excluded from the mean and are "
    "reported separately as 'open', because an unfinished recovery would "
    "otherwise improve the number."
)

CHANGE_FAILURE_DEFINITION = (
    "see app.services.change_intelligence.CHANGE_FAILURE_METHODOLOGY"
)


def _seconds(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """Positive elapsed seconds, or ``None`` when either end is unknown.

    Clamps negatives to ``None`` rather than zero: timestamps that run backwards
    mean the data is wrong, and reporting that as "instant" would hide it.
    """
    aware_start = _aware(start)
    aware_end = _aware(end)
    if aware_start is None or aware_end is None:
        return None
    delta = (aware_end - aware_start).total_seconds()
    return delta if delta >= 0 else None


@dataclass
class RecoveryBreakdown:
    """One incident's measured lifecycle (§74)."""

    incident_id: str
    severity: Optional[str]
    detected_at: str
    resolved_at: Optional[str]
    detection_seconds: Optional[float] = None
    triage_seconds: Optional[float] = None
    diagnosis_seconds: Optional[float] = None
    remediation_seconds: Optional[float] = None
    verification_seconds: Optional[float] = None
    recovery_seconds: Optional[float] = None
    total_seconds: Optional[float] = None
    measured_stages: list[str] = field(default_factory=list)
    unmeasured_stages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "severity": self.severity,
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "detection_seconds": self.detection_seconds,
            "triage_seconds": self.triage_seconds,
            "diagnosis_seconds": self.diagnosis_seconds,
            "remediation_seconds": self.remediation_seconds,
            "verification_seconds": self.verification_seconds,
            "recovery_seconds": self.recovery_seconds,
            "total_seconds": self.total_seconds,
            "measured_stages": list(self.measured_stages),
            "unmeasured_stages": list(self.unmeasured_stages),
        }


async def incident_recovery_breakdown(
    session: AsyncSession,
    *,
    incident: Incident,
) -> RecoveryBreakdown:
    """Measure every stage of one incident's lifecycle (§74)."""
    detected_at = _aware(incident.detected_at)
    resolved_at = _aware(incident.resolved_at)
    breakdown = RecoveryBreakdown(
        incident_id=str(incident.id),
        severity=getattr(incident.severity, "value", str(incident.severity)),
        detected_at=detected_at.isoformat(),
        resolved_at=resolved_at.isoformat() if resolved_at else None,
    )

    # -- detection: earliest contributing anomaly
    first_anomaly = await session.scalar(
        select(func.min(Anomaly.detected_at)).where(Anomaly.incident_id == incident.id)
    )
    breakdown.detection_seconds = _seconds(first_anomaly, detected_at)

    # -- triage: acknowledged
    breakdown.triage_seconds = _seconds(
        detected_at, getattr(incident, "acknowledged_at", None)
    )

    # -- diagnosis: last completed causal analysis
    last_analysis = await session.scalar(
        select(func.max(CausalAnalysis.completed_at)).where(
            CausalAnalysis.incident_id == incident.id,
            CausalAnalysis.status == AnalysisStatus.COMPLETED,
        )
    )
    breakdown.diagnosis_seconds = _seconds(detected_at, last_analysis)

    # -- remediation: first action created → first terminal status
    first_action = (
        await session.scalars(
            select(RemediationAction)
            .where(RemediationAction.incident_id == incident.id)
            .order_by(RemediationAction.created_at)
            .limit(1)
        )
    ).first()
    if first_action is not None:
        start = getattr(first_action, "started_at", None) or first_action.created_at
        breakdown.remediation_seconds = _seconds(
            start, getattr(first_action, "completed_at", None)
        )

        # -- verification: started → verdict
        verification = (
            await session.scalars(
                select(RemediationVerification)
                .where(RemediationVerification.action_id == first_action.id)
                .order_by(RemediationVerification.created_at)
                .limit(1)
            )
        ).first()
        if verification is not None:
            #: A verification row carries its own observed window rather than a
            #: started/completed pair, so the window is the measurement.
            breakdown.verification_seconds = _seconds(
                verification.window_start, verification.window_end
            )
        breakdown.recovery_seconds = _seconds(
            getattr(first_action, "completed_at", None), resolved_at
        )

    breakdown.total_seconds = _seconds(detected_at, resolved_at)

    stages = {
        "detection": breakdown.detection_seconds,
        "triage": breakdown.triage_seconds,
        "diagnosis": breakdown.diagnosis_seconds,
        "remediation": breakdown.remediation_seconds,
        "verification": breakdown.verification_seconds,
        "recovery": breakdown.recovery_seconds,
    }
    breakdown.measured_stages = [
        name for name, value in stages.items() if value is not None
    ]
    breakdown.unmeasured_stages = [
        name for name, value in stages.items() if value is None
    ]
    return breakdown


async def time_to_recover_breakdown(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_days: int = 30,
    limit: int = 200,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """The aggregate §74 view: per-stage means, with their sample sizes.

    Each mean carries the count it was computed from. A mean of two incidents and
    a mean of ninety are different claims, and hiding that is how dashboards lie.
    """
    moment = _aware(now) or datetime.now(timezone.utc)
    window = timedelta(days=window_days)
    incidents = (
        await session.scalars(
            select(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.detected_at >= moment - window,
            )
            .order_by(Incident.detected_at.desc())
            .limit(limit)
        )
    ).all()

    resolved_ids: list[uuid.UUID] = []
    breakdowns: list[RecoveryBreakdown] = []
    open_incidents = 0
    for incident in incidents:
        breakdown = await incident_recovery_breakdown(session, incident=incident)
        breakdowns.append(breakdown)
        if breakdown.resolved_at is None:
            open_incidents += 1
        else:
            resolved_ids.append(incident.id)

    def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
        present = [value for value in values if value is not None]
        if not present:
            return None
        return round(sum(present) / len(present), 1)

    stages = {
        "detection": [b.detection_seconds for b in breakdowns],
        "triage": [b.triage_seconds for b in breakdowns],
        "diagnosis": [b.diagnosis_seconds for b in breakdowns],
        "remediation": [b.remediation_seconds for b in breakdowns],
        "verification": [b.verification_seconds for b in breakdowns],
        "recovery": [b.recovery_seconds for b in breakdowns],
    }
    stage_summary = {}
    for name, values in stages.items():
        present = [value for value in values if value is not None]
        stage_summary[name] = {
            "mean_seconds": _mean(values),
            "samples": len(present),
            "unmeasured": len(values) - len(present),
        }

    resolved_values = [
        b.total_seconds for b in breakdowns if b.total_seconds is not None
    ]
    return {
        "window_days": window_days,
        "incidents_considered": len(incidents),
        "incidents_resolved": len(resolved_ids),
        "incidents_open": open_incidents,
        "stages": stage_summary,
        "mttr_seconds": _mean(resolved_values),
        "mttr_samples": len(resolved_values),
        "slowest_incidents": [
            breakdown.as_dict()
            for breakdown in sorted(
                breakdowns,
                key=lambda b: b.total_seconds or -1,
                reverse=True,
            )[:5]
        ],
        "definitions": {"mttr": MTTR_DEFINITION, "stages": STAGE_DEFINITION},
        "limitations": [
            "stages without stored evidence are reported as null rather than "
            "zero, so a missing measurement never looks like a fast one"
        ]
        + (
            []
            if len(resolved_values) >= 5
            else [
                f"only {len(resolved_values)} incident(s) with a measured total, "
                "so these means are indicative rather than stable"
            ]
        ),
    }


STAGE_DEFINITION = (
    "detection = earliest contributing anomaly → incident detected; "
    "triage = detected → acknowledged; "
    "diagnosis = detected → last completed causal analysis; "
    "remediation = first action start → first terminal action status; "
    "verification = verification start → verdict; "
    "recovery = action completion → incident resolved"
)


async def mean_time_to_detect(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_days: int = 30,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """MTTD over a window (§85), with its methodology and sample size."""
    moment = _aware(now) or datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(
                Incident.id,
                Incident.detected_at,
                func.min(Anomaly.detected_at).label("first_anomaly"),
            )
            .join(Anomaly, Anomaly.incident_id == Incident.id, isouter=True)
            .where(
                Incident.project_id == project_id,
                Incident.detected_at >= moment - timedelta(days=window_days),
            )
            .group_by(Incident.id, Incident.detected_at)
            .limit(1000)
        )
    ).all()
    values: list[float] = []
    excluded = 0
    for _incident_id, detected_at, first_anomaly in rows:
        seconds = _seconds(first_anomaly, detected_at)
        if seconds is None:
            excluded += 1
            continue
        values.append(seconds)
    return {
        "window_days": window_days,
        "mttd_seconds": round(sum(values) / len(values), 1) if values else None,
        "samples": len(values),
        "excluded_without_anomaly": excluded,
        "definition": MTTD_DEFINITION,
    }


@dataclass
class ReliabilityScorecard:
    """The §75 component scorecard: explicit dimensions, no single score.

    §75 says not to collapse this into one number unless the methodology is
    documented, and the honest reading of that is: don't, because no defensible
    weighting exists for "availability vs. remediation success". So the scorecard
    returns its dimensions and the documentation, and leaves judgement to the
    reader.
    """

    component_id: uuid.UUID
    component_name: Optional[str]
    window_days: int
    dimensions: dict[str, Any] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    methodology: str = ""
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_id": str(self.component_id),
            "component_name": self.component_name,
            "window_days": self.window_days,
            "dimensions": self.dimensions,
            "unavailable": self.unavailable,
            "methodology": self.methodology,
            "limitations": list(self.limitations),
        }


SCORECARD_METHODOLOGY = (
    "Each dimension is measured independently over the window and reported with "
    "its own sample size. There is deliberately no composite score: weighting "
    "availability against remediation success is a policy decision, and a made-up "
    "weight would make the number look more authoritative than the evidence "
    "behind it."
)


async def component_scorecard(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    window_days: int = 30,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> ReliabilityScorecard:
    """Scorecard for one component (§75)."""
    from app.models.system import SystemComponent

    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    window = timedelta(days=window_days)
    component = await session.get(SystemComponent, component_id)
    scorecard = ReliabilityScorecard(
        component_id=component_id,
        component_name=component.name if component else None,
        window_days=window_days,
        methodology=SCORECARD_METHODOLOGY,
    )
    if component is None:
        scorecard.unavailable["component"] = "no such component"
        return scorecard

    # -- incident frequency
    incident_count = await session.scalar(
        select(func.count(Incident.id)).where(
            Incident.primary_component_id == component_id,
            Incident.detected_at >= moment - window,
        )
    )
    scorecard.dimensions["incident_frequency"] = {
        "incidents": int(incident_count or 0),
        "per_week": round((incident_count or 0) / (window_days / 7.0), 2),
    }

    # -- recovery time
    resolved = (
        await session.scalars(
            select(Incident).where(
                Incident.primary_component_id == component_id,
                Incident.resolved_at.is_not(None),
                Incident.detected_at >= moment - window,
            )
        )
    ).all()
    durations = [
        _seconds(incident.detected_at, incident.resolved_at) for incident in resolved
    ]
    present = [value for value in durations if value is not None]
    scorecard.dimensions["recovery_time"] = {
        "mean_seconds": round(sum(present) / len(present), 1) if present else None,
        "samples": len(present),
    }

    # -- anomalies
    anomaly_count = await session.scalar(
        select(func.count(Anomaly.id)).where(
            Anomaly.component_id == component_id,
            Anomaly.detected_at >= moment - window,
        )
    )
    scorecard.dimensions["anomaly_frequency"] = {"anomalies": int(anomaly_count or 0)}

    # -- prediction accuracy for this component
    try:
        from app.models.reliability import (
            ForecastOutcome,
            PredictionOutcomeType,
        )

        outcome_rows = (
            await session.execute(
                select(ForecastOutcome.outcome, func.count())
                .where(
                    ForecastOutcome.component_id == component_id,
                    ForecastOutcome.evaluated_at >= moment - window,
                )
                .group_by(ForecastOutcome.outcome)
            )
        ).all()
        counts = {
            getattr(outcome, "value", str(outcome)): int(count)
            for outcome, count in outcome_rows
        }
        answered = counts.get(
            PredictionOutcomeType.TRUE_POSITIVE.value, 0
        ) + counts.get(PredictionOutcomeType.FALSE_POSITIVE.value, 0)
        scorecard.dimensions["prediction_accuracy"] = {
            "by_outcome": counts,
            "precision": (
                round(
                    counts.get(PredictionOutcomeType.TRUE_POSITIVE.value, 0) / answered,
                    4,
                )
                if answered
                else None
            ),
            "samples": answered,
        }
    except Exception:  # pragma: no cover - forecasting is optional (§59)
        scorecard.unavailable["prediction_accuracy"] = "forecasting did not answer"

    # -- remediation success
    remediation_outcome_rows = (
        await session.execute(
            select(RemediationAction.outcome, func.count())
            .where(
                RemediationAction.component_id == component_id,
                RemediationAction.outcome.is_not(None),
                RemediationAction.created_at >= moment - window,
            )
            .group_by(RemediationAction.outcome)
        )
    ).all()
    remediation_counts = {
        getattr(outcome, "value", str(outcome)): int(count)
        for outcome, count in remediation_outcome_rows
    }
    decided = sum(remediation_counts.values())
    effective = remediation_counts.get("EFFECTIVE", 0) + remediation_counts.get(
        "PARTIALLY_EFFECTIVE", 0
    )
    scorecard.dimensions["remediation_success"] = {
        "by_outcome": remediation_counts,
        "success_rate": round(effective / decided, 4) if decided else None,
        "samples": decided,
    }

    # -- SLO compliance, when the component has objectives
    try:
        from app.services.slo_service import component_slo_status

        scorecard.dimensions["slo_compliance"] = await component_slo_status(
            session, component_id=component_id, settings=settings, now=moment
        )
    except Exception:  # pragma: no cover - SLOs are optional
        scorecard.unavailable["slo_compliance"] = "objectives could not be evaluated"

    # -- availability and latency come from telemetry the phases store; when
    #: there is none, the dimension is unavailable rather than zero.
    from app.models.observability import ObservabilityEvent

    telemetry_count = await session.scalar(
        select(func.count(ObservabilityEvent.id)).where(
            ObservabilityEvent.component_id == component_id,
            ObservabilityEvent.timestamp >= moment - window,
        )
    )
    scorecard.dimensions["telemetry_volume"] = {
        "events": int(telemetry_count or 0),
        "covered": bool(telemetry_count),
    }
    if not telemetry_count:
        scorecard.unavailable["availability"] = (
            "no telemetry in the window, so availability cannot be measured"
        )
        scorecard.unavailable["latency"] = (
            "no telemetry in the window, so latency cannot be measured"
        )
    scorecard.limitations.append(
        "a component with no telemetry has unavailable dimensions, not good ones"
    )
    return scorecard


__all__ = [
    "CHANGE_FAILURE_DEFINITION",
    "MTTD_DEFINITION",
    "MTTR_DEFINITION",
    "SCORECARD_METHODOLOGY",
    "STAGE_DEFINITION",
    "ReliabilityScorecard",
    "RecoveryBreakdown",
    "component_scorecard",
    "incident_recovery_breakdown",
    "mean_time_to_detect",
    "time_to_recover_breakdown",
]
