"""ARGUS Observability Schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import Field

from app.models.observability import EventType, MetricType, Severity, TraceStatus
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin


# Observability Event Schemas
class ObservabilityEventCreate(BaseSchema):
    """Schema for creating an observability event."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    timestamp: datetime
    source: str = Field(..., min_length=1, max_length=255)
    event_type: EventType
    severity: Optional[Severity] = None
    payload: Optional[dict] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    deployment_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None


class ObservabilityEventResponse(IDMixin, TimestampMixin):
    """Schema for observability event response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    timestamp: datetime
    source: str
    event_type: EventType
    severity: Optional[Severity] = None
    payload: Optional[dict] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    deployment_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None


class ObservabilityEventList(BaseSchema):
    """List of observability events."""

    items: List[ObservabilityEventResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# Log Schemas
class LogRecordCreate(BaseSchema):
    """Schema for creating a log record."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    timestamp: datetime
    level: Severity
    message: str
    service: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")
    raw_payload: Optional[dict] = None


class LogRecordResponse(IDMixin, TimestampMixin):
    """Schema for log record response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    timestamp: datetime
    level: Severity
    message: str
    service: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")
    raw_payload: Optional[dict] = None


class LogRecordList(BaseSchema):
    """List of log records."""

    items: List[LogRecordResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# Metric Schemas
class MetricRecordCreate(BaseSchema):
    """Schema for creating a metric record."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    timestamp: datetime
    metric_name: str = Field(..., min_length=1, max_length=255)
    metric_type: MetricType
    value: float
    unit: Optional[str] = None
    labels: Optional[dict] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class MetricRecordResponse(IDMixin, TimestampMixin):
    """Schema for metric record response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    timestamp: datetime
    metric_name: str
    metric_type: MetricType
    value: float
    unit: Optional[str] = None
    labels: Optional[dict] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class MetricRecordList(BaseSchema):
    """List of metric records."""

    items: List[MetricRecordResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# Trace Schemas
class TraceRecordCreate(BaseSchema):
    """Schema for creating a trace."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    trace_id: str = Field(..., min_length=1, max_length=255)
    name: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    status: TraceStatus = TraceStatus.UNKNOWN
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class TraceRecordResponse(IDMixin, TimestampMixin):
    """Schema for trace response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    trace_id: str
    name: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    status: TraceStatus
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class SpanRecordCreate(BaseSchema):
    """Schema for creating a span."""

    trace_id: str = Field(..., min_length=1, max_length=255)
    span_id: str = Field(..., min_length=1, max_length=255)
    parent_span_id: Optional[str] = None
    project_id: uuid.UUID
    component_id: Optional[uuid.UUID] = None
    operation: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    status: TraceStatus = TraceStatus.UNKNOWN
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class SpanRecordResponse(IDMixin, TimestampMixin):
    """Schema for span response."""

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    project_id: uuid.UUID
    component_id: Optional[uuid.UUID] = None
    operation: Optional[str] = None
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    status: TraceStatus
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class TraceWithSpans(BaseSchema):
    """Trace with its spans."""

    trace: TraceRecordResponse
    spans: List[SpanRecordResponse]


class TraceRecordList(BaseSchema):
    """List of trace records."""

    items: List[TraceRecordResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)
