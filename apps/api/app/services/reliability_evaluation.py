"""ARGUS Prediction Evaluation (Phase 8 §28, §29, §32, §33, §53).

Scores forecasts against what actually happened. This is the module that makes
Phase 8 falsifiable: without it, "ARGUS predicts reliability risk" is an
unchecked claim.

Four decisions carry the honesty of everything downstream:

**An outcome is a row, never an overwrite.** Re-evaluating writes a new
:class:`~app.models.reliability.ForecastOutcome`; an evaluation run is an
immutable record of one scoring pass. A result that later looks wrong can be
inspected rather than silently replaced (§32).

**A metric is withheld below the sample floor.** Precision from four forecasts
is noise dressed as a percentage, so ``metrics`` is omitted and the run is
marked ``INSUFFICIENT_SAMPLE`` with the count stated instead (§29, §53).

**The risk score is not described as a probability.** It is a bounded risk
score, so calibration compares *risk score* against *observed frequency* and
says so. A Brier score is reported only when a score existed, and the
reliability diagram is emitted as bands with their own sample counts (§33).

**An unevaluable forecast is INCONCLUSIVE, not a negative.** A forecast whose
risk level was ``UNKNOWN``, or whose data quality was ``INSUFFICIENT``, is
recorded as inconclusive and excluded from precision and recall — counting it
as "we were right to say nothing" would flatter the numbers (§28).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.anomaly import Anomaly, AnomalySeverity
from app.models.incident import Incident
from app.models.reliability import (
    CalibrationStatus,
    EvaluationStatus,
    ForecastDataQuality,
    ForecastHorizon,
    ForecastOutcome,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionOutcomeType,
    PredictionType,
    ReliabilityEvaluationRun,
    ReliabilityForecast,
)
from app.services.reliability_features import aware_utc

logger = logging.getLogger(__name__)
settings = get_settings()

#: Which observed evidence counts as a *reliability event* for a prediction
#: type. Incidents count for every type. Telemetry-shaped predictions also count
#: a matching anomaly, because for "is latency about to degrade" an anomaly is
#: the event the claim is about. Regression risk is explicitly incident-only:
#: a code regression that never caused an incident is not evidence the forecast
#: was wrong or right.
ANOMALY_TYPES_BY_PREDICTION: dict[PredictionType, tuple[str, ...]] = {
    PredictionType.LATENCY_RISK: ("LATENCY_SPIKE", "HEALTH_DEGRADATION"),
    PredictionType.ERROR_RATE_RISK: ("ERROR_RATE_SPIKE", "LOG_PATTERN_SPIKE"),
    PredictionType.FAILURE_RISK: (
        "ERROR_RATE_SPIKE",
        "LATENCY_SPIKE",
        "TRACE_FAILURE_SPIKE",
    ),
    PredictionType.AVAILABILITY_RISK: (
        "ERROR_RATE_SPIKE",
        "TRACE_FAILURE_SPIKE",
        "HEALTH_DEGRADATION",
    ),
    PredictionType.RESOURCE_EXHAUSTION_RISK: ("RESOURCE_USAGE_SPIKE",),
    PredictionType.DEPENDENCY_FAILURE_RISK: (
        "TRACE_FAILURE_SPIKE",
        "LATENCY_SPIKE",
        "HEALTH_DEGRADATION",
    ),
    PredictionType.INCIDENT_RISK: ("ERROR_RATE_SPIKE", "LATENCY_SPIKE"),
    PredictionType.RELIABILITY_DEGRADATION: (
        "LATENCY_SPIKE",
        "ERROR_RATE_SPIKE",
        "HEALTH_DEGRADATION",
        "THROUGHPUT_DROP",
    ),
    PredictionType.REGRESSION_RISK: (),
}

#: Severity at or above which an anomaly counts as an event for scoring. A LOW
#: anomaly is routine noise; a forecast should not be marked wrong for failing
#: to predict it.
COUNTABLE_ANOMALY_SEVERITIES = (
    AnomalySeverity.MEDIUM,
    AnomalySeverity.HIGH,
    AnomalySeverity.CRITICAL,
)

#: Calibration bands for the reliability diagram (§33).
CALIBRATION_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 0.2),
    (0.2, 0.4),
    (0.4, 0.6),
    (0.6, 0.8),
    (0.8, 1.01),
)


def positive_levels() -> set[ForecastRiskLevel]:
    """Risk levels that count as a positive prediction (§29)."""
    levels = set()
    for name in settings.RELIABILITY_POSITIVE_RISK_LEVELS:
        try:
            levels.add(ForecastRiskLevel(name))
        except ValueError:
            logger.warning("ignoring unknown positive risk level %r", name)
    return levels or {ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL}


@dataclass
class OutcomeResult:
    """One scored forecast, before it is persisted."""

    forecast_id: Any
    outcome: PredictionOutcomeType
    reason: str
    actual_event: Optional[str] = None
    actual_severity: Optional[str] = None
    time_to_event_seconds: Optional[int] = None
    matched_incident_id: Any = None
    matched_anomaly_id: Any = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


@dataclass
class MetricsSummary:
    """Aggregate metrics, with the counts that justify them (§29, §53)."""

    sample_count: int = 0
    evaluated_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    inconclusive_count: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    metrics: dict = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)
    calibration_status: CalibrationStatus = CalibrationStatus.UNKNOWN
    reliability_bands: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: EvaluationStatus = EvaluationStatus.COMPLETED

    def as_dict(self) -> dict:
        return {
            "sample_count": self.sample_count,
            "evaluated_count": self.evaluated_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "inconclusive_count": self.inconclusive_count,
            "confusion": {
                "true_positives": self.true_positives,
                "false_positives": self.false_positives,
                "true_negatives": self.true_negatives,
                "false_negatives": self.false_negatives,
            },
            "metrics": self.metrics,
            "calibration": self.calibration,
            "calibration_status": self.calibration_status.value,
            "reliability_bands": self.reliability_bands,
            "status": self.status.value,
            "notes": self.notes,
        }


def compute_metrics(outcomes: Sequence[ForecastOutcome]) -> MetricsSummary:
    """Aggregate scored outcomes into metrics, withholding thin samples.

    Every ratio is computed from the confusion counts, and any ratio whose
    denominator is zero is omitted rather than reported as ``0.0`` — "no
    positives were predicted" is not the same as "precision was zero".
    """
    summary = MetricsSummary()
    summary.sample_count = len(outcomes)
    if not outcomes:
        summary.status = EvaluationStatus.INSUFFICIENT_SAMPLE
        summary.notes.append("no scored forecasts were available to evaluate")
        return summary

    for outcome in outcomes:
        if outcome.outcome is PredictionOutcomeType.INCONCLUSIVE:
            summary.inconclusive_count += 1
        elif outcome.outcome is PredictionOutcomeType.TRUE_POSITIVE:
            summary.true_positives += 1
            summary.positive_count += 1
        elif outcome.outcome is PredictionOutcomeType.FALSE_POSITIVE:
            summary.false_positives += 1
            summary.positive_count += 1
        elif outcome.outcome is PredictionOutcomeType.TRUE_NEGATIVE:
            summary.true_negatives += 1
            summary.negative_count += 1
        elif outcome.outcome is PredictionOutcomeType.FALSE_NEGATIVE:
            summary.false_negatives += 1
            summary.negative_count += 1

    summary.evaluated_count = summary.sample_count - summary.inconclusive_count
    summary.status = EvaluationStatus.INSUFFICIENT_SAMPLE
    summary.notes.append(
        f"only {summary.evaluated_count} forecasts had a definitive outcome; "
        f"minimum {settings.RELIABILITY_MIN_EVALUATION_SAMPLE} for reporting metrics"
    )
    summary.notes.append(
        f"{summary.inconclusive_count} forecasts were inconclusive and are "
        "excluded from precision and recall"
    )
    if summary.evaluated_count < settings.RELIABILITY_MIN_EVALUATION_SAMPLE:
        return summary

    summary.status = EvaluationStatus.COMPLETED
    tp, fp = summary.true_positives, summary.false_positives
    tn, fn = summary.true_negatives, summary.false_negatives

    metrics: dict[str, Any] = {
        "precision": (tp / (tp + fp)) if (tp + fp) else None,
        "recall": (tp / (tp + fn)) if (tp + fn) else None,
        "false_positive_rate": (fp / (fp + tn)) if (fp + tn) else None,
        "false_negative_rate": (fn / (fn + tp)) if (fn + tp) else None,
        #: Share of evaluated forecasts that were definitive and actionable.
        "coverage": summary.evaluated_count / summary.sample_count,
        "sample_count": summary.evaluated_count,
    }
    lead_times = [
        outcome.time_to_event_seconds
        for outcome in outcomes
        if outcome.outcome is PredictionOutcomeType.TRUE_POSITIVE
        and outcome.time_to_event_seconds is not None
    ]
    metrics["average_lead_time_seconds"] = (
        sum(lead_times) / len(lead_times) if lead_times else None
    )
    metrics["lead_time_sample_count"] = len(lead_times)
    #: PR-AUC / ROC-AUC are deliberately absent: computing them needs a
    #: probability and a ranked threshold sweep, and this phase produces a
    #: bounded risk score. Reporting them would be inventing a number (§2, §29).
    metrics["pr_auc"] = None
    metrics["roc_auc"] = None
    summary.metrics = metrics

    summary.reliability_bands = _reliability_bands(outcomes)
    summary.calibration, summary.calibration_status = _calibration(
        outcomes, summary.reliability_bands
    )
    if summary.calibration_status is CalibrationStatus.UNKNOWN:
        summary.notes.append(
            "calibration could not be measured from the available risk scores"
        )
    return summary


def _reliability_bands(outcomes: Sequence[ForecastOutcome]) -> list[dict]:
    """Predicted risk band vs observed event frequency (§33).

    Only forecasts that carried a numeric risk score and a definitive outcome
    are counted; a band with no samples is reported with ``sample_count=0``
    rather than omitted, so a reader can see the gap.
    """
    bands: list[dict] = []
    for low, high in CALIBRATION_BANDS:
        members = [
            outcome
            for outcome in outcomes
            if outcome.predicted_risk_score is not None
            and low <= outcome.predicted_risk_score < high
            and outcome.outcome is not PredictionOutcomeType.INCONCLUSIVE
        ]
        positives = sum(
            1
            for outcome in members
            if outcome.outcome
            in (
                PredictionOutcomeType.TRUE_POSITIVE,
                PredictionOutcomeType.FALSE_NEGATIVE,
            )
        )
        bands.append(
            {
                "band_low": low,
                "band_high": min(high, 1.0),
                "sample_count": len(members),
                "observed_event_rate": (positives / len(members)) if members else None,
                "mean_predicted_risk": (
                    sum(outcome.predicted_risk_score or 0.0 for outcome in members)
                    / len(members)
                    if members
                    else None
                ),
            }
        )
    return bands


def _calibration(
    outcomes: Sequence[ForecastOutcome], bands: Sequence[dict]
) -> tuple[dict, CalibrationStatus]:
    """Measure whether the risk score tracks observed frequency (§33).

    Two declared statistics:

    * **Brier score** — mean squared error between the risk score and the
      binary outcome, over forecasts that had both. Low is better.
    * **max band deviation** — the largest gap between the mean predicted risk
      and the observed event rate in any band with enough members.

    Status bands are explicit: ``GOOD`` ≤ 0.10 deviation, ``ACCEPTABLE`` ≤ 0.20,
    ``POOR`` beyond that, ``UNKNOWN`` when there is not enough evidence. The
    wording everywhere says "risk score", because it is not a probability.
    """
    paired = [
        outcome
        for outcome in outcomes
        if outcome.predicted_risk_score is not None
        and outcome.outcome is not PredictionOutcomeType.INCONCLUSIVE
    ]
    if len(paired) < settings.RELIABILITY_MIN_EVALUATION_SAMPLE:
        return (
            {
                "insufficient_sample_size": True,
                "sample_count": len(paired),
                "note": (
                    "calibration is not reported below "
                    f"{settings.RELIABILITY_MIN_EVALUATION_SAMPLE} scored forecasts"
                ),
            },
            CalibrationStatus.UNKNOWN,
        )

    brier = sum(
        (
            (outcome.predicted_risk_score or 0.0)
            - (
                1.0
                if outcome.outcome
                in (
                    PredictionOutcomeType.TRUE_POSITIVE,
                    PredictionOutcomeType.FALSE_NEGATIVE,
                )
                else 0.0
            )
        )
        ** 2
        for outcome in paired
    ) / len(paired)

    populated = [band for band in bands if band["sample_count"] >= 5]
    deviations = [
        abs((band["mean_predicted_risk"] or 0.0) - (band["observed_event_rate"] or 0.0))
        for band in populated
    ]
    max_deviation = max(deviations) if deviations else None

    if max_deviation is None:
        status = CalibrationStatus.UNKNOWN
    elif max_deviation <= 0.10:
        status = CalibrationStatus.GOOD
    elif max_deviation <= 0.20:
        status = CalibrationStatus.ACCEPTABLE
    else:
        status = CalibrationStatus.POOR

    return (
        {
            "insufficient_sample_size": False,
            "sample_count": len(paired),
            "brier_score": brier,
            "max_band_deviation": max_deviation,
            "bands_with_enough_samples": len(populated),
            "measured_on": "risk_score",
            "limitations": (
                "the risk score is a bounded composite of predictive signals, "
                "not a calibrated probability; this measures how well it tracks "
                "observed event frequency, and Brier here is a score-accuracy "
                "measure rather than a probabilistic forecast assessment"
            ),
        },
        status,
    )


class PredictionEvaluationService:
    """Scores forecasts against observed outcomes (§28, §29)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- outcome assignment (§28, §27) ---------------------------------
    async def evaluate_due(
        self,
        *,
        project_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        limit: int = 500,
    ) -> dict:
        """Score every forecast whose horizon has elapsed and lacks an outcome.

        Bounded and idempotent: a forecast is scored once per pass, and the
        gate is "no outcome row yet", so re-running changes nothing.
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        grace = timedelta(seconds=settings.RELIABILITY_EVALUATION_GRACE_SECONDS)
        clauses = [
            ReliabilityForecast.valid_until <= now - grace,
            ReliabilityForecast.status.in_(
                [
                    ForecastStatus.GENERATED,
                    ForecastStatus.ACTIVE,
                    ForecastStatus.CONFIRMED,
                    ForecastStatus.INCONCLUSIVE,
                    ForecastStatus.EXPIRED,
                ]
            ),
        ]
        if project_id is not None:
            clauses.append(ReliabilityForecast.project_id == project_id)

        forecasts = (
            (
                await self.session.execute(
                    select(ReliabilityForecast)
                    .where(*clauses)
                    .order_by(ReliabilityForecast.valid_until.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        summary = {"candidates": len(forecasts), "scored": 0, "skipped": 0}
        for forecast in forecasts:
            existing = (
                await self.session.execute(
                    select(ForecastOutcome.id)
                    .where(ForecastOutcome.forecast_id == forecast.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                summary["skipped"] += 1
                continue
            result = await self.score(forecast, now=now, commit=False)
            if result is None:
                summary["skipped"] += 1
                continue
            summary["scored"] += 1
        await self.session.flush()
        return summary

    async def score(
        self,
        forecast: ReliabilityForecast,
        *,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> Optional[OutcomeResult]:
        """Score one forecast and persist the outcome + status transition."""
        now = aware_utc(now or datetime.now(timezone.utc))
        window_start = aware_utc(forecast.valid_from)
        window_end = aware_utc(forecast.valid_until)

        result = await self.classify(
            forecast, window_start=window_start, window_end=window_end, now=now
        )
        self.session.add(
            ForecastOutcome(
                forecast_id=forecast.id,
                project_id=forecast.project_id,
                environment_id=forecast.environment_id,
                component_id=forecast.component_id,
                evaluation_window_start=window_start,
                evaluation_window_end=window_end,
                outcome=result.outcome,
                actual_event=result.actual_event,
                actual_severity=result.actual_severity,
                time_to_event_seconds=result.time_to_event_seconds,
                matched_incident_id=result.matched_incident_id,
                matched_anomaly_id=result.matched_anomaly_id,
                predicted_risk_level=forecast.risk_level,
                predicted_risk_score=forecast.risk_score,
                evaluation_reason=result.reason,
                evaluated_at=now,
                metadata_={
                    "prediction_type": forecast.prediction_type.value,
                    "forecast_horizon": forecast.forecast_horizon.value,
                    "data_quality": forecast.data_quality.value,
                },
            )
        )

        if result.outcome is PredictionOutcomeType.TRUE_POSITIVE:
            forecast.status = ForecastStatus.CONFIRMED
        elif result.outcome is PredictionOutcomeType.FALSE_POSITIVE:
            forecast.status = ForecastStatus.FALSE_POSITIVE
        elif result.outcome is PredictionOutcomeType.INCONCLUSIVE:
            forecast.status = ForecastStatus.INCONCLUSIVE
        elif result.outcome is PredictionOutcomeType.FALSE_NEGATIVE:
            #: A miss is a real outcome and is recorded as such; the forecast
            #: stays CONFIRMED-free but is not a false positive either, so the
            #: lifecycle records it as INCONCLUSIVE *only* when it truly was.
            forecast.status = ForecastStatus.EXPIRED
        else:
            forecast.status = ForecastStatus.EXPIRED

        await self.session.flush()

        #: Phase 10 §6. A scored horizon is a learning event: whether the
        #: forecast came true is exactly what calibration knowledge is built
        #: from. Best-effort, so a learning-table problem cannot fail an
        #: evaluation that already succeeded.
        from app.services.learning_hooks import record_forecast_outcome

        outcome_row = await self.session.scalar(
            select(ForecastOutcome)
            .where(ForecastOutcome.forecast_id == forecast.id)
            .order_by(ForecastOutcome.evaluated_at.desc())
            .limit(1)
        )
        if outcome_row is not None:
            await record_forecast_outcome(
                self.session, outcome=outcome_row, forecast=forecast
            )

        if commit:
            await self.session.commit()
        return result

    async def classify(
        self,
        forecast: ReliabilityForecast,
        *,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> OutcomeResult:
        """Decide the outcome of one forecast from stored evidence (§28).

        Three gates run first, in order, because each makes the question
        unanswerable rather than negative:

        1. an ``UNKNOWN`` risk level had no claim to test;
        2. ``INSUFFICIENT`` data quality means the window itself is untrustworthy;
        3. a window that ends after ``now`` has not finished yet.
        """
        if forecast.risk_level is ForecastRiskLevel.UNKNOWN:
            return OutcomeResult(
                forecast_id=forecast.id,
                outcome=PredictionOutcomeType.INCONCLUSIVE,
                reason=(
                    "the forecast made no risk claim (risk level UNKNOWN), so "
                    "there is nothing to score"
                ),
                window_start=window_start,
                window_end=window_end,
            )
        if forecast.data_quality is ForecastDataQuality.INSUFFICIENT:
            return OutcomeResult(
                forecast_id=forecast.id,
                outcome=PredictionOutcomeType.INCONCLUSIVE,
                reason=(
                    "the forecast was produced from insufficient data, so its "
                    "window cannot be scored fairly"
                ),
                window_start=window_start,
                window_end=window_end,
            )
        if window_end > now:
            return OutcomeResult(
                forecast_id=forecast.id,
                outcome=PredictionOutcomeType.INCONCLUSIVE,
                reason="the forecast horizon has not elapsed yet",
                window_start=window_start,
                window_end=window_end,
            )

        event = await self._observed_event(
            forecast, window_start=window_start, window_end=window_end
        )
        predicted_positive = forecast.risk_level in positive_levels()
        event_occurred = event is not None

        if predicted_positive and event_occurred:
            outcome = PredictionOutcomeType.TRUE_POSITIVE
        elif predicted_positive and not event_occurred:
            outcome = PredictionOutcomeType.FALSE_POSITIVE
        elif not predicted_positive and event_occurred:
            outcome = PredictionOutcomeType.FALSE_NEGATIVE
        else:
            outcome = PredictionOutcomeType.TRUE_NEGATIVE

        if event is None:
            reason = (
                f"no reliability event was observed on this component between "
                f"{window_start.isoformat()} and {window_end.isoformat()}, while "
                f"the forecast predicted {forecast.risk_level.value} risk"
            )
            return OutcomeResult(
                forecast_id=forecast.id,
                outcome=outcome,
                reason=reason,
                window_start=window_start,
                window_end=window_end,
            )

        lead = int(
            (
                aware_utc(event["detected_at"]) - aware_utc(forecast.generated_at)
            ).total_seconds()
        )
        return OutcomeResult(
            forecast_id=forecast.id,
            outcome=outcome,
            reason=(
                f"a {event['kind']} was observed on this component inside the "
                f"forecast window ({event['detected_at'].isoformat()}), while the "
                f"forecast predicted {forecast.risk_level.value} risk"
            ),
            actual_event=event["kind"],
            actual_severity=event["severity"],
            time_to_event_seconds=max(lead, 0),
            matched_incident_id=event.get("incident_id"),
            matched_anomaly_id=event.get("anomaly_id"),
            window_start=window_start,
            window_end=window_end,
        )

    async def _observed_event(
        self,
        forecast: ReliabilityForecast,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> Optional[dict]:
        """The first reliability event inside the window, if any.

        Incidents are matched on the component (or the project/environment when
        the forecast was project-scoped). A matching anomaly of a countable
        severity is used as a secondary event for the prediction types whose
        claim is about telemetry behaviour.

        Matching is on *scope and time*, never on the forecast's own signals:
        scoring a prediction against the evidence that produced it would make
        every forecast self-confirming.
        """
        incident_clauses = [
            Incident.project_id == forecast.project_id,
            Incident.detected_at >= window_start,
            Incident.detected_at <= window_end,
        ]
        if forecast.component_id is not None:
            incident_clauses.append(
                Incident.primary_component_id == forecast.component_id
            )
        if forecast.environment_id is not None:
            incident_clauses.append(Incident.environment_id == forecast.environment_id)

        incident_rows = (
            await self.session.execute(
                select(
                    Incident.id,
                    Incident.detected_at,
                    Incident.severity,
                )
                .where(*incident_clauses)
                .order_by(Incident.detected_at.asc())
                .limit(1)
            )
        ).all()
        if incident_rows:
            incident_id, detected_at, severity = incident_rows[0]
            return {
                "kind": "incident",
                "anomaly_id": None,
                "incident_id": incident_id,
                "detected_at": aware_utc(detected_at),
                "severity": _enum_text(severity),
            }

        anomaly_types = ANOMALY_TYPES_BY_PREDICTION.get(forecast.prediction_type, ())
        if not anomaly_types:
            return None

        anomaly_clauses = [
            Anomaly.project_id == forecast.project_id,
            Anomaly.detected_at >= window_start,
            Anomaly.detected_at <= window_end,
            Anomaly.severity.in_(COUNTABLE_ANOMALY_SEVERITIES),
        ]
        if forecast.component_id is not None:
            anomaly_clauses.append(Anomaly.component_id == forecast.component_id)
        if forecast.environment_id is not None:
            anomaly_clauses.append(Anomaly.environment_id == forecast.environment_id)

        from app.models.anomaly import AnomalyType

        try:
            allowed = [AnomalyType(name) for name in anomaly_types]
        except ValueError:
            allowed = []
        if allowed:
            anomaly_clauses.append(Anomaly.anomaly_type.in_(allowed))

        anomaly_rows = (
            await self.session.execute(
                select(Anomaly.id, Anomaly.detected_at, Anomaly.severity)
                .where(*anomaly_clauses)
                .order_by(Anomaly.detected_at.asc())
                .limit(1)
            )
        ).all()
        if anomaly_rows:
            anomaly_id, detected_at, severity = anomaly_rows[0]
            return {
                "kind": "anomaly",
                "anomaly_id": anomaly_id,
                "incident_id": None,
                "detected_at": aware_utc(detected_at),
                "severity": _enum_text(severity),
            }
        return None

    # -- evaluation runs (§32, §29, §33) -------------------------------
    async def run_evaluation(
        self,
        *,
        project_id: Optional[Any] = None,
        window_start: datetime,
        window_end: datetime,
        prediction_type: Optional[PredictionType] = None,
        horizon: Optional[ForecastHorizon] = None,
        model_version_label: Optional[str] = None,
        score_due_first: bool = True,
        now: Optional[datetime] = None,
    ) -> ReliabilityEvaluationRun:
        """Score due forecasts, aggregate them, and store an immutable run.

        The run records the window, the filter, the confusion counts, the
        metrics and the calibration together, so a reported number can always
        be traced back to the rows behind it (§32, §53).
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        if score_due_first:
            await self.evaluate_due(project_id=project_id, now=now)

        clauses = [
            ForecastOutcome.evaluated_at >= aware_utc(window_start),
            ForecastOutcome.evaluated_at <= aware_utc(window_end),
        ]
        if project_id is not None:
            clauses.append(ForecastOutcome.project_id == project_id)
        if prediction_type is not None:
            clauses.append(ForecastOutcome.predicted_risk_level.is_not(None))
        outcomes = (
            (await self.session.execute(select(ForecastOutcome).where(*clauses)))
            .scalars()
            .all()
        )
        if prediction_type is not None or horizon is not None or model_version_label:
            outcomes = await self._filter_by_forecast(
                outcomes,
                prediction_type=prediction_type,
                horizon=horizon,
                model_version_label=model_version_label,
            )

        summary = compute_metrics(list(outcomes))
        notes = list(summary.notes)
        if prediction_type is not None:
            notes.append(f"filtered to prediction type {prediction_type.value}")
        if horizon is not None:
            notes.append(f"filtered to forecast horizon {horizon.value}")
        if model_version_label:
            notes.append(f"filtered to model {model_version_label}")
        notes.append(
            "incidents count as reliability events for every prediction type; "
            "anomalies count only for the prediction types whose claim is about "
            "telemetry behaviour, and only at MEDIUM severity or above"
        )

        run = ReliabilityEvaluationRun(
            project_id=project_id,
            model_version_label=model_version_label,
            prediction_type=prediction_type,
            forecast_horizon=horizon,
            status=summary.status,
            dataset_window_start=aware_utc(window_start),
            dataset_window_end=aware_utc(window_end),
            feature_schema_version="v1",
            sample_count=summary.sample_count,
            positive_count=summary.positive_count,
            negative_count=summary.negative_count,
            inconclusive_count=summary.inconclusive_count,
            metrics=summary.metrics,
            calibration=summary.calibration,
            calibration_status=summary.calibration_status,
            reliability_bands=summary.reliability_bands,
            notes=notes,
            metadata_={
                "confusion": summary.as_dict()["confusion"],
                "evaluated_count": summary.evaluated_count,
                "min_evaluation_sample": settings.RELIABILITY_MIN_EVALUATION_SAMPLE,
            },
        )
        self.session.add(run)
        await self.session.flush()
        logger.info(
            "reliability evaluation run %s: %s samples, status %s",
            run.id,
            summary.sample_count,
            summary.status.value,
        )
        return run

    async def _filter_by_forecast(
        self,
        outcomes: Sequence[ForecastOutcome],
        *,
        prediction_type: Optional[PredictionType],
        horizon: Optional[ForecastHorizon],
        model_version_label: Optional[str],
    ) -> list[ForecastOutcome]:
        """Join outcomes back to their forecasts for the run's filters."""
        ids = [outcome.forecast_id for outcome in outcomes]
        if not ids:
            return []
        forecasts = (
            await self.session.execute(
                select(
                    ReliabilityForecast.id,
                    ReliabilityForecast.prediction_type,
                    ReliabilityForecast.forecast_horizon,
                    ReliabilityForecast.model_version_label,
                ).where(ReliabilityForecast.id.in_(ids))
            )
        ).all()
        allowed = {}
        for forecast_id, ftype, fhorizon, label in forecasts:
            if prediction_type is not None and ftype is not prediction_type:
                continue
            if horizon is not None and fhorizon is not horizon:
                continue
            if model_version_label and label != model_version_label:
                continue
            allowed[forecast_id] = True
        return [outcome for outcome in outcomes if outcome.forecast_id in allowed]

    async def latest_evaluation(
        self, *, project_id: Optional[Any] = None
    ) -> Optional[ReliabilityEvaluationRun]:
        clauses = []
        if project_id is not None:
            clauses.append(ReliabilityEvaluationRun.project_id == project_id)
        return (
            (
                await self.session.execute(
                    select(ReliabilityEvaluationRun)
                    .where(*clauses)
                    .order_by(ReliabilityEvaluationRun.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )


def predicted_probability_note() -> str:
    """One-line statement reused by the API, keeping the wording consistent."""
    return (
        "ARGUS reports risk levels and a bounded risk score derived from named "
        "predictive signals. It does not report a probability of failure, and "
        "calibration describes how the risk score tracks observed event "
        "frequency rather than the accuracy of a probabilistic forecast."
    )


def _enum_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


__all__ = [
    "ANOMALY_TYPES_BY_PREDICTION",
    "CALIBRATION_BANDS",
    "COUNTABLE_ANOMALY_SEVERITIES",
    "MetricsSummary",
    "OutcomeResult",
    "PredictionEvaluationService",
    "compute_metrics",
    "positive_levels",
    "predicted_probability_note",
]
