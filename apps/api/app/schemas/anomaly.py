"""ARGUS Anomaly & Incident Intelligence Schemas (Phase 3).

Follows the shared ``BaseSchema`` conventions (``from_attributes``,
``use_enum_values``, ``extra="forbid"``) so ORM rows validate directly into
response models and unknown request fields are rejected.

Rule creation is *validated*: a rule whose condition cannot be evaluated from
the fields provided is rejected at the API boundary rather than silently
detecting nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import Field, model_validator

from app.models.anomaly import (
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalyType,
    BaselineStrategy,
    RuleCondition,
)
from app.models.incident import IncidentSeverity, IncidentStatus
from app.schemas.base import BaseSchema, IDMixin, PaginatedResponse, TimestampMixin
from app.schemas.incident import EvidenceResponse, TimelineEventResponse


# ---------------------------------------------------------------------------
# Anomaly rules
# ---------------------------------------------------------------------------
class AnomalyRuleBase(BaseSchema):
    """Common rule fields."""

    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    anomaly_type: AnomalyType
    condition: RuleCondition
    metric_name: Optional[str] = Field(None, max_length=255)
    baseline_strategy: BaselineStrategy = BaselineStrategy.ROLLING
    expected_value: Optional[float] = None
    threshold: Optional[float] = None
    multiplier: Optional[float] = Field(None, gt=0)
    z_threshold: Optional[float] = Field(None, gt=0)
    min_samples: int = Field(5, ge=1, le=10_000)
    window_seconds: int = Field(300, ge=1, le=86_400)
    cooldown_seconds: int = Field(300, ge=0, le=86_400)
    persistence_cycles: int = Field(1, ge=1, le=100)
    severity: AnomalySeverity
    severity_policy: Optional[dict] = None
    enabled: bool = True
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")
    created_by: Optional[str] = Field(None, max_length=255)
    updated_by: Optional[str] = Field(None, max_length=255)


class AnomalyRuleCreate(AnomalyRuleBase):
    """Create a rule (``project_id`` comes from the request)."""

    project_id: uuid.UUID

    @model_validator(mode="after")
    def _validate_condition_fields(self) -> "AnomalyRuleCreate":
        """Reject rules whose condition has nothing to evaluate (§18).

        Determinism depends on a rule carrying the field its condition needs;
        accepting an unevaluable rule would silently detect nothing.

        ``BaseSchema`` sets ``use_enum_values``, so ``self.condition`` arrives
        as a plain string — coerce it back to the enum before comparing, or the
        checks below would silently never fire.
        """
        condition = RuleCondition(self.condition)
        if condition is RuleCondition.THRESHOLD and self.threshold is None:
            raise ValueError("THRESHOLD rules require a 'threshold'")
        if condition is RuleCondition.BASELINE_DEVIATION and self.multiplier is None:
            raise ValueError("BASELINE_DEVIATION rules require a 'multiplier'")
        if condition is RuleCondition.Z_SCORE and self.z_threshold is None:
            raise ValueError("Z_SCORE rules require a 'z_threshold'")
        if condition is RuleCondition.LATENCY_RATIO and self.multiplier is None:
            raise ValueError("LATENCY_RATIO rules require a 'multiplier'")
        if condition is RuleCondition.ERROR_RATE and self.threshold is None:
            raise ValueError("ERROR_RATE rules require a 'threshold'")
        if condition is RuleCondition.RATE_CHANGE and (
            self.threshold is None and self.multiplier is None
        ):
            raise ValueError("RATE_CHANGE rules require a 'threshold' or 'multiplier'")
        if condition is RuleCondition.PATTERN_SPIKE and self.multiplier is None:
            raise ValueError("PATTERN_SPIKE rules require a 'multiplier'")
        if condition is RuleCondition.TRACE_FAILURE_RATE and self.threshold is None:
            raise ValueError("TRACE_FAILURE_RATE rules require a 'threshold'")
        if (
            condition
            in (
                RuleCondition.THRESHOLD,
                RuleCondition.BASELINE_DEVIATION,
                RuleCondition.Z_SCORE,
                RuleCondition.RATE_CHANGE,
                RuleCondition.ERROR_RATE,
                RuleCondition.LATENCY_RATIO,
                RuleCondition.PATTERN_SPIKE,
                RuleCondition.TRACE_FAILURE_RATE,
            )
            and not self.metric_name
        ):
            raise ValueError("metric-based rules require a 'metric_name'")
        return self


class AnomalyRuleUpdate(BaseSchema):
    """Update a rule — every field optional (``exclude_unset`` at route)."""

    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    anomaly_type: Optional[AnomalyType] = None
    condition: Optional[RuleCondition] = None
    metric_name: Optional[str] = Field(None, max_length=255)
    baseline_strategy: Optional[BaselineStrategy] = None
    expected_value: Optional[float] = None
    threshold: Optional[float] = None
    multiplier: Optional[float] = Field(None, gt=0)
    z_threshold: Optional[float] = Field(None, gt=0)
    min_samples: Optional[int] = Field(None, ge=1, le=10_000)
    window_seconds: Optional[int] = Field(None, ge=1, le=86_400)
    cooldown_seconds: Optional[int] = Field(None, ge=0, le=86_400)
    persistence_cycles: Optional[int] = Field(None, ge=1, le=100)
    severity: Optional[AnomalySeverity] = None
    severity_policy: Optional[dict] = None
    enabled: Optional[bool] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")
    updated_by: Optional[str] = Field(None, max_length=255)


class AnomalyRuleResponse(IDMixin, TimestampMixin, AnomalyRuleBase):
    """Rule response."""

    project_id: uuid.UUID
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
class AnomalyBaselineResponse(IDMixin, TimestampMixin):
    """A computed baseline row."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    metric_name: str
    strategy: BaselineStrategy
    window_seconds: int
    sample_count: int
    mean: Optional[float] = None
    median: Optional[float] = None
    stddev: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    expected_value: Optional[float] = None
    computed_at: datetime
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------
class AnomalyObservationResponse(IDMixin, TimestampMixin):
    """One supporting observation."""

    anomaly_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    observed_at: datetime
    observed_value: Optional[float] = None
    expected_value: Optional[float] = None
    deviation: Optional[float] = None
    z_score: Optional[float] = None
    sample_count: Optional[int] = None
    source_event_id: Optional[str] = None
    payload_summary: Optional[dict] = None


