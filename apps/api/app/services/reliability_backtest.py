"""ARGUS Backtest Engine (Phase 8 §30, §31, §32, §65, §66).

Replays history: for a sequence of past instants it builds the features that
were knowable *then*, produces the forecast the live system would have
produced, and compares it against what actually happened in the window that
followed.

The whole value of this module is one property, and it is enforced by
construction rather than asserted in a comment:

    **A backtest step can only see the past.**

Two mechanisms make that true:

* features are built through the same
  :class:`~app.services.reliability_features.ReliabilityFeatureEngine` the live
  path uses, with ``forecast_time = origin`` — and the engine filters every
  query on ``timestamp <= forecast_time``, so there is exactly one
  implementation of the boundary rather than two that can drift;
* the *label* window (``origin`` → ``origin + horizon``) is consulted only to
  decide the outcome, never to compute a feature. The two windows are named
  separately in the step record so an auditor can see which is which.

The engine is walk-forward and rolling-origin: each step is an independent
origin, so a backtest can never be confused with a single random train/test
split (§30, §66).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.reliability import (
    BacktestStatus,
    EvaluationStatus,
    ForecastHorizon,
    ForecastOutcome,
    ForecastRiskLevel,
    PredictionOutcomeType,
    PredictionType,
    ReliabilityBacktest,
    ReliabilityEvaluationRun,
)
from app.services.reliability_evaluation import (
    compute_metrics,
    positive_levels,
)
from app.services.reliability_features import (
    ReliabilityFeatureEngine,
    aware_utc,
)
from app.services.reliability_forecast_service import ForecastScope
from app.services.reliability_models import ReliabilityModelRegistry
from app.services.reliability_risk import classify_score

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class BacktestConfiguration:
    """Everything a backtest needs, stored verbatim for repeatability (§31)."""

    start_time: datetime
    end_time: datetime
    training_window_seconds: int
    forecast_horizon: ForecastHorizon
    prediction_type: PredictionType
    #: How far each origin steps forward. Bounded below by the horizon, so
    #: consecutive label windows do not double-count the same event more than
    #: the caller explicitly asked for.
    step_seconds: int = 3600
    component_id: Optional[Any] = None
    environment_id: Optional[Any] = None
    model_version_label: Optional[str] = None
    #: Cap on origins evaluated, so a wide window cannot run unbounded (§59).
    max_steps: Optional[int] = None

    def __post_init__(self) -> None:
        self.start_time = aware_utc(self.start_time)
        self.end_time = aware_utc(self.end_time)
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if (
            self.training_window_seconds
            < settings.RELIABILITY_BACKTEST_MIN_TRAINING_SECONDS
        ):
            raise ValueError(
                "training_window_seconds must be at least "
                f"{settings.RELIABILITY_BACKTEST_MIN_TRAINING_SECONDS}"
            )
        if self.step_seconds <= 0:
            raise ValueError("step_seconds must be positive")

    @property
    def effective_max_steps(self) -> int:
        return self.max_steps or settings.RELIABILITY_BACKTEST_MAX_STEPS

    @property
    def label_window_seconds(self) -> int:
        """The forward window each step is scored against."""
        return self.forecast_horizon.seconds

    def origins(self) -> list[datetime]:
        """The sequence of forecast instants, newest-last and bounded.

        Each origin must leave room for its own label window inside
        ``end_time``; an origin whose window would run past the end of the
        requested range is dropped rather than scored against partial evidence.
        """
        step = timedelta(seconds=self.step_seconds)
        label = timedelta(seconds=self.label_window_seconds)
        latest_origin = self.end_time - label
        origins: list[datetime] = []
        origin = self.start_time
        while origin <= latest_origin and len(origins) < self.effective_max_steps:
            origins.append(origin)
            origin = origin + step
        return origins

    def as_dict(self) -> dict:
        return {
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "training_window_seconds": self.training_window_seconds,
            "forecast_horizon": self.forecast_horizon.value,
            "prediction_type": self.prediction_type.value,
            "step_seconds": self.step_seconds,
            "component_id": str(self.component_id) if self.component_id else None,
            "environment_id": str(self.environment_id) if self.environment_id else None,
            "model_version_label": self.model_version_label,
            "max_steps": self.effective_max_steps,
            "label_window_seconds": self.label_window_seconds,
        }


@dataclass
class BacktestStep:
    """One origin: what was predicted and what followed it."""

    origin: datetime
    risk_level: str
    risk_score: Optional[float]
    data_quality: str
    failure_reason: Optional[str]
    dominant_signal: Optional[str]
    outcome: str
    actual_event: Optional[str] = None
    time_to_event_seconds: Optional[int] = None
    feature_window_start: Optional[datetime] = None
    label_window_end: Optional[datetime] = None
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "origin": self.origin.isoformat(),
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "data_quality": self.data_quality,
            "failure_reason": self.failure_reason,
            "dominant_signal": self.dominant_signal,
            "outcome": self.outcome,
            "actual_event": self.actual_event,
            "time_to_event_seconds": self.time_to_event_seconds,
            "feature_window_start": (
                self.feature_window_start.isoformat()
                if self.feature_window_start
                else None
            ),
            #: The boundary an auditor checks: no feature may come from after
            #: ``origin``, and the label window is strictly after it.
            "label_window_end": (
                self.label_window_end.isoformat() if self.label_window_end else None
            ),
            "note": self.note,
        }


@dataclass
class BacktestResult:
    """Aggregate output of one backtest run."""

    steps: list[BacktestStep] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)
    calibration_status: str = "UNKNOWN"
    notes: list[str] = field(default_factory=list)
    status: EvaluationStatus = EvaluationStatus.INSUFFICIENT_SAMPLE

    @property
    def sample_count(self) -> int:
        return len(self.steps)


class BacktestEngine:
    """Walk-forward evaluation over stored history (§30, §31)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.engine = ReliabilityFeatureEngine(session)

    async def run(
        self,
        *,
        project_id: Any,
        configuration: BacktestConfiguration,
        created_by: Optional[str] = None,
    ) -> ReliabilityBacktest:
        """Execute a backtest and persist it with its steps and metrics."""
        row = ReliabilityBacktest(
            project_id=project_id,
            status=BacktestStatus.RUNNING,
            configuration=configuration.as_dict(),
            start_time=configuration.start_time,
            end_time=configuration.end_time,
            training_window_seconds=configuration.training_window_seconds,
            forecast_horizon=configuration.forecast_horizon,
            prediction_type=configuration.prediction_type,
            created_by=created_by,
        )
        self.session.add(row)
        await self.session.flush()

        try:
            result = await self.execute(
                project_id=project_id, configuration=configuration
            )
        except Exception as error:  # noqa: BLE001 - a failed backtest is recorded
            logger.exception("backtest %s failed", row.id)
            row.status = BacktestStatus.FAILED
            row.error = f"{type(error).__name__}: {error}"
            await self.session.flush()
            return row

        evaluation = ReliabilityEvaluationRun(
            project_id=project_id,
            model_version_label=configuration.model_version_label,
            prediction_type=configuration.prediction_type,
            forecast_horizon=configuration.forecast_horizon,
            status=result.status,
            dataset_window_start=configuration.start_time,
            dataset_window_end=configuration.end_time,
            sample_count=result.sample_count,
            positive_count=sum(
                1
                for step in result.steps
                if step.outcome
                in (
                    PredictionOutcomeType.TRUE_POSITIVE.value,
                    PredictionOutcomeType.FALSE_POSITIVE.value,
                )
            ),
            negative_count=sum(
                1
                for step in result.steps
                if step.outcome
                in (
                    PredictionOutcomeType.TRUE_NEGATIVE.value,
                    PredictionOutcomeType.FALSE_NEGATIVE.value,
                )
            ),
            inconclusive_count=sum(
                1
                for step in result.steps
                if step.outcome == PredictionOutcomeType.INCONCLUSIVE.value
            ),
            metrics=result.metrics,
            calibration=result.calibration,
            notes=result.notes,
            metadata_={"source": "backtest", "backtest_id": str(row.id)},
        )
        self.session.add(evaluation)
        await self.session.flush()

        row.status = BacktestStatus.COMPLETED
        row.steps = [step.as_dict() for step in result.steps]
        row.metrics = result.metrics
        row.sample_count = result.sample_count
        row.evaluation_run_id = evaluation.id
        await self.session.flush()
        return row

    async def execute(
        self,
        *,
        project_id: Any,
        configuration: BacktestConfiguration,
    ) -> BacktestResult:
        """Run the walk-forward loop without persisting a backtest row.

        Useful for a caller that wants the evaluation run only, and for tests
        that need to inspect intermediate steps.
        """
        registry = ReliabilityModelRegistry(self.session)
        predictor = await registry.resolve(configuration.prediction_type)
        model_row = await registry.ensure_version(predictor)

        result = BacktestResult()
        target_label = f"{model_row.model_name}/{model_row.version}"
        if (
            configuration.model_version_label
            and configuration.model_version_label != target_label
        ):
            result.notes.append(
                f"the requested model {configuration.model_version_label!r} is not "
                f"the resolved deterministic model {target_label!r}; the "
                "deterministic predictor was used"
            )

        scope = ForecastScope(
            project_id=project_id,
            environment_id=configuration.environment_id,
            component_id=configuration.component_id,
        )

        origins = configuration.origins()
        if not origins:
            result.notes.append(
                "no origin could be evaluated: the requested range is shorter "
                "than one forecast horizon, or the step size skipped every origin"
            )
            result.status = EvaluationStatus.INSUFFICIENT_SAMPLE
            return result

        for origin in origins:
            step = await self._evaluate_origin(
                scope=scope,
                origin=origin,
                configuration=configuration,
                predictor=predictor,
                model_label=target_label,
            )
            result.steps.append(step)

        #: The metrics come from the *same* aggregation the live evaluation uses,
        #: over lightweight outcome objects, so a backtest and a production
        #: evaluation can never disagree about what precision means.
        outcomes = [_outcome_for_step(step) for step in result.steps]
        summary = compute_metrics(outcomes)
        result.metrics = summary.metrics
        result.calibration = summary.calibration
        result.calibration_status = summary.calibration_status.value
        result.status = summary.status
        result.notes.extend(summary.notes)
        result.notes.append(
            "walk-forward evaluation: each origin uses only telemetry at or "
            "before it, and is scored against the window that follows it"
        )
        if configuration.component_id is None:
            result.notes.append(
                "no component scope was supplied, so every origin used "
                "project-scoped features"
            )
        return result

    async def _evaluate_origin(
        self,
        *,
        scope: ForecastScope,
        origin: datetime,
        configuration: BacktestConfiguration,
        predictor: Any,
        model_label: str,
    ) -> BacktestStep:
        """Produce the forecast for one past instant and label it."""
        bundle = await self.engine.build(
            project_id=scope.project_id,
            environment_id=scope.environment_id,
            component_id=scope.component_id,
            forecast_time=origin,
            baseline_seconds=configuration.training_window_seconds,
        )
        draft = predictor.predict(
            bundle, configuration.prediction_type, configuration.forecast_horizon
        )
        level = classify_score(draft.risk_score)
        label_end = origin + timedelta(seconds=configuration.label_window_seconds)

        outcome, event, lead, note = await self._label(
            scope=scope,
            origin=origin,
            label_end=label_end,
            level=level,
            draft=draft,
        )
        return BacktestStep(
            origin=origin,
            risk_level=level.value,
            risk_score=draft.risk_score,
            data_quality=bundle.quality.value,
            failure_reason=None if draft.risk_score is not None else "no_score",
            dominant_signal=draft.dominant_signal,
            outcome=outcome.value,
            actual_event=event,
            time_to_event_seconds=lead,
            feature_window_start=bundle.window_start,
            label_window_end=label_end,
            note=note,
        )

    async def _label(
        self,
        *,
        scope: ForecastScope,
        origin: datetime,
        label_end: datetime,
        level: ForecastRiskLevel,
        draft: Any,
    ) -> tuple[PredictionOutcomeType, Optional[str], Optional[int], Optional[str]]:
        """Label one origin from the *future* window only.

        This is the one place a backtest looks forward, and it looks only to
        decide the outcome. Nothing it reads is written back into a feature.
        """
        if level is ForecastRiskLevel.UNKNOWN or draft.risk_score is None:
            return (
                PredictionOutcomeType.INCONCLUSIVE,
                None,
                None,
                "no risk claim was made, so the origin cannot be scored",
            )

        event = await self._event_between(scope=scope, start=origin, end=label_end)
        predicted_positive = level in positive_levels()
        if event is None:
            return (
                PredictionOutcomeType.FALSE_POSITIVE
                if predicted_positive
                else PredictionOutcomeType.TRUE_NEGATIVE,
                None,
                None,
                None,
            )

        lead = int((aware_utc(event["detected_at"]) - origin).total_seconds())
        return (
            PredictionOutcomeType.TRUE_POSITIVE
            if predicted_positive
            else PredictionOutcomeType.FALSE_NEGATIVE,
            event["kind"],
            max(lead, 0),
            None,
        )

    async def _event_between(
        self, *, scope: ForecastScope, start: datetime, end: datetime
    ) -> Optional[dict]:
        """The first incident (or countable anomaly) inside a forward window."""
        from app.models.anomaly import Anomaly, AnomalySeverity
        from app.models.incident import Incident

        incident_clauses = [
            Incident.project_id == scope.project_id,
            Incident.detected_at > start,
            Incident.detected_at <= end,
        ]
        if scope.component_id is not None:
            incident_clauses.append(Incident.primary_component_id == scope.component_id)
        if scope.environment_id is not None:
            incident_clauses.append(Incident.environment_id == scope.environment_id)
        incident = (
            await self.session.execute(
                select(Incident.id, Incident.detected_at)
                .where(*incident_clauses)
                .order_by(Incident.detected_at.asc())
                .limit(1)
            )
        ).all()
        if incident:
            return {"kind": "incident", "detected_at": aware_utc(incident[0][1])}

        anomaly_clauses = [
            Anomaly.project_id == scope.project_id,
            Anomaly.detected_at > start,
            Anomaly.detected_at <= end,
            Anomaly.severity.in_(
                [
                    AnomalySeverity.MEDIUM,
                    AnomalySeverity.HIGH,
                    AnomalySeverity.CRITICAL,
                ]
            ),
        ]
        if scope.component_id is not None:
            anomaly_clauses.append(Anomaly.component_id == scope.component_id)
        if scope.environment_id is not None:
            anomaly_clauses.append(Anomaly.environment_id == scope.environment_id)
        anomaly = (
            await self.session.execute(
                select(Anomaly.id, Anomaly.detected_at)
                .where(*anomaly_clauses)
                .order_by(Anomaly.detected_at.asc())
                .limit(1)
            )
        ).all()
        if anomaly:
            #: A backtest labels on any countable anomaly regardless of type:
            #: the alternative — restricting by prediction type — would score a
            #: past forecast against a taxonomy that itself changed over time.
            return {"kind": "anomaly", "detected_at": aware_utc(anomaly[0][1])}
        return None


