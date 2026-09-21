"""ARGUS Predictive Reliability Models (Phase 8).

Phase 8 forecasts reliability risk *before* it becomes an incident. The module
is built around one refusal: **a prediction is not a fact.** Every row here
exists so a forecast can be audited, reproduced and later scored — never so it
can be presented as certainty.

The structural rules:

* **A forecast always carries its own epistemic status.** ``confidence``,
  ``calibration_status``, ``data_coverage`` and ``limitations`` are columns,
  not afterthoughts, and ``UNKNOWN`` risk is a first-class value: a component
  with too little history is *not* low risk.
* **A forecast is reproducible from its feature snapshot.** The exact numeric
  features, the window they were computed over and the model version are all
  stored, so a later reviewer can recompute the same prediction (§17, §82).
* **A predictive signal is not a cause.** ``PredictiveSignal`` records
  *predictive* contribution and is never allowed to masquerade as causal
  evidence produced by Phase 4 (§6, §35).
* **Evaluation never overwrites history.** Outcomes are append-only rows keyed
  to the forecast, and evaluation runs are immutable records of one scoring
  pass (§32).

Enum type names are set explicitly (``forecast_risk_level`` rather than
``risklevel``) because PostgreSQL enum types are global to a database and two
earlier phases already created ``risklevel`` and ``risksignaltype`` with
different members. Reusing those names would silently coerce Phase 8 values.

Ownership follows the Phase 2–7 pattern: project/environment scoped rows use
database-level ``ON DELETE CASCADE``; component references use
``ON DELETE SET NULL`` so deleting a component never erases forecast history.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType, Guid as UUID

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ForecastHorizon(str, enum.Enum):
    """How far ahead a forecast looks (§3).

    The forward window is derived from :data:`HORIZON_SECONDS` rather than
    being hard-coded at each call site, so adding a horizon is a one-line
    change and no service can disagree about what "24 hours" means.
    """

    ONE_HOUR = "ONE_HOUR"
    SIX_HOURS = "SIX_HOURS"
    TWENTY_FOUR_HOURS = "TWENTY_FOUR_HOURS"
    SEVEN_DAYS = "SEVEN_DAYS"

    @property
    def seconds(self) -> int:
        return HORIZON_SECONDS[self]

    @property
    def label(self) -> str:
        """Human-facing shorthand used by the UI and explanations."""
        return HORIZON_LABELS[self]


#: The single source of truth for horizon durations (§3).
HORIZON_SECONDS: dict[ForecastHorizon, int] = {
    ForecastHorizon.ONE_HOUR: 3_600,
    ForecastHorizon.SIX_HOURS: 21_600,
    ForecastHorizon.TWENTY_FOUR_HOURS: 86_400,
    ForecastHorizon.SEVEN_DAYS: 604_800,
}

HORIZON_LABELS: dict[ForecastHorizon, str] = {
    ForecastHorizon.ONE_HOUR: "1h",
    ForecastHorizon.SIX_HOURS: "6h",
    ForecastHorizon.TWENTY_FOUR_HOURS: "24h",
    ForecastHorizon.SEVEN_DAYS: "7d",
}


class PredictionType(str, enum.Enum):
    """Explicit prediction categories (§4).

    Everything is not collapsed into one generic "risk": a latency trajectory
    and a dependency failure are different claims with different evidence, and
    the evaluation metrics are computed per type.
    """

    FAILURE_RISK = "FAILURE_RISK"
    ERROR_RATE_RISK = "ERROR_RATE_RISK"
    LATENCY_RISK = "LATENCY_RISK"
    AVAILABILITY_RISK = "AVAILABILITY_RISK"
    RESOURCE_EXHAUSTION_RISK = "RESOURCE_EXHAUSTION_RISK"
    DEPENDENCY_FAILURE_RISK = "DEPENDENCY_FAILURE_RISK"
    REGRESSION_RISK = "REGRESSION_RISK"
    INCIDENT_RISK = "INCIDENT_RISK"
    RELIABILITY_DEGRADATION = "RELIABILITY_DEGRADATION"


class ForecastRiskLevel(str, enum.Enum):
    """Classified risk (§5).

    ``UNKNOWN`` is not "no risk" — it is the honest answer when the evidence is
    too thin to classify (§18, §69).
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class ForecastStatus(str, enum.Enum):
    """Forecast lifecycle (§27)."""

    GENERATED = "GENERATED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CONFIRMED = "CONFIRMED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    INCONCLUSIVE = "INCONCLUSIVE"