class AnomalyResponse(IDMixin, TimestampMixin):
    """Anomaly response (list + detail)."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    rule_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    status: AnomalyStatus
    source: AnomalySource
    metric_name: Optional[str] = None
    pattern_template: Optional[str] = None
    observed_value: Optional[float] = None
    expected_value: Optional[float] = None
    deviation: Optional[float] = None
    threshold: Optional[float] = None
    z_score: Optional[float] = None
    confidence: Optional[float] = None
    fingerprint: str
    description: Optional[str] = None
    source_event_id: Optional[str] = None
    observation_count: int
    detected_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    status_changed_by: Optional[str] = None
    suppressed: bool = False
    suppression_rule_id: Optional[uuid.UUID] = None
    suppression_reason: Optional[str] = None
    suppressed_at: Optional[datetime] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class AnomalyDetailResponse(AnomalyResponse):
    """Anomaly detail with its observations and an explainability block."""

    observations: List[AnomalyObservationResponse] = Field(default_factory=list)
    #: Deterministic "why was this detected" explanation (§52).
    explanation: Optional[dict] = None


class AnomalyStatusUpdate(BaseSchema):
    """Acknowledge / resolve request (auditable actor + optional note)."""

    actor: Optional[str] = Field(None, max_length=255)
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Suppressions & maintenance windows
# ---------------------------------------------------------------------------
class AnomalySuppressionCreate(BaseSchema):
    """Create an auditable suppression rule."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    anomaly_type: Optional[AnomalyType] = None
    metric_name: Optional[str] = Field(None, max_length=255)
    reason: str = Field(..., min_length=1)
    starts_at: datetime
    ends_at: Optional[datetime] = None
    enabled: bool = True
    created_by: Optional[str] = Field(None, max_length=255)
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")

    @model_validator(mode="after")
    def _validate_window(self) -> "AnomalySuppressionCreate":
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("'ends_at' must be at or after 'starts_at'")
        return self


class AnomalySuppressionUpdate(BaseSchema):
    """Patch a suppression — only supplied fields change.

    Suppressions are deactivated, never deleted: the record that a window muted
    detection must survive the window, or "auditable" would only mean
    "auditable until it gets in the way". ``ends_at`` is the supported way to
    close a suppression early, keeping its start and reason on file.
    """

    ends_at: Optional[datetime] = None
    reason: Optional[str] = Field(None, min_length=1)
    enabled: Optional[bool] = None