def _outcome_for_step(step: BacktestStep) -> ForecastOutcome:
    """A transient outcome row so backtests reuse the production metrics code.

    Built in memory and never added to the session: its only job is to satisfy
    the aggregation function's input contract.
    """
    return ForecastOutcome(
        forecast_id=None,  # type: ignore[arg-type]
        project_id=None,  # type: ignore[arg-type]
        evaluation_window_start=step.origin,
        evaluation_window_end=step.label_window_end or step.origin,
        outcome=PredictionOutcomeType(step.outcome),
        predicted_risk_level=ForecastRiskLevel(step.risk_level),
        predicted_risk_score=step.risk_score,
        time_to_event_seconds=step.time_to_event_seconds,
        evaluation_reason=step.note or "backtest step",
        evaluated_at=step.origin,
    )


def default_configuration(
    *,
    end_time: datetime,
    forecast_horizon: ForecastHorizon = ForecastHorizon.ONE_HOUR,
    prediction_type: PredictionType = PredictionType.INCIDENT_RISK,
    component_id: Optional[Any] = None,
    environment_id: Optional[Any] = None,
    days: int = 7,
    step_seconds: int = 3600,
    training_window_seconds: Optional[int] = None,
) -> BacktestConfiguration:
    """A sensible, explicitly-documented default configuration (§31).

    Seven days of origins at one-hour steps against a 24-hour training window:
    long enough to produce a usable sample, short enough to stay bounded. The
    caller may override every part.
    """
    end = aware_utc(end_time)
    return BacktestConfiguration(
        start_time=end - timedelta(days=days),
        end_time=end,
        training_window_seconds=(
            training_window_seconds or settings.RELIABILITY_BASELINE_WINDOW_SECONDS
        ),
        forecast_horizon=forecast_horizon,
        prediction_type=prediction_type,
        step_seconds=step_seconds,
        component_id=component_id,
        environment_id=environment_id,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BacktestConfiguration",
    "BacktestEngine",
    "BacktestResult",
    "BacktestStep",
    "default_configuration",
    "utcnow",
]
