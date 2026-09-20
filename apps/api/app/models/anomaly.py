"""ARGUS Anomaly & Incident Intelligence Models (Phase 3).

Phase 3 turns normalized telemetry into *deterministic, explainable* anomaly
records and groups related ones into incidents. Two boundaries are structural
here, not merely documented:

* An anomaly is never a root cause. ``anomaly_type`` describes the *shape* of
  an observed deviation, and ``incident_id`` records grouping only.
* Every row carries provenance (``source``, ``rule_id``, ``source_event_id``)
  so a human — or a later phase — can ask *why was this detected?*

Nothing in this module comes from an LLM or an unexplained score: severity,
confidence and deviation are computed by deterministic rules and stored for
inspection.

Ownership follows the Phase 2 graph pattern: rows are project/environment
scoped with database-level ``ON DELETE CASCADE`` rather than ORM relationships,
so a raw project delete can never be blocked by, or orphan, Phase 3 data.
Component references use ``ON DELETE SET NULL`` — a deleted component must not
erase incident history.
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
    from app.models.incident import Incident


class AnomalyType(str, enum.Enum):
    """The *shape* of an observed deviation (never a cause)."""

    METRIC_THRESHOLD = "METRIC_THRESHOLD"
    METRIC_BASELINE_DEVIATION = "METRIC_BASELINE_DEVIATION"
    ERROR_RATE_SPIKE = "ERROR_RATE_SPIKE"
    LATENCY_SPIKE = "LATENCY_SPIKE"
    THROUGHPUT_DROP = "THROUGHPUT_DROP"
    LOG_PATTERN_SPIKE = "LOG_PATTERN_SPIKE"
    TRACE_FAILURE_SPIKE = "TRACE_FAILURE_SPIKE"
    HEALTH_DEGRADATION = "HEALTH_DEGRADATION"
    REQUEST_RATE_CHANGE = "REQUEST_RATE_CHANGE"
    RESOURCE_USAGE_SPIKE = "RESOURCE_USAGE_SPIKE"
    # Temporal/contextual association only — these never imply causality.
    DEPLOYMENT_RELATED_CHANGE = "DEPLOYMENT_RELATED_CHANGE"
    CONFIGURATION_RELATED_CHANGE = "CONFIGURATION_RELATED_CHANGE"


class AnomalySeverity(str, enum.Enum):
    """Severity from deterministic, explainable rules (§7)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AnomalyStatus(str, enum.Enum):
    """Anomaly lifecycle (§8)."""

    DETECTED = "DETECTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


class AnomalySource(str, enum.Enum):
    """Provenance of an anomaly — which evidence produced it."""

    METRIC = "METRIC"
    LOG = "LOG"
    TRACE = "TRACE"
    SPAN = "SPAN"
    HEALTH_CHECK = "HEALTH_CHECK"
    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION = "CONFIGURATION"
    GRAPH = "GRAPH"
    COMPOSITE = "COMPOSITE"
    UNKNOWN = "UNKNOWN"


class BaselineStrategy(str, enum.Enum):
    """Deterministic baseline strategies (§9)."""

    STATIC = "STATIC"
    ROLLING = "ROLLING"


class RuleCondition(str, enum.Enum):
    """Deterministic detection conditions a rule can evaluate (§11)."""

    THRESHOLD = "THRESHOLD"
    BASELINE_DEVIATION = "BASELINE_DEVIATION"
    Z_SCORE = "Z_SCORE"
    RATE_CHANGE = "RATE_CHANGE"
    ERROR_RATE = "ERROR_RATE"
    LATENCY_RATIO = "LATENCY_RATIO"
    PATTERN_SPIKE = "PATTERN_SPIKE"
    HEALTH_TRANSITION = "HEALTH_TRANSITION"
    TRACE_FAILURE_RATE = "TRACE_FAILURE_RATE"