class AnomalySuppressionResponse(IDMixin, TimestampMixin, BaseSchema):
    """Suppression response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    anomaly_type: Optional[AnomalyType] = None
    metric_name: Optional[str] = None
    reason: str
    starts_at: datetime
    ends_at: Optional[datetime] = None
    enabled: bool
    created_by: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class MaintenanceWindowCreate(BaseSchema):
    """Create a maintenance window."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    starts_at: datetime
    ends_at: datetime
    suppress_anomalies: bool = True
    downgrade_severity: bool = False
    reason: Optional[str] = None
    enabled: bool = True
    created_by: Optional[str] = Field(None, max_length=255)
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")

    @model_validator(mode="after")
    def _validate_window(self) -> "MaintenanceWindowCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("'ends_at' must be after 'starts_at'")
        if not self.suppress_anomalies and not self.downgrade_severity:
            raise ValueError(
                "a window must either suppress anomalies or downgrade severity"
            )
        return self


class MaintenanceWindowUpdate(BaseSchema):
    """Patch a maintenance window — only supplied fields change.

    Windows are deactivated, never deleted (same audit argument as
    suppressions): the history of planned maintenance stays queryable.
    """

    ends_at: Optional[datetime] = None
    reason: Optional[str] = None
    enabled: Optional[bool] = None
    suppress_anomalies: Optional[bool] = None
    downgrade_severity: Optional[bool] = None


class MaintenanceWindowResponse(IDMixin, TimestampMixin, BaseSchema):
    """Maintenance window response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    name: str
    starts_at: datetime
    ends_at: datetime
    suppress_anomalies: bool
    downgrade_severity: bool
    reason: Optional[str] = None
    enabled: bool
    created_by: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


# ---------------------------------------------------------------------------
# Incident intelligence read models
# ---------------------------------------------------------------------------
class AffectedComponentResponse(BaseSchema):
    """A component in an incident's observed blast radius (§30).

    ``classification`` states *how* the component relates to the incident —
    it is never a statement that the component is failing.
    """

    component_id: uuid.UUID
    name: Optional[str] = None
    classification: str
    reason: Optional[str] = None
    anomaly_count: int = 0
    severity: Optional[AnomalySeverity] = None


class IncidentGraphNode(BaseSchema):
    """A graph node in incident context."""

    node_id: uuid.UUID
    name: str
    node_type: str
    classification: str


class IncidentGraphEdge(BaseSchema):
    """A graph edge in incident context."""

    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str
    source: Optional[str] = None


class IncidentGraphContext(BaseSchema):
    """Knowledge-graph context for an incident (§39, §23)."""

    nodes: List[IncidentGraphNode] = Field(default_factory=list)
    edges: List[IncidentGraphEdge] = Field(default_factory=list)
    #: Explicit reminder rendered by the UI — related is not causal.
    disclaimer: str = (
        "Graph relationships show structural context. Related components are "
        "not implied to be causes."
    )


class DeploymentContextItem(BaseSchema):
    """A nearby deployment (§31)."""

    deployment_event_id: uuid.UUID
    deployment_id: str
    component_id: Optional[uuid.UUID] = None
    version: Optional[str] = None
    deployed_at: datetime
    status: Optional[str] = None
    seconds_before_first_anomaly: Optional[float] = None
    is_context_only: bool = True


class ConfigurationContextItem(BaseSchema):
    """A nearby configuration change (§32)."""

    configuration_event_id: uuid.UUID
    component_id: Optional[uuid.UUID] = None
    changed_at: datetime
    summary: Optional[str] = None
    actor: Optional[str] = None
    is_context_only: bool = True


class IncidentSummaryResponse(BaseSchema):
    """Deterministic incident summary built from stored evidence (§33)."""

    incident_id: uuid.UUID
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    started_at: Optional[datetime] = None
    detected_at: datetime
    resolved_at: Optional[datetime] = None
    text: str
    generated_from: List[str] = Field(default_factory=list)


# Generic paginated aliases (consistent with the Phase 2 graph schemas).
AnomalyRuleList = PaginatedResponse[AnomalyRuleResponse]
AnomalyList = PaginatedResponse[AnomalyResponse]
SuppressionList = PaginatedResponse[AnomalySuppressionResponse]
MaintenanceWindowList = PaginatedResponse[MaintenanceWindowResponse]
TimelineEventList = PaginatedResponse[TimelineEventResponse]
IncidentEvidenceList = PaginatedResponse[EvidenceResponse]
AffectedComponentList = PaginatedResponse[AffectedComponentResponse]