class ForecastDataQuality(str, enum.Enum):
    """Explicit data-coverage verdict (§18)."""

    GOOD = "GOOD"
    PARTIAL = "PARTIAL"
    POOR = "POOR"
    INSUFFICIENT = "INSUFFICIENT"


class CalibrationStatus(str, enum.Enum):
    """Whether predicted risk matches observed frequency (§33, §53)."""

    GOOD = "GOOD"
    ACCEPTABLE = "ACCEPTABLE"
    POOR = "POOR"
    UNKNOWN = "UNKNOWN"


class PredictionOutcomeType(str, enum.Enum):
    """Scored outcome of a forecast (§28)."""

    TRUE_POSITIVE = "TRUE_POSITIVE"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    TRUE_NEGATIVE = "TRUE_NEGATIVE"
    FALSE_NEGATIVE = "FALSE_NEGATIVE"
    INCONCLUSIVE = "INCONCLUSIVE"


class ReliabilityModelType(str, enum.Enum):
    """Model families (§19, §23, §25)."""

    #: Deterministic statistical predictors — the Phase 8 default.
    ROLLING_TREND = "ROLLING_TREND"
    EWMA = "EWMA"
    THRESHOLD_TRAJECTORY = "THRESHOLD_TRAJECTORY"
    HISTORICAL_FREQUENCY = "HISTORICAL_FREQUENCY"
    #: Reserved families: supported by the interface, only usable when the
    #: data-sufficiency gate passes (§24). None ship enabled.
    LOGISTIC_REGRESSION = "LOGISTIC_REGRESSION"
    GRADIENT_BOOSTED_TREES = "GRADIENT_BOOSTED_TREES"
    TIME_SERIES = "TIME_SERIES"
    SURVIVAL = "SURVIVAL"


class ReliabilityModelStatus(str, enum.Enum):
    """Model lifecycle (§25)."""

    DEVELOPMENT = "DEVELOPMENT"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class PredictiveSignalType(str, enum.Enum):
    """What a predictive signal observed (§6, §73).

    The names describe a *trend in evidence*, never a cause.
    """

    ERROR_RATE_INCREASING = "ERROR_RATE_INCREASING"
    LATENCY_INCREASING = "LATENCY_INCREASING"
    RESOURCE_SATURATION = "RESOURCE_SATURATION"
    DEPENDENCY_DEGRADATION = "DEPENDENCY_DEGRADATION"
    FAILURE_FREQUENCY_INCREASING = "FAILURE_FREQUENCY_INCREASING"
    DEPLOYMENT_INSTABILITY = "DEPLOYMENT_INSTABILITY"
    RECURRENT_INCIDENT_PATTERN = "RECURRENT_INCIDENT_PATTERN"
    CODE_CHURN_RISK = "CODE_CHURN_RISK"
    RECENT_REGRESSION_SIGNAL = "RECENT_REGRESSION_SIGNAL"
    ANOMALY_CLUSTER = "ANOMALY_CLUSTER"


class SignalSeverity(str, enum.Enum):
    """How much a signal moved the risk, in bands rather than fake precision."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FeatureTrend(str, enum.Enum):
    """Direction of a feature over its window (§8, §64).

    ``FLAT`` and ``VOLATILE`` are distinct on purpose: a metric that oscillates
    around a stable mean is not the same evidence as a metric holding steady.
    """

    RISING = "RISING"
    FALLING = "FALLING"
    FLAT = "FLAT"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"


class ForecastFailureReason(str, enum.Enum):
    """Why a forecast could not be produced (§81).

    Recorded explicitly rather than silently returning a default.
    """

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DATA_QUALITY_FAILURE = "DATA_QUALITY_FAILURE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    FEATURE_GENERATION_FAILED = "FEATURE_GENERATION_FAILED"
    PREDICTION_FAILED = "PREDICTION_FAILED"
    EVALUATION_FAILED = "EVALUATION_FAILED"


class DriftKind(str, enum.Enum):
    """What drifted (§41, §42)."""

    FEATURE_DRIFT = "FEATURE_DRIFT"
    PREDICTION_DRIFT = "PREDICTION_DRIFT"
    OUTCOME_DRIFT = "OUTCOME_DRIFT"
    CALIBRATION_DRIFT = "CALIBRATION_DRIFT"
    DATA_DRIFT = "DATA_DRIFT"


class DriftStatus(str, enum.Enum):
    """Drift verdict. ``FLAGGED`` requests human review — never a retrain."""

    STABLE = "STABLE"
    WATCH = "WATCH"
    FLAGGED = "FLAGGED"


class EarlyWarningStatus(str, enum.Enum):
    """Early-warning lifecycle (§39)."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"


