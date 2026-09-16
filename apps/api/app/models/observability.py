"""ARGUS Observability Models."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, Text, Enum, DateTime, Float, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    from app.models.project import SoftwareProject


class EventType(str, enum.Enum):
    """Observability event types."""
    LOG = "LOG"
    METRIC = "METRIC"
    TRACE = "TRACE"
    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    HEALTH_CHECK = "HEALTH_CHECK"
    SYSTEM_EVENT = "SYSTEM_EVENT"


class Severity(str, enum.Enum):
    """Event severity levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"


class MetricType(str, enum.Enum):
    """Metric types."""
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    HISTOGRAM = "HISTOGRAM"
    SUMMARY = "SUMMARY"
    UNKNOWN = "UNKNOWN"


class TraceStatus(str, enum.Enum):
    """Trace/Span status."""
    OK = "OK"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class ObservabilityEvent(BaseModel):
    """A normalized observability event."""

    __tablename__ = "observability_events"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id"), nullable=True, index=True
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_components.id"), nullable=True, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), nullable=False, index=True)
    severity: Mapped[Optional[Severity]] = mapped_column(Enum(Severity), nullable=True, index=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)
    # Time the event was ingested — distinct from the event timestamp (§22).
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Correlation IDs
    request_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    span_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    deployment_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    incident_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Dedup fingerprint (Phase 1 §21) — canonical hash of immutable event fields.
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="observability_events"
    )


class LogRecord(BaseModel):
    """A normalized log record."""

    __tablename__ = "log_records"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id"), nullable=True, index=True
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_components.id"), nullable=True, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    level: Mapped[Severity] = mapped_column(Enum(Severity), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    service: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    span_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    source_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MetricRecord(BaseModel):
    """A normalized metric record."""

    __tablename__ = "metric_records"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id"), nullable=True, index=True
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_components.id"), nullable=True, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    metric_type: Mapped[MetricType] = mapped_column(Enum(MetricType), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    labels: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)


class TraceRecord(BaseModel):
    """A distributed trace."""

    __tablename__ = "traces"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id"), nullable=True, index=True
    )
    trace_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[TraceStatus] = mapped_column(Enum(TraceStatus), default=TraceStatus.UNKNOWN, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)


class SpanRecord(BaseModel):
    """A span within a distributed trace."""

    __tablename__ = "spans"

    trace_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    span_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    parent_span_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_components.id"), nullable=True, index=True
    )
    operation: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[TraceStatus] = mapped_column(Enum(TraceStatus), default=TraceStatus.UNKNOWN, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)