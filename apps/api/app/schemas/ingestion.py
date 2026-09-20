"""ARGUS Ingestion Schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import Field

from app.models.ingestion import (
    HealthStatus,
    ObservabilitySourceCategory,
    ObservabilitySourceStatus,
)
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin


# ---------------------------------------------------------------------------
# Observability source registry (§5, §45)
# ---------------------------------------------------------------------------
class ObservabilitySourceCreate(BaseSchema):
    """Create a registered ingestion source.

    ``configuration`` must never contain secrets — the pipeline redacts and
    the API rejects known secret keys at the boundary.
    """

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    source_type: ObservabilitySourceCategory
    description: Optional[str] = None
    configuration: Optional[dict] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class ObservabilitySourceResponse(IDMixin, TimestampMixin):
    """Registered source with live health state."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    name: str
    source_type: ObservabilitySourceCategory
    description: Optional[str] = None
    configuration: Optional[dict] = None
    status: ObservabilitySourceStatus
    last_event_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    error_count: int
    consecutive_errors: int
    event_count: int
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class ObservabilitySourceList(BaseSchema):
    items: List[ObservabilitySourceResponse]
    total: int


class ObservabilitySourceUpdate(BaseSchema):
    """Patch the mutable health fields of a source (admin/inspection)."""

    status: Optional[ObservabilitySourceStatus] = None
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Configuration change events (§14)
# ---------------------------------------------------------------------------
class ConfigurationChangeEventCreate(BaseSchema):
    """Safe configuration change — sensitive values are rejected/redacted."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    change_id: str = Field(..., min_length=1, max_length=255)
    timestamp: datetime
    source: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class ConfigurationChangeEventResponse(IDMixin, TimestampMixin):
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    change_id: str
    timestamp: datetime
    source: Optional[str] = None
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")
    source_id: Optional[str] = None


class ConfigurationChangeEventList(BaseSchema):
    items: List[ConfigurationChangeEventResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Health check events (§15)
# ---------------------------------------------------------------------------
class HealthCheckEventCreate(BaseSchema):
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: uuid.UUID
    timestamp: datetime
    status: HealthStatus
    latency_ms: Optional[float] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class HealthCheckEventResponse(IDMixin, TimestampMixin):
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: uuid.UUID
    timestamp: datetime
    status: HealthStatus
    latency_ms: Optional[float] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")
    source_id: Optional[str] = None


class HealthCheckEventList(BaseSchema):
    items: List[HealthCheckEventResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Ingestion dead-letter / health (§44, §45)
# ---------------------------------------------------------------------------
class IngestionFailureResponse(IDMixin, TimestampMixin):
    fingerprint: str
    source_id: Optional[str] = None
    source: Optional[str] = None
    project_id: Optional[uuid.UUID] = None
    event_type: Optional[str] = None
    error_type: str
    error_message: str
    retry_count: int
    received_at: Optional[datetime] = None
    failed_at: datetime
    payload_summary: Optional[dict] = None


class IngestionSourceHealth(BaseSchema):
    """Per-source health row for the ingestion center."""

    id: uuid.UUID
    name: str
    source_type: str
    status: str
    events_7d: int
    error_count: int
    consecutive_errors: int
    last_success_at: Optional[datetime] = None


class IngestionSummary(BaseSchema):
    """Top-level ingestion health summary."""

    source_count: int
    status_counts: dict[str, int]
    dead_letter_count: int
    events_ingested_7d: int
    healthy_sources: int
    failing_sources: int
