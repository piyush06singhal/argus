"""ARGUS Predictive Reliability Schemas (Phase 8 §44, §80).

Every response that carries a prediction also carries the things that make it
honest: the horizon, the model version, the feature snapshot it was built from,
its data quality and coverage, its calibration status, and its limitations.

Two shape decisions worth stating:

* **Compound views are dictionaries, not invented rows.** ``explain``,
  ``profile`` and ``health`` are read-only projections assembled from stored
  rows; typing them as free-form dicts keeps the API from pretending there is a
  table that does not exist.
* **No probability without a sample size.** Where a metric could be read as
  statistical (precision, calibration), the sample count travels beside it in
  the same object (§29, §53).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import Field

from app.models.reliability import (
    BacktestStatus,
    CalibrationStatus,
    DriftKind,
    DriftStatus,
    EarlyWarningStatus,
    EvaluationStatus,
    FeatureTrend,
    ForecastDataQuality,
    ForecastFailureReason,
    ForecastHorizon,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionOutcomeType,
    PredictionType,
    ReliabilityModelStatus,
    ReliabilityModelType,
    SignalSeverity,
)
from app.schemas.base import BaseSchema


# ---------------------------------------------------------------------------
# Signals and evidence (§6, §35, §50)
# ---------------------------------------------------------------------------


class PredictiveSignalResponse(BaseSchema):
    """One predictive signal. It is a *signal*, never a claimed cause (§6)."""

    id: uuid.UUID
    forecast_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    signal_type: str
    severity: SignalSeverity
    contribution: Optional[float] = None
    rank: int = 0
    description: str
    metric_name: Optional[str] = None
    observed_value: Optional[float] = None
    baseline_value: Optional[float] = None
    change_rate: Optional[float] = None
    trend: FeatureTrend = FeatureTrend.UNKNOWN
    evidence_ids: dict = Field(default_factory=dict)
    similar_incident_count: int = 0
    created_at: datetime


class FeatureSnapshotResponse(BaseSchema):
    """The exact features behind a forecast (§17, §82)."""

    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    forecast_time: datetime
    feature_window_start: datetime
    feature_window_end: datetime
    feature_schema_version: str
    feature_values: dict
    data_sources: dict = Field(default_factory=dict)
    data_quality: ForecastDataQuality
    data_quality_notes: list = Field(default_factory=list)
    data_coverage: Optional[float] = None
    sample_count: int = 0
    created_at: datetime


# ---------------------------------------------------------------------------
# Forecasts (§2, §3, §4, §27)
# ---------------------------------------------------------------------------


class ReliabilityForecastResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    prediction_type: PredictionType
    forecast_horizon: ForecastHorizon
    generated_at: datetime
    valid_from: datetime
    valid_until: datetime
    risk_score: Optional[float] = None
    risk_level: ForecastRiskLevel
    confidence: Optional[float] = None
    confidence_reason: Optional[str] = None
    calibration_status: CalibrationStatus
    data_quality: ForecastDataQuality
    data_coverage: Optional[float] = None
    model_version_id: Optional[uuid.UUID] = None
    model_version_label: str
    feature_snapshot_id: Optional[uuid.UUID] = None
    status: ForecastStatus
    fingerprint: str
    dominant_signal: Optional[str] = None
    headline: str
    summary: Optional[str] = None
    limitations: list = Field(default_factory=list)
    supporting_evidence: dict = Field(default_factory=dict)
    failure_reason: Optional[ForecastFailureReason] = None
    failure_detail: Optional[str] = None
    previous_forecast_id: Optional[uuid.UUID] = None
    revision: int = 1
    created_at: datetime
    updated_at: datetime
    signals: list[PredictiveSignalResponse] = Field(default_factory=list)


class ReliabilityForecastListResponse(BaseSchema):
    items: list[ReliabilityForecastResponse]
    total: int
    truncated: bool = False


class ForecastExplanationResponse(BaseSchema):
    """The §34/§51 explanation, assembled from stored rows only.

    The four questions the phase requires are answered by four named fields:
    ``what_changed`` (what the evidence did), ``why_risk_increased`` (which
    signals moved it), ``what_supports_this`` (the stored facts and similar
    past incidents), and ``what_is_uncertain`` (the data and model limits).
    """

    forecast_id: uuid.UUID
    headline: str
    summary: Optional[str] = None
    risk_level: ForecastRiskLevel
    risk_score: Optional[float] = None
    prediction_type: PredictionType
    forecast_horizon: ForecastHorizon
    horizon_label: str
    model_version: str
    generated_at: datetime
    valid_until: datetime
    confidence: Optional[float] = None
    confidence_reason: Optional[str] = None
    calibration_status: CalibrationStatus
    data_quality: ForecastDataQuality
    data_coverage: Optional[float] = None
    what_changed: list[str] = Field(default_factory=list)
    why_risk_increased: list[str] = Field(default_factory=list)
    what_supports_this: dict = Field(default_factory=dict)
    what_is_uncertain: list[str] = Field(default_factory=list)
    why_risk_changed: dict = Field(default_factory=dict)
    historical_evidence: list[dict] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    #: Optional re-wording of the explanation. It is additive: the fields above
    #: are built from stored rows and are never derived from this text.
    ai_narrative: Optional[str] = None
    #: Which narrative layer produced it ("none" when the API is deterministic),
    #: so a reader can tell model prose from stored facts.
    ai_narrative_provider: str = "none"
    #: True when a model was configured but did not answer and the deterministic
    #: narrative was shown instead.
    ai_narrative_degraded: bool = False


class RiskHeatmapCellResponse(BaseSchema):
    component_id: Optional[uuid.UUID] = None
    component_name: Optional[str] = None
    environment_id: Optional[uuid.UUID] = None
    prediction_type: PredictionType
    by_horizon: dict[ForecastHorizon, ForecastRiskLevel] = Field(default_factory=dict)
    worst_level: ForecastRiskLevel
    evidence_count: int = 0


class RiskHeatmapResponse(BaseSchema):
    cells: list[RiskHeatmapCellResponse] = Field(default_factory=list)
    horizons: list[ForecastHorizon] = Field(default_factory=list)
    generated_at: datetime
    empty_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Component profile and reliability score (§37, §38)
# ---------------------------------------------------------------------------


class ComponentProfileResponse(BaseSchema):
    """The §37 view. Read-only: opening a profile changes nothing.

    ``current_risk`` is keyed ``"<prediction_type>:<horizon>"`` so a reader can
    see that a component is calm over 1h and elevated over 24h — collapsing
    that to one number would hide the thing the phase is about.
    """

    project_id: uuid.UUID
    project_name: Optional[str] = None
    environment_id: Optional[uuid.UUID] = None
    environment_name: Optional[str] = None
    component_id: uuid.UUID
    component_name: Optional[str] = None
    component_type: Optional[str] = None
    generated_at: datetime
    current_risk: dict = Field(default_factory=dict)
    worst_risk: ForecastRiskLevel = ForecastRiskLevel.UNKNOWN
    signals: dict = Field(default_factory=dict)
    reliability_score: dict = Field(default_factory=dict)
    data_quality: ForecastDataQuality = ForecastDataQuality.INSUFFICIENT
    data_coverage: Optional[float] = None
    data_quality_notes: list = Field(default_factory=list)
    recent_incidents: list[dict] = Field(default_factory=list)
    forecasts: list[ReliabilityForecastResponse] = Field(default_factory=list)
    similar_incidents: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Models (§25, §54), evaluations (§29, §32, §53)
# ---------------------------------------------------------------------------


class ModelVersionResponse(BaseSchema):
    id: uuid.UUID
    model_name: str
    model_type: ReliabilityModelType
    version: str
    algorithm: Optional[str] = None
    training_window_seconds: Optional[int] = None
    feature_schema_version: str
    parameters: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)
    calibration_metrics: dict = Field(default_factory=dict)
    calibration_status: CalibrationStatus
    sample_count: int = 0
    status: ReliabilityModelStatus
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ModelVersionListResponse(BaseSchema):
    items: list[ModelVersionResponse]
    total: int
    truncated: bool = False


class EvaluationRunResponse(BaseSchema):
    id: uuid.UUID
    project_id: Optional[uuid.UUID] = None
    model_version_id: Optional[uuid.UUID] = None
    model_version_label: Optional[str] = None
    prediction_type: Optional[PredictionType] = None
    forecast_horizon: Optional[ForecastHorizon] = None
    status: EvaluationStatus
    dataset_window_start: datetime
    dataset_window_end: datetime
    feature_schema_version: str
    sample_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    inconclusive_count: int = 0
    metrics: dict = Field(default_factory=dict)
    calibration: dict = Field(default_factory=dict)
    calibration_status: CalibrationStatus
    reliability_bands: list = Field(default_factory=list)
    notes: list = Field(default_factory=list)
    created_at: datetime


class EvaluationRunListResponse(BaseSchema):
    items: list[EvaluationRunResponse]
    total: int
    truncated: bool = False


# ---------------------------------------------------------------------------
# Backtesting (§30, §31, §55)
# ---------------------------------------------------------------------------


class BacktestRequest(BaseSchema):
    """Configuration for one walk-forward backtest (§31).

    ``max_steps`` and ``max_components`` exist so a backtest cannot be used as
    an unbounded scan: the caller states a bound and the engine respects it.
    The label window is *not* a parameter — it is the forecast horizon, because
    scoring a forecast against a window the caller chose would make the metric
    mean whatever the caller wanted it to mean.
    """

    start_time: datetime
    end_time: datetime
    training_window_seconds: int = Field(86400, ge=600)
    forecast_horizon: ForecastHorizon = ForecastHorizon.SIX_HOURS
    prediction_type: PredictionType = PredictionType.FAILURE_RISK
    component_ids: Optional[list[uuid.UUID]] = Field(None, max_length=100)
    environment_id: Optional[uuid.UUID] = None
    step_seconds: Optional[int] = Field(None, ge=60)
    max_steps: Optional[int] = Field(None, ge=1, le=500)
    max_components: Optional[int] = Field(None, ge=1, le=100)
    created_by: Optional[str] = Field(None, max_length=255)


class BacktestResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    status: BacktestStatus
    configuration: dict = Field(default_factory=dict)
    start_time: datetime
    end_time: datetime
    training_window_seconds: int
    forecast_horizon: ForecastHorizon
    prediction_type: PredictionType
    evaluation_run_id: Optional[uuid.UUID] = None
    metrics: dict = Field(default_factory=dict)
    sample_count: int = 0
    error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    steps: list = Field(default_factory=list)


class BacktestListResponse(BaseSchema):
    items: list[BacktestResponse]
    total: int
    truncated: bool = False


class BacktestRunResponse(BaseSchema):
    """The result of a backtest request, one entry per requested component."""

    items: list[BacktestResponse]
    total: int
    note: str


# ---------------------------------------------------------------------------
# Drift (§41, §42)
# ---------------------------------------------------------------------------


class DriftFindingResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    model_version_id: Optional[uuid.UUID] = None
    kind: DriftKind
    status: DriftStatus
    feature_name: Optional[str] = None
    drift_score: Optional[float] = None
    threshold: Optional[float] = None
    description: str
    requires_review: bool = False
    created_at: datetime


class DriftFindingDetailResponse(BaseSchema):
    """The inline finding shape inside a drift report (not a stored row)."""

    kind: DriftKind
    status: DriftStatus
    feature_name: Optional[str] = None
    drift_score: Optional[float] = None
    threshold: Optional[float] = None
    description: str
    requires_review: bool = False
    reference_count: int = 0
    current_count: int = 0


class DriftReportResponse(BaseSchema):
    """One drift assessment (§41, §42).

    ``review_policy`` is part of the payload on purpose: the response tells the
    caller that drift requests a human, and that nothing was retrained or
    activated as a result of this call.
    """

    project_id: uuid.UUID
    reference_window: list[datetime] = Field(default_factory=list)
    current_window: list[datetime] = Field(default_factory=list)
    worst_status: DriftStatus
    flagged_count: int = 0
    findings: list[DriftFindingDetailResponse] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    review_policy: str
    retrain_performed: bool = False
    model_activated: bool = False


class StoredDriftFindingResponse(DriftFindingResponse):
    """Alias used by the list/detail endpoints, kept for naming clarity."""


class DriftHistoryResponse(BaseSchema):
    summary: dict = Field(default_factory=dict)
    items: list[DriftFindingResponse] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# Early warnings (§39, §40)
# ---------------------------------------------------------------------------


class EarlyWarningResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    fingerprint: str
    title: str
    description: Optional[str] = None
    severity: ForecastRiskLevel
    status: EarlyWarningStatus
    occurrence_count: int = 1
    first_raised_at: datetime
    last_raised_at: datetime
    last_suppressed_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None


class EarlyWarningListResponse(BaseSchema):
    items: list[EarlyWarningResponse]
    total: int
    truncated: bool = False


class WarningActionRequest(BaseSchema):
    """Acknowledge or dismiss one warning. No remediation follows (§8, §87)."""

    actor: Optional[str] = Field(None, max_length=255)
    reason: Optional[str] = Field(None, max_length=1000)


# ---------------------------------------------------------------------------
# Generation requests and platform health (§43, §57, §58)
# ---------------------------------------------------------------------------


class ForecastGenerateRequest(BaseSchema):
    """Ask for a forecast pass. The request only *schedules* work (§57)."""

    environment_id: Optional[uuid.UUID] = None
    prediction_types: Optional[list[PredictionType]] = None
    horizons: Optional[list[ForecastHorizon]] = None
    limit: Optional[int] = Field(None, ge=1, le=1000)
    dispatch: bool = Field(
        True, description="Queue the pass on the worker rather than run it inline"
    )


class ForecastGenerateResponse(BaseSchema):
    dispatched: bool
    project_id: uuid.UUID
    job_id: Optional[str] = None
    scopes: int = 0
    forecasts_created: int = 0
    forecasts_updated: int = 0
    signals_created: int = 0
    skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    message: str


class PlatformHealthResponse(BaseSchema):
    """§43/§46/§53: what the reliability platform is doing, with sample sizes.

    Accuracy and calibration are included only when an evaluation run has
    produced them; otherwise the field says so rather than showing a zero that
    would read as "perfectly inaccurate".
    """

    generated_at: datetime
    forecast_count: int = 0
    active_forecasts: int = 0
    high_risk_forecasts: int = 0
    unknown_forecasts: int = 0
    data_quality_distribution: dict = Field(default_factory=dict)
    model_version_count: int = 0
    thresholds: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    accuracy: dict = Field(default_factory=dict)
    calibration: dict = Field(default_factory=dict)
    coverage: dict = Field(default_factory=dict)
    drift: dict = Field(default_factory=dict)
    warnings: dict = Field(default_factory=dict)
    models: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PredictionOutcomeResponse(BaseSchema):
    id: uuid.UUID
    forecast_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    evaluation_window_start: datetime
    evaluation_window_end: datetime
    outcome: PredictionOutcomeType
    actual_event: Optional[str] = None
    actual_severity: Optional[str] = None
    time_to_event_seconds: Optional[int] = None
    matched_incident_id: Optional[uuid.UUID] = None
    matched_anomaly_id: Optional[uuid.UUID] = None
    predicted_risk_level: ForecastRiskLevel
    predicted_risk_score: Optional[float] = None
    evaluation_reason: str
    evaluated_at: datetime
