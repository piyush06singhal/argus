"""ARGUS Reliability Drift Monitoring (Phase 8 §41, §42).

Watches for the ways a forecasting system goes quietly wrong: the features
shift, the predictions shift, the outcomes shift, the calibration slips, or the
data itself stops arriving properly.

The rule that shapes every line here:

    **Drift flags for review. It never retrains and never activates a model.**

There is deliberately no code path from a drift record to a model change. A
``FLAGGED`` record means a human should look; the model lifecycle in
:mod:`app.services.reliability_models` is the only place a status changes, and
nothing in this module calls it.

The other rule is §42's: **a data-pipeline failure must not read as an
improvement.** A drop in telemetry volume, a rise in missing features and stale
forecasts are all measured and reported as data drift, so "the numbers got
quieter because ingestion broke" is visible rather than mistaken for calm.

Every statistic is a normalized, documented quantity rather than a fitted
detector:

* **relative mean shift** — ``|mean_current - mean_reference| / (|mean_reference| + eps)``
* **missing-feature rate** — share of snapshots where the feature had no value
* **share shift** — change in the proportion of forecasts at or above a level
* **outcome-rate shift** — change in the observed event rate
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.observability import MetricRecord
from app.models.reliability import (
    DriftKind,
    DriftStatus,
    ForecastDataQuality,
    ForecastFeatureSnapshot,
    ForecastOutcome,
    ForecastRiskLevel,
    PredictionOutcomeType,
    ReliabilityDriftRecord,
    ReliabilityEvaluationRun,
    ReliabilityForecast,
)
from app.services.reliability_features import aware_utc

logger = logging.getLogger(__name__)
settings = get_settings()

_EPSILON = 1e-12

#: Features whose drift is tracked. Each is a canonical feature emitted by the
#: feature engine, so a shift here means the *inputs* to prediction moved.
TRACKED_FEATURES: tuple[str, ...] = (
    "latency_p95_current",
    "latency_p95_mean",
    "latency_p95_slope",
    "error_rate_current",
    "error_rate_mean",
    "cpu_utilization_current",
    "memory_utilization_current",
    "queue_depth_current",
    "resource_saturation_rate",
    "component_anomaly_density",
    "incidents_last_7d",
    "deployments_last_24h",
    "telemetry_staleness_seconds",
)


def relative_mean_shift(
    reference: Sequence[float], current: Sequence[float]
) -> Optional[float]:
    """Deterministic relative shift between two samples.

    ``None`` when either side is empty, or when the reference mean is ~0 and so
    a ratio would be meaningless — an absent reference is not zero drift.
    """
    if not reference or not current:
        return None
    mean_ref = sum(reference) / len(reference)
    mean_cur = sum(current) / len(current)
    if abs(mean_ref) <= _EPSILON:
        return None
    return abs(mean_cur - mean_ref) / abs(mean_ref)


def status_for(score: Optional[float]) -> DriftStatus:
    """Map a drift score onto the configured watch/flag bands."""
    if score is None:
        return DriftStatus.STABLE
    if score >= settings.RELIABILITY_DRIFT_FLAG_THRESHOLD:
        return DriftStatus.FLAGGED
    if score >= settings.RELIABILITY_DRIFT_WATCH_THRESHOLD:
        return DriftStatus.WATCH
    return DriftStatus.STABLE


def missing_rate(values: Sequence[Optional[float]]) -> Optional[float]:
    """Share of values that are missing, or ``None`` for an empty sample."""
    total = len(values)
    if not total:
        return None
    return sum(1 for value in values if value is None) / total


@dataclass
class DriftFinding:
    """One evaluated drift statistic, before it is persisted."""

    kind: DriftKind
    status: DriftStatus
    description: str
    drift_score: Optional[float] = None
    threshold: Optional[float] = None
    feature_name: Optional[str] = None
    reference_count: int = 0
    current_count: int = 0
    reference_window: tuple[Optional[datetime], Optional[datetime]] = (None, None)
    current_window: tuple[Optional[datetime], Optional[datetime]] = (None, None)
    metadata: dict = field(default_factory=dict)

    @property
    def requires_review(self) -> bool:
        return self.status is DriftStatus.FLAGGED


@dataclass
class DriftReport:
    """Everything one assessment pass concluded."""

    project_id: Any
    reference_window: tuple[datetime, datetime]
    current_window: tuple[datetime, datetime]
    findings: list[DriftFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> list[DriftFinding]:
        return [
            finding
            for finding in self.findings
            if finding.status is DriftStatus.FLAGGED
        ]

    @property
    def worst_status(self) -> DriftStatus:
        order = {
            DriftStatus.STABLE: 0,
            DriftStatus.WATCH: 1,
            DriftStatus.FLAGGED: 2,
        }
        if not self.findings:
            return DriftStatus.STABLE
        return max(self.findings, key=lambda f: order[f.status]).status

    def as_dict(self) -> dict:
        return {
            "project_id": str(self.project_id),
            "reference_window": [
                moment.isoformat() for moment in self.reference_window
            ],
            "current_window": [moment.isoformat() for moment in self.current_window],
            "worst_status": self.worst_status.value,
            "flagged_count": len(self.flagged),
            "findings": [
                {
                    "kind": finding.kind.value,
                    "status": finding.status.value,
                    "feature_name": finding.feature_name,
                    "drift_score": finding.drift_score,
                    "threshold": finding.threshold,
                    "description": finding.description,
                    "requires_review": finding.requires_review,
                    "reference_count": finding.reference_count,
                    "current_count": finding.current_count,
                }
                for finding in self.findings
            ],
            "notes": self.notes,
            "review_policy": (
                "a FLAGGED finding requests human review of the model or the "
                "pipeline; this service never retrains, activates or retires a "
                "model"
            ),
        }


class DriftMonitor:
    """Evaluates model and data drift over stored snapshots and outcomes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        persist: bool = True,
    ) -> DriftReport:
        """Assess every drift family and (optionally) store the findings."""
        now = aware_utc(now or datetime.now(timezone.utc))
        reference_window = (
            now
            - timedelta(seconds=settings.RELIABILITY_DRIFT_REFERENCE_WINDOW_SECONDS),
            now - timedelta(seconds=settings.RELIABILITY_DRIFT_CURRENT_WINDOW_SECONDS),
        )
        current_window = (
            reference_window[1],
            now,
        )
        report = DriftReport(
            project_id=project_id,
            reference_window=reference_window,
            current_window=current_window,
        )

        report.findings.extend(
            await self.feature_drift(
                project_id=project_id,
                environment_id=environment_id,
                reference_window=reference_window,
                current_window=current_window,
            )
        )
        report.findings.extend(
            await self.data_drift(
                project_id=project_id,
                environment_id=environment_id,
                reference_window=reference_window,
                current_window=current_window,
            )
        )
        report.findings.extend(
            await self.prediction_drift(
                project_id=project_id,
                reference_window=reference_window,
                current_window=current_window,
            )
        )
        report.findings.extend(
            await self.outcome_drift(
                project_id=project_id,
                reference_window=reference_window,
                current_window=current_window,
            )
        )
        report.findings.extend(await self.calibration_drift(project_id=project_id))

        if not report.findings:
            report.notes.append(
                "no drift statistic could be computed: there were not enough "
                "stored snapshots or outcomes in the compared windows"
            )
        report.notes.append(
            "drift is measured between "
            f"{reference_window[0].isoformat()} and {current_window[1].isoformat()}"
        )
        if persist and report.findings:
            await self._persist(report, environment_id=environment_id)
        return report

    # -- feature + data drift (§41, §42) -------------------------------
    async def feature_drift(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any],
        reference_window: tuple[datetime, datetime],
        current_window: tuple[datetime, datetime],
    ) -> list[DriftFinding]:
        """Per-feature distribution shift, plus missing-feature rate (§42)."""
        reference = await self._snapshot_values(
            project_id=project_id,
            environment_id=environment_id,
            window=reference_window,
        )
        current = await self._snapshot_values(
            project_id=project_id,
            environment_id=environment_id,
            window=current_window,
        )
        if not reference or not current:
            return []

        findings: list[DriftFinding] = []
        for feature in TRACKED_FEATURES:
            ref_values = [entry.get(feature) for entry in reference]
            cur_values = [entry.get(feature) for entry in current]
            ref_present = [v for v in ref_values if v is not None]
            cur_present = [v for v in cur_values if v is not None]
            if not ref_present or not cur_present:
                continue

            score = relative_mean_shift(ref_present, cur_present)
            status = status_for(score)
            findings.append(
                DriftFinding(
                    kind=DriftKind.FEATURE_DRIFT,
                    status=status,
                    feature_name=feature,
                    drift_score=score,
                    threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                    description=(
                        f"feature {feature} shifted {score:.1%} relative to the "
                        f"reference window ({len(ref_present)} vs "
                        f"{len(cur_present)} samples)"
                        if score is not None
                        else f"feature {feature} could not be compared"
                    ),
                    reference_count=len(ref_present),
                    current_count=len(cur_present),
                    reference_window=reference_window,
                    current_window=current_window,
                )
            )

            #: Missing-rate drift is reported separately because it means
            #: something different: not "the value moved" but "we stopped
            #: receiving it" (§42).
            ref_missing = missing_rate(ref_values)
            cur_missing = missing_rate(cur_values)
            if (
                ref_missing is not None
                and cur_missing is not None
                and cur_missing > ref_missing
            ):
                delta = cur_missing - ref_missing
                finding_status = (
                    DriftStatus.FLAGGED
                    if delta >= settings.RELIABILITY_DRIFT_FLAG_THRESHOLD
                    else DriftStatus.WATCH
                    if delta >= settings.RELIABILITY_DRIFT_WATCH_THRESHOLD
                    else DriftStatus.STABLE
                )
                findings.append(
                    DriftFinding(
                        kind=DriftKind.DATA_DRIFT,
                        status=finding_status,
                        feature_name=feature,
                        drift_score=delta,
                        threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                        description=(
                            f"feature {feature} is missing in {cur_missing:.0%} of "
                            f"recent snapshots, up from {ref_missing:.0%}"
                        ),
                        reference_count=len(ref_values),
                        current_count=len(cur_values),
                        reference_window=reference_window,
                        current_window=current_window,
                    )
                )
        return findings

    async def data_drift(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any],
        reference_window: tuple[datetime, datetime],
        current_window: tuple[datetime, datetime],
    ) -> list[DriftFinding]:
        """Telemetry volume and stale-forecast share (§42).

        A collapse in ingestion is the classic false "everything improved"
        signal: fewer samples, fewer anomalies, lower risk — because nothing is
        arriving. It is measured explicitly.
        """
        findings: list[DriftFinding] = []
        ref_volume = await self._telemetry_volume(
            project_id=project_id,
            environment_id=environment_id,
            window=reference_window,
        )
        cur_volume = await self._telemetry_volume(
            project_id=project_id,
            environment_id=environment_id,
            window=current_window,
        )
        if ref_volume or cur_volume:
            ref_days = max(
                (reference_window[1] - reference_window[0]).total_seconds() / 86400,
                1e-6,
            )
            cur_days = max(
                (current_window[1] - current_window[0]).total_seconds() / 86400,
                1e-6,
            )
            ref_rate = ref_volume / ref_days
            cur_rate = cur_volume / cur_days
            score = (
                abs(cur_rate - ref_rate) / abs(ref_rate)
                if abs(ref_rate) > _EPSILON
                else None
            )
            status = status_for(score)
            direction = "fell" if cur_rate < ref_rate else "rose"
            findings.append(
                DriftFinding(
                    kind=DriftKind.DATA_DRIFT,
                    status=status,
                    drift_score=score,
                    threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                    feature_name="telemetry_volume",
                    description=(
                        f"telemetry volume {direction} from {ref_rate:.1f} to "
                        f"{cur_rate:.1f} samples per day"
                    ),
                    reference_count=ref_volume,
                    current_count=cur_volume,
                    reference_window=reference_window,
                    current_window=current_window,
                    metadata={"reference_rate": ref_rate, "current_rate": cur_rate},
                )
            )

        ref_stale = await self._stale_share(
            project_id=project_id, window=reference_window
        )
        cur_stale = await self._stale_share(
            project_id=project_id, window=current_window
        )
        if ref_stale is not None and cur_stale is not None and cur_stale > ref_stale:
            delta = cur_stale - ref_stale
            findings.append(
                DriftFinding(
                    kind=DriftKind.DATA_DRIFT,
                    status=(
                        DriftStatus.FLAGGED
                        if delta >= settings.RELIABILITY_DRIFT_FLAG_THRESHOLD
                        else DriftStatus.WATCH
                    ),
                    drift_score=delta,
                    threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                    feature_name="poor_data_quality_share",
                    description=(
                        f"forecasts built on insufficient or poor data rose from "
                        f"{ref_stale:.0%} to {cur_stale:.0%} of the window"
                    ),
                    reference_window=reference_window,
                    current_window=current_window,
                )
            )
        return findings

    # -- prediction, outcome, calibration drift (§41) ------------------
    async def prediction_drift(
        self,
        *,
        project_id: Any,
        reference_window: tuple[datetime, datetime],
        current_window: tuple[datetime, datetime],
    ) -> list[DriftFinding]:
        """Shift in the share of elevated forecasts."""
        ref_share, ref_count = await self._high_risk_share(
            project_id=project_id, window=reference_window
        )
        cur_share, cur_count = await self._high_risk_share(
            project_id=project_id, window=current_window
        )
        if ref_count == 0 or cur_count == 0 or ref_share is None or cur_share is None:
            return []
        delta = abs(cur_share - ref_share)
        if delta < settings.RELIABILITY_DRIFT_WATCH_THRESHOLD:
            return []
        return [
            DriftFinding(
                kind=DriftKind.PREDICTION_DRIFT,
                status=status_for(delta),
                drift_score=delta,
                threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                feature_name="elevated_forecast_share",
                description=(
                    f"the share of high/critical forecasts moved from "
                    f"{ref_share:.0%} to {cur_share:.0%}"
                ),
                reference_count=ref_count,
                current_count=cur_count,
                reference_window=reference_window,
                current_window=current_window,
            )
        ]

    async def outcome_drift(
        self,
        *,
        project_id: Any,
        reference_window: tuple[datetime, datetime],
        current_window: tuple[datetime, datetime],
    ) -> list[DriftFinding]:
        """Shift in the observed event rate — the ground truth moving."""
        ref_rate, ref_count = await self._outcome_rate(
            project_id=project_id, window=reference_window
        )
        cur_rate, cur_count = await self._outcome_rate(
            project_id=project_id, window=current_window
        )
        if ref_rate is None or cur_rate is None or ref_count == 0 or cur_count == 0:
            return []
        delta = abs(cur_rate - ref_rate)
        if delta < settings.RELIABILITY_DRIFT_WATCH_THRESHOLD:
            return []
        return [
            DriftFinding(
                kind=DriftKind.OUTCOME_DRIFT,
                status=status_for(delta),
                drift_score=delta,
                threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                feature_name="observed_event_rate",
                description=(
                    f"the observed event rate moved from {ref_rate:.0%} to "
                    f"{cur_rate:.0%} of scored forecasts"
                ),
                reference_count=ref_count,
                current_count=cur_count,
                reference_window=reference_window,
                current_window=current_window,
            )
        ]

    async def calibration_drift(self, *, project_id: Any) -> list[DriftFinding]:
        """Compare calibration between the two most recent evaluation runs."""
        runs = (
            (
                await self.session.execute(
                    select(ReliabilityEvaluationRun)
                    .where(ReliabilityEvaluationRun.project_id == project_id)
                    .order_by(ReliabilityEvaluationRun.created_at.desc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(runs) < 2:
            return []
        current, previous = runs[0], runs[1]
        cur_value = (current.calibration or {}).get("max_band_deviation")
        prev_value = (previous.calibration or {}).get("max_band_deviation")
        if cur_value is None or prev_value is None:
            return []
        delta = abs(cur_value - prev_value)
        if delta < settings.RELIABILITY_DRIFT_WATCH_THRESHOLD:
            return []
        status = status_for(delta)
        return [
            DriftFinding(
                kind=DriftKind.CALIBRATION_DRIFT,
                status=status,
                drift_score=delta,
                threshold=settings.RELIABILITY_DRIFT_FLAG_THRESHOLD,
                feature_name="max_band_deviation",
                description=(
                    f"calibration deviation moved from {prev_value:.2f} to "
                    f"{cur_value:.2f} between the last two evaluation runs"
                ),
                reference_count=previous.sample_count,
                current_count=current.sample_count,
                reference_window=(
                    aware_utc(previous.dataset_window_start),
                    aware_utc(previous.dataset_window_end),
                ),
                current_window=(
                    aware_utc(current.dataset_window_start),
                    aware_utc(current.dataset_window_end),
                ),
                metadata={
                    "previous_calibration_status": previous.calibration_status.value,
                    "current_calibration_status": current.calibration_status.value,
                },
            )
        ]

    # -- persistence and read -----------------------------------------
    async def _persist(
        self, report: DriftReport, *, environment_id: Optional[Any]
    ) -> None:
        """Store the findings. Records only — no model action follows."""
        for finding in report.findings:
            self.session.add(
                ReliabilityDriftRecord(
                    project_id=report.project_id,
                    environment_id=environment_id,
                    kind=finding.kind,
                    status=finding.status,
                    feature_name=finding.feature_name,
                    drift_score=finding.drift_score,
                    threshold=finding.threshold,
                    reference_window_start=finding.reference_window[0],
                    reference_window_end=finding.reference_window[1],
                    current_window_start=finding.current_window[0],
                    current_window_end=finding.current_window[1],
                    description=finding.description,
                    requires_review=finding.requires_review,
                    metadata_={
                        **finding.metadata,
                        "reference_count": finding.reference_count,
                        "current_count": finding.current_count,
                    },
                )
            )
        await self.session.flush()

    async def list_findings(
        self,
        *,
        project_id: Optional[Any] = None,
        kind: Optional[DriftKind] = None,
        requires_review: Optional[bool] = None,
        limit: int = 100,
    ) -> list[ReliabilityDriftRecord]:
        clauses = []
        if project_id is not None:
            clauses.append(ReliabilityDriftRecord.project_id == project_id)
        if kind is not None:
            clauses.append(ReliabilityDriftRecord.kind == kind)
        if requires_review is not None:
            clauses.append(ReliabilityDriftRecord.requires_review == requires_review)
        return list(
            (
                await self.session.execute(
                    select(ReliabilityDriftRecord)
                    .where(*clauses)
                    .order_by(ReliabilityDriftRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    # -- measurement helpers ------------------------------------------
    async def _snapshot_values(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any],
        window: tuple[datetime, datetime],
        limit: int = 500,
    ) -> list[dict]:
        clauses = [
            ForecastFeatureSnapshot.project_id == project_id,
            ForecastFeatureSnapshot.forecast_time >= window[0],
            ForecastFeatureSnapshot.forecast_time < window[1],
        ]
        if environment_id is not None:
            clauses.append(ForecastFeatureSnapshot.environment_id == environment_id)
        snapshots = (
            (
                await self.session.execute(
                    select(ForecastFeatureSnapshot.feature_values)
                    .where(*clauses)
                    .order_by(ForecastFeatureSnapshot.forecast_time.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        out: list[dict] = []
        for payload in snapshots:
            numeric = (payload or {}).get("numeric") or {}
            out.append(numeric)
        return out

    async def _telemetry_volume(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any],
        window: tuple[datetime, datetime],
    ) -> int:
        clauses = [
            MetricRecord.project_id == project_id,
            MetricRecord.timestamp >= window[0],
            MetricRecord.timestamp < window[1],
        ]
        if environment_id is not None:
            clauses.append(MetricRecord.environment_id == environment_id)
        return int(
            (
                await self.session.execute(
                    select(func.count()).select_from(MetricRecord).where(*clauses)
                )
            ).scalar()
            or 0
        )

    async def _stale_share(
        self, *, project_id: Any, window: tuple[datetime, datetime]
    ) -> Optional[float]:
        rows = (
            (
                await self.session.execute(
                    select(ReliabilityForecast.data_quality).where(
                        ReliabilityForecast.project_id == project_id,
                        ReliabilityForecast.generated_at >= window[0],
                        ReliabilityForecast.generated_at < window[1],
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        weak = {
            ForecastDataQuality.INSUFFICIENT,
            ForecastDataQuality.POOR,
        }
        return sum(1 for quality in rows if quality in weak) / len(rows)

    async def _high_risk_share(
        self, *, project_id: Any, window: tuple[datetime, datetime]
    ) -> tuple[Optional[float], int]:
        rows = (
            (
                await self.session.execute(
                    select(ReliabilityForecast.risk_level).where(
                        ReliabilityForecast.project_id == project_id,
                        ReliabilityForecast.generated_at >= window[0],
                        ReliabilityForecast.generated_at < window[1],
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None, 0
        elevated = {ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL}
        return sum(1 for level in rows if level in elevated) / len(rows), len(rows)

    async def _outcome_rate(
        self, *, project_id: Any, window: tuple[datetime, datetime]
    ) -> tuple[Optional[float], int]:
        rows = (
            (
                await self.session.execute(
                    select(ForecastOutcome.outcome).where(
                        ForecastOutcome.project_id == project_id,
                        ForecastOutcome.evaluated_at >= window[0],
                        ForecastOutcome.evaluated_at < window[1],
                        ForecastOutcome.outcome != PredictionOutcomeType.INCONCLUSIVE,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None, 0
        events = (
            PredictionOutcomeType.TRUE_POSITIVE,
            PredictionOutcomeType.FALSE_NEGATIVE,
        )
        return sum(1 for outcome in rows if outcome in events) / len(rows), len(rows)


def drift_summary(records: Sequence[ReliabilityDriftRecord]) -> dict:
    """Roll a set of stored findings into a platform-level summary (§43)."""
    total = len(records)
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for record in records:
        by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
        by_kind[record.kind.value] = by_kind.get(record.kind.value, 0) + 1
    return {
        "total": total,
        "by_status": by_status,
        "by_kind": by_kind,
        "requiring_review": sum(1 for record in records if record.requires_review),
        "policy": (
            "drift records request human review; they never modify a model or "
            "its status"
        ),
    }


__all__ = [
    "TRACKED_FEATURES",
    "DriftFinding",
    "DriftMonitor",
    "DriftReport",
    "drift_summary",
    "missing_rate",
    "relative_mean_shift",
    "status_for",
]