class AnomalyRule(BaseModel):
    """A configurable, validated detection rule (§18)."""

    __tablename__ = "anomaly_rules"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_anomaly_rules_project_name"),
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
    #: Optional scope to a single component; NULL means "any in scope".
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    anomaly_type: Mapped[AnomalyType] = mapped_column(
        SAEnum(AnomalyType), nullable=False, index=True
    )
    condition: Mapped[RuleCondition] = mapped_column(
        SAEnum(RuleCondition), nullable=False
    )
    #: Metric this rule evaluates (e.g. ``http.checkout.latency.p95``).
    metric_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    baseline_strategy: Mapped[BaselineStrategy] = mapped_column(
        SAEnum(BaselineStrategy), default=BaselineStrategy.ROLLING, nullable=False
    )
    #: Static expected value (STATIC strategy) and/or absolute threshold.
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Relative multiplier / z-score cutoff, depending on ``condition``.
    multiplier: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    z_threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Minimum samples before a statistical condition may fire (§10).
    min_samples: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    #: Rolling window used to compute the baseline, in seconds.
    window_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    #: Suppress re-detection of the same fingerprint for this long.
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    #: Consecutive violating evaluations required before an anomaly opens.
    persistence_cycles: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    severity: Mapped[AnomalySeverity] = mapped_column(
        SAEnum(AnomalySeverity), nullable=False
    )
    #: Deterministic severity escalation knobs (e.g. by criticality/duration).
    severity_policy: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class AnomalyBaseline(BaseModel):
    """A computed baseline for one metric in one scope (§9).

    Append-only: the engine writes a new row each evaluation and reads the most
    recent, so baseline history is itself auditable. ``sample_count`` is stored
    so consumers can distinguish a real baseline from ``INSUFFICIENT_DATA``.
    """

    __tablename__ = "anomaly_baselines"
    __table_args__ = (
        Index(
            "ix_anomaly_baselines_scope_metric_time",
            "project_id",
            "metric_name",
            "computed_at",
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
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    strategy: Mapped[BaselineStrategy] = mapped_column(
        SAEnum(BaselineStrategy), nullable=False
    )
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mean: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    median: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stddev: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p95: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p99: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: The value detection compares observed values against.
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class AnomalyFingerprint(BaseModel):
    """Deduplication registry keyed by deterministic fingerprint (§16, §41).

    One row per logical anomaly per project. Repeated detections bump
    ``occurrence_count`` and point ``anomaly_id`` at the current record instead
    of creating duplicates — the mechanism that turns "an anomaly every 10s for
    5 minutes" into one evolving anomaly rather than 30.
    """

    __tablename__ = "anomaly_fingerprints"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "fingerprint",
            name="uq_anomaly_fingerprints_project_fingerprint",
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
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    anomaly_type: Mapped[AnomalyType] = mapped_column(
        SAEnum(AnomalyType), nullable=False, index=True
    )
    #: The current anomaly this fingerprint maps to (nullable while suppressed).
    anomaly_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomalies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_suppressed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class Anomaly(BaseModel):
    """A detected anomaly — evidence of abnormal behaviour (§5)."""

    __tablename__ = "anomalies"
    __table_args__ = (
        Index(
            "ix_anomalies_project_status_severity", "project_id", "status", "severity"
        ),
        Index("ix_anomalies_project_fingerprint", "project_id", "fingerprint"),
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
    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomaly_rules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Grouping only — an anomaly belonging to an incident is *correlated*, not
    #: established as a cause. SET NULL so deleting an incident keeps evidence.
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    anomaly_type: Mapped[AnomalyType] = mapped_column(
        SAEnum(AnomalyType), nullable=False, index=True
    )
    severity: Mapped[AnomalySeverity] = mapped_column(
        SAEnum(AnomalySeverity), nullable=False, index=True
    )
    status: Mapped[AnomalyStatus] = mapped_column(
        SAEnum(AnomalyStatus),
        default=AnomalyStatus.DETECTED,
        nullable=False,
        index=True,
    )
    source: Mapped[AnomalySource] = mapped_column(
        SAEnum(AnomalySource),
        default=AnomalySource.UNKNOWN,
        nullable=False,
        index=True,
    )
    metric_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    #: Normalized log pattern template (dynamic values elided) when applicable.
    pattern_template: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    observed_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Signed relative deviation ((observed-expected)/expected) for explainability.
    deviation: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    z_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Evidence strength, NOT probability of causality (§11).
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_event_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    observation_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_changed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Suppression is recorded, never silent (§42).
    suppressed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    suppression_rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomaly_suppressions.id", ondelete="SET NULL"),
        nullable=True,
    )
    suppression_reason: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    suppressed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    incident: Mapped[Optional["Incident"]] = relationship(
        "Incident", back_populates="anomalies"
    )
    observations: Mapped[List["AnomalyObservation"]] = relationship(
        "AnomalyObservation",
        back_populates="anomaly",
        cascade="all, delete-orphan",
    )


class AnomalyObservation(BaseModel):
    """One supporting observation for an anomaly (§28).

    Raw telemetry is never copied here — ``payload_summary`` holds only the
    redacted key/type shape produced by the Phase 1 ``RedactionEngine``.
    """

    __tablename__ = "anomaly_observations"
    __table_args__ = (
        Index("ix_anomaly_observations_anomaly_time", "anomaly_id", "observed_at"),
    )

    anomaly_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomalies.id", ondelete="CASCADE"),
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
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    observed_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    deviation: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    z_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sample_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_event_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    payload_summary: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    # Relationships
    anomaly: Mapped["Anomaly"] = relationship("Anomaly", back_populates="observations")


class AnomalySuppression(BaseModel):
    """An explicit, auditable suppression rule (§42).

    Suppressed anomalies are still detected and stored — they are flagged
    (``Anomaly.suppressed``), never discarded, so data is never hidden.
    """

    __tablename__ = "anomaly_suppressions"

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
    #: NULL anomaly_type/metric means "all" within the configured scope.
    anomaly_type: Mapped[Optional[AnomalyType]] = mapped_column(
        SAEnum(AnomalyType), nullable=True, index=True
    )
    metric_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class MaintenanceWindow(BaseModel):
    """A planned maintenance window (§43).

    Anomalies inside the window may be suppressed or downgraded by explicit
    configuration — never hidden: the effect is recorded on each anomaly.
    """

    __tablename__ = "maintenance_windows"

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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    suppress_anomalies: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    downgrade_severity: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


__all__ = [
    "AnomalyType",
    "AnomalySeverity",
    "AnomalyStatus",
    "AnomalySource",
    "BaselineStrategy",
    "RuleCondition",
    "AnomalyRule",
    "AnomalyBaseline",
    "AnomalyFingerprint",
    "Anomaly",
    "AnomalyObservation",
    "AnomalySuppression",
    "MaintenanceWindow",
]