class BacktestStatus(str, enum.Enum):
    """Backtest lifecycle (§30, §31)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EvaluationStatus(str, enum.Enum):
    """One evaluation pass over generated forecasts (§32)."""

    COMPLETED = "COMPLETED"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReliabilityModelVersion(BaseModel):
    """A registered predictive model (§25).

    Registration is explicit so every forecast can name the model that produced
    it (§82). Deterministic predictors are registered automatically on first
    use; ML versions are only ever created by an offline training process —
    nothing in the request path promotes a model to ``ACTIVE``.
    """

    __tablename__ = "reliability_model_versions"
    __table_args__ = (
        UniqueConstraint(
            "model_name",
            "version",
            name="uq_reliability_model_versions_name_version",
        ),
    )

    model_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    model_type: Mapped[ReliabilityModelType] = mapped_column(
        SAEnum(ReliabilityModelType, name="reliability_model_type"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    algorithm: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    #: Seconds of history the model expects to be trained/calibrated on.
    training_window_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    #: Version of the feature contract; a bump invalidates reuse (§17).
    feature_schema_version: Mapped[str] = mapped_column(
        String(40), nullable=False, default="v1"
    )
    #: Deterministic hyper-parameters (alpha, windows, thresholds).
    parameters: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Evaluation metrics from the last scoring pass; never authoritative here.
    metrics: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    calibration_metrics: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    calibration_status: Mapped[CalibrationStatus] = mapped_column(
        SAEnum(CalibrationStatus, name="calibration_status"),
        default=CalibrationStatus.UNKNOWN,
        nullable=False,
    )
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[ReliabilityModelStatus] = mapped_column(
        SAEnum(ReliabilityModelStatus, name="reliability_model_status"),
        default=ReliabilityModelStatus.DEVELOPMENT,
        nullable=False,
        index=True,
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ForecastFeatureSnapshot(BaseModel):
    """The exact features a forecast was computed from (§17).

    Stored, not recomputed on read: reproducibility and backtesting both need
    the numbers that were actually used, and a recomputation after the fact
    would silently change the answer.
    """

    __tablename__ = "forecast_feature_snapshots"
    __table_args__ = (
        Index(
            "ix_forecast_feature_snapshots_scope_time",
            "project_id",
            "component_id",
            "forecast_time",
        ),
        Index(
            "ix_forecast_feature_snapshots_window",
            "feature_window_start",
            "feature_window_end",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The instant the snapshot represents. Everything after this is invisible
    #: to the prediction — the leakage boundary (§30, §65).
    forecast_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    feature_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    feature_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Deterministic feature contract version, so old snapshots stay readable.
    feature_schema_version: Mapped[str] = mapped_column(
        String(40), nullable=False, default="v1"
    )
    #: ``{feature_name: {"value": float|None, "trend": str, ...}}``.
    feature_values: Mapped[dict] = mapped_column(JSONType, nullable=False)
    #: Which Phase 0–7 tables actually contributed, with row counts (§18).
    data_sources: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    data_quality: Mapped[ForecastDataQuality] = mapped_column(
        SAEnum(ForecastDataQuality, name="forecast_data_quality"),
        nullable=False,
        index=True,
    )
    #: Plain-language reasons behind the verdict ("only 9 days of history").
    data_quality_notes: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Fraction of expected feature families that produced a usable value.
    data_coverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ReliabilityForecast(BaseModel):
    """One prediction for one component, type and horizon (§2).

    ``risk_score`` is a bounded, documented number where statistical
    justification exists and ``None`` where it does not — the API surfaces
    ``UNKNOWN`` rather than inventing a value (§7, §83).
    """

    __tablename__ = "reliability_forecasts"
    __table_args__ = (
        #: One forecast per scope/type/horizon *revision*. Deduplication (§40)
        #: keeps a single current row per scope by *scope* lookup; revisions
        #: deliberately share a fingerprint when the leading signal is the same,
        #: so the fingerprint cannot be part of the uniqueness key.
        UniqueConstraint(
            "project_id",
            "environment_id",
            "component_id",
            "prediction_type",
            "forecast_horizon",
            "revision",
            name="uq_reliability_forecasts_scope_revision",
        ),
        Index(
            "ix_reliability_forecasts_scope_generated",
            "project_id",
            "generated_at",
        ),
        Index(
            "ix_reliability_forecasts_scope_status",
            "project_id",
            "status",
            "risk_level",
        ),
        Index(
            "ix_reliability_forecasts_expiry",
            "valid_until",
            "status",
        ),
        Index(
            "ix_reliability_forecasts_type_level",
            "prediction_type",
            "risk_level",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    prediction_type: Mapped[PredictionType] = mapped_column(
        SAEnum(PredictionType, name="prediction_type"),
        nullable=False,
        index=True,
    )
    forecast_horizon: Mapped[ForecastHorizon] = mapped_column(
        SAEnum(ForecastHorizon, name="forecast_horizon"),
        nullable=False,
        index=True,
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: Bounded 0–1 risk where the evidence supports a number; NULL otherwise.
    risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_level: Mapped[ForecastRiskLevel] = mapped_column(
        SAEnum(ForecastRiskLevel, name="forecast_risk_level"),
        nullable=False,
        index=True,
    )
    #: Confidence in the *level*, not a probability of failure (§7).
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    calibration_status: Mapped[CalibrationStatus] = mapped_column(
        SAEnum(CalibrationStatus, name="calibration_status"),
        default=CalibrationStatus.UNKNOWN,
        nullable=False,
    )
    data_quality: Mapped[ForecastDataQuality] = mapped_column(
        SAEnum(ForecastDataQuality, name="forecast_data_quality"),
        nullable=False,
        index=True,
    )
    data_coverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_model_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Retained even if the model row is later deleted (§82).
    model_version_label: Mapped[str] = mapped_column(String(120), nullable=False)
    feature_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("forecast_feature_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[ForecastStatus] = mapped_column(
        SAEnum(ForecastStatus, name="forecast_status"),
        default=ForecastStatus.GENERATED,
        nullable=False,
        index=True,
    )
    #: ``valid_until`` and ``status`` both carry single-column indexes; the
    #: composite below is what the expiry sweep actually filters on.
    #: Deterministic dedup key over scope/type/horizon/dominant-signal (§40).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The single strongest contributing signal, used by the fingerprint.
    dominant_signal: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, index=True
    )
    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Plain-language uncertainty, always populated (§34).
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    supporting_evidence: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Explicit failure record when a forecast could not be produced (§81).
    failure_reason: Mapped[Optional[ForecastFailureReason]] = mapped_column(
        SAEnum(ForecastFailureReason, name="forecast_failure_reason"),
        nullable=True,
    )
    failure_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Previous forecast for the same dedup scope, for "why risk changed" (§51).
    previous_forecast_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
        nullable=True,
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    signals: Mapped[List["PredictiveSignal"]] = relationship(
        "PredictiveSignal",
        back_populates="forecast",
        cascade="all, delete-orphan",
    )
    outcomes: Mapped[List["ForecastOutcome"]] = relationship(
        "ForecastOutcome",
        back_populates="forecast",
        cascade="all, delete-orphan",
    )


class PredictiveSignal(BaseModel):
    """A contributing predictive signal (§6, §35).

    Named in terms of evidence, not causation: "latency is rising", not
    "latency caused the failure". ``contribution`` is the weight the model
    assigned; it is a predictive attribution and §6 forbids reading it as a
    causal one.
    """

    __tablename__ = "predictive_signals"
    __table_args__ = (
        Index(
            "ix_predictive_signals_forecast_rank",
            "forecast_id",
            "rank",
        ),
        Index(
            "ix_predictive_signals_scope_type",
            "project_id",
            "signal_type",
            "created_at",
        ),
    )

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    signal_type: Mapped[PredictiveSignalType] = mapped_column(
        SAEnum(PredictiveSignalType, name="predictive_signal_type"),
        nullable=False,
        index=True,
    )
    severity: Mapped[SignalSeverity] = mapped_column(
        SAEnum(SignalSeverity, name="signal_severity"),
        nullable=False,
    )
    #: 0–1 predictive weight. Not a probability, not a causal coefficient.
    contribution: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metric_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    observed_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    baseline_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Signed relative change versus baseline, when defined.
    change_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trend: Mapped[FeatureTrend] = mapped_column(
        SAEnum(FeatureTrend, name="feature_trend"),
        default=FeatureTrend.UNKNOWN,
        nullable=False,
    )
    #: Opaque references to Phase 1–7 rows (anomaly/incident/deployment ids).
    evidence_ids: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Historically similar situations, with the honest caveat attached (§36).
    similar_incident_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    forecast: Mapped["ReliabilityForecast"] = relationship(
        "ReliabilityForecast", back_populates="signals"
    )


class ForecastOutcome(BaseModel):
    """What actually happened after a forecast (§28).

    Append-only: re-evaluating a forecast writes a new row, so a scoring run
    that later looks wrong can be inspected rather than silently replaced.
    """

    __tablename__ = "forecast_outcomes"
    __table_args__ = (
        Index(
            "ix_forecast_outcomes_forecast_evaluated",
            "forecast_id",
            "evaluated_at",
        ),
        Index(
            "ix_forecast_outcomes_project_outcome",
            "project_id",
            "outcome",
        ),
    )

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    evaluation_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    evaluation_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    outcome: Mapped[PredictionOutcomeType] = mapped_column(
        SAEnum(PredictionOutcomeType, name="prediction_outcome"),
        nullable=False,
        index=True,
    )
    #: The observed event kind, e.g. ``incident`` / ``anomaly`` / ``none``.
    actual_event: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    actual_severity: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    #: Seconds from forecast generation to the first matching event (§29).
    time_to_event_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    matched_incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    matched_anomaly_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomalies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The risk level the forecast claimed, copied so scoring survives edits.
    predicted_risk_level: Mapped[ForecastRiskLevel] = mapped_column(
        SAEnum(ForecastRiskLevel, name="forecast_risk_level"),
        nullable=False,
    )
    predicted_risk_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evaluation_reason: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    forecast: Mapped["ReliabilityForecast"] = relationship(
        "ReliabilityForecast", back_populates="outcomes"
    )


class ReliabilityEvaluationRun(BaseModel):
    """One immutable scoring pass over a set of forecasts (§32).

    Sample sizes are stored alongside the metrics so the UI can refuse to show
    a precision computed from four forecasts (§29, §53).
    """

    __tablename__ = "reliability_evaluation_runs"
    __table_args__ = (
        Index(
            "ix_reliability_evaluation_runs_project_created",
            "project_id",
            "created_at",
        ),
    )

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_model_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_version_label: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    prediction_type: Mapped[Optional[PredictionType]] = mapped_column(
        SAEnum(PredictionType, name="prediction_type"), nullable=True
    )
    forecast_horizon: Mapped[Optional[ForecastHorizon]] = mapped_column(
        SAEnum(ForecastHorizon, name="forecast_horizon"), nullable=True
    )
    status: Mapped[EvaluationStatus] = mapped_column(
        SAEnum(EvaluationStatus, name="evaluation_status"),
        default=EvaluationStatus.COMPLETED,
        nullable=False,
    )
    dataset_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dataset_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    feature_schema_version: Mapped[str] = mapped_column(
        String(40), nullable=False, default="v1"
    )
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    positive_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    negative_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inconclusive_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Precision/recall/etc. — omitted keys mean "not statistically meaningful".
    metrics: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    calibration: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    calibration_status: Mapped[CalibrationStatus] = mapped_column(
        SAEnum(CalibrationStatus, name="calibration_status"),
        default=CalibrationStatus.UNKNOWN,
        nullable=False,
    )
    #: Bands of (predicted risk, observed frequency) for a reliability diagram.
    reliability_bands: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    notes: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ReliabilityBacktest(BaseModel):
    """A repeatable historical evaluation (§30, §31).

    Its configuration is stored verbatim so the same backtest can be re-run
    and compared; results are never overwritten (§32).
    """

    __tablename__ = "reliability_backtests"
    __table_args__ = (
        Index(
            "ix_reliability_backtests_project_created",
            "project_id",
            "created_at",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[BacktestStatus] = mapped_column(
        SAEnum(BacktestStatus, name="backtest_status"),
        default=BacktestStatus.QUEUED,
        nullable=False,
        index=True,
    )
    #: Frozen :class:`BacktestConfiguration` (§31).
    configuration: Mapped[dict] = mapped_column(JSONType, nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    training_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_horizon: Mapped[ForecastHorizon] = mapped_column(
        SAEnum(ForecastHorizon, name="forecast_horizon"), nullable=False
    )
    prediction_type: Mapped[PredictionType] = mapped_column(
        SAEnum(PredictionType, name="prediction_type"), nullable=False
    )
    evaluation_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_evaluation_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: One row per origin step, so the walk-forward path is inspectable.
    steps: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    metrics: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ReliabilityDriftRecord(BaseModel):
    """A drift observation (§41, §42).

    Drift only ever *flags for review*: there is no code path here that
    retrains or activates a model automatically.
    """

    __tablename__ = "reliability_drift_records"
    __table_args__ = (
        Index(
            "ix_reliability_drift_records_scope_kind",
            "project_id",
            "kind",
            "created_at",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[DriftKind] = mapped_column(
        SAEnum(DriftKind, name="reliability_drift_kind"),
        nullable=False,
        index=True,
    )
    status: Mapped[DriftStatus] = mapped_column(
        SAEnum(DriftStatus, name="reliability_drift_status"),
        default=DriftStatus.STABLE,
        nullable=False,
        index=True,
    )
    feature_name: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True, index=True
    )
    #: Deterministic drift statistic (normalized mean shift, PSI-like value).
    drift_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reference_window_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reference_window_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_window_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_window_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requires_review: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ReliabilityEarlyWarning(BaseModel):
    """A deduplicated, cooled-down request for human attention (§39, §40)."""

    __tablename__ = "reliability_early_warnings"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "fingerprint", name="uq_early_warnings_project_fingerprint"
        ),
        Index(
            "ix_reliability_early_warnings_project_status",
            "project_id",
            "status",
            "severity",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The forecast that triggered it; SET NULL so history survives expiry.
    forecast_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[ForecastRiskLevel] = mapped_column(
        SAEnum(ForecastRiskLevel, name="forecast_risk_level"),
        nullable=False,
        index=True,
    )
    status: Mapped[EarlyWarningStatus] = mapped_column(
        SAEnum(EarlyWarningStatus, name="early_warning_status"),
        default=EarlyWarningStatus.OPEN,
        nullable=False,
        index=True,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: Cooldown bookkeeping — a warning is updated, not re-raised (§40).
    last_suppressed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ForecastFingerprint(BaseModel):
    """Dedup registry for forecast revisions (§40).

    Keeps a history of revisions for one logical scope instead of emitting a
    new forecast row on every sweep, and records which one is current.
    """

    __tablename__ = "forecast_fingerprints"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "fingerprint",
            name="uq_forecast_fingerprints_project_fingerprint",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prediction_type: Mapped[PredictionType] = mapped_column(
        SAEnum(PredictionType, name="prediction_type"), nullable=False
    )
    forecast_horizon: Mapped[ForecastHorizon] = mapped_column(
        SAEnum(ForecastHorizon, name="forecast_horizon"), nullable=False
    )
    current_forecast_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revision_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Risk level from the previous revision, for "why risk changed" (§51).
    previous_risk_level: Mapped[Optional[ForecastRiskLevel]] = mapped_column(
        SAEnum(ForecastRiskLevel, name="forecast_risk_level"), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


__all__ = [
    "ForecastHorizon",
    "HORIZON_SECONDS",
    "HORIZON_LABELS",
    "PredictionType",
    "ForecastRiskLevel",
    "ForecastStatus",
    "ForecastDataQuality",
    "CalibrationStatus",
    "PredictionOutcomeType",
    "ReliabilityModelType",
    "ReliabilityModelStatus",
    "PredictiveSignalType",
    "SignalSeverity",
    "FeatureTrend",
    "ForecastFailureReason",
    "DriftKind",
    "DriftStatus",
    "EarlyWarningStatus",
    "BacktestStatus",
    "EvaluationStatus",
    "ReliabilityModelVersion",
    "ForecastFeatureSnapshot",
    "ReliabilityForecast",
    "PredictiveSignal",
    "ForecastOutcome",
    "ReliabilityEvaluationRun",
    "ReliabilityBacktest",
    "ReliabilityDriftRecord",
    "ReliabilityEarlyWarning",
    "ForecastFingerprint",
]
