"""ARGUS Incident Schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import Field

from app.models.incident import (
    EvidenceType,
    IncidentSeverity,
    IncidentStatus,
    TimelineEventType,
)
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin


# Incident Schemas
class IncidentCreate(BaseSchema):
    """Schema for creating an incident."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    severity: IncidentSeverity
    status: IncidentStatus = IncidentStatus.OPEN
    detected_at: datetime
    started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    # Phase 3 context (all optional — a manually filed incident carries none).
    fingerprint: Optional[str] = Field(None, max_length=64)
    summary: Optional[str] = None
    primary_component_id: Optional[uuid.UUID] = None
    correlation_rationale: Optional[dict] = None
    acknowledged_at: Optional[datetime] = None
    status_changed_by: Optional[str] = Field(None, max_length=255)
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class IncidentUpdate(BaseSchema):
    """Schema for updating an incident."""

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    severity: Optional[IncidentSeverity] = None
    status: Optional[IncidentStatus] = None
    started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    summary: Optional[str] = None
    primary_component_id: Optional[uuid.UUID] = None
    correlation_rationale: Optional[dict] = None
    status_changed_by: Optional[str] = Field(None, max_length=255)
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class IncidentResponse(IDMixin, TimestampMixin):
    """Schema for incident response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    title: str
    description: Optional[str] = None
    severity: IncidentSeverity
    status: IncidentStatus
    detected_at: datetime
    started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    fingerprint: Optional[str] = None
    summary: Optional[str] = None
    primary_component_id: Optional[uuid.UUID] = None
    correlation_rationale: Optional[dict] = None
    acknowledged_at: Optional[datetime] = None
    status_changed_by: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class IncidentList(BaseSchema):
    """List of incidents."""

    items: List[IncidentResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# Evidence Schemas
class EvidenceCreate(BaseSchema):
    """Schema for creating incident evidence.

    Note: `incident_id` comes from the URL path parameter, not the body.
    """

    evidence_type: EvidenceType
    source_id: str = Field(..., min_length=1, max_length=255)
    timestamp: datetime
    relevance_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    description: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    observed_value: Optional[str] = Field(None, max_length=512)
    expected_value: Optional[str] = Field(None, max_length=512)
    severity: Optional[IncidentSeverity] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    provenance: Optional[str] = Field(None, max_length=64)
    relevance_reason: Optional[str] = None
    anomaly_id: Optional[uuid.UUID] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class EvidenceResponse(IDMixin, TimestampMixin):
    """Schema for evidence response."""

    incident_id: uuid.UUID
    evidence_type: EvidenceType
    source_id: str
    timestamp: datetime
    relevance_score: Optional[float] = None
    description: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    observed_value: Optional[str] = None
    expected_value: Optional[str] = None
    severity: Optional[IncidentSeverity] = None
    confidence: Optional[float] = None
    provenance: Optional[str] = None
    relevance_reason: Optional[str] = None
    anomaly_id: Optional[uuid.UUID] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class EvidenceList(BaseSchema):
    """List of evidence items."""

    items: List[EvidenceResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Timeline & lifecycle (Phase 3)
# ---------------------------------------------------------------------------
class TimelineEventResponse(IDMixin, TimestampMixin):
    """One incident timeline entry (§27)."""

    incident_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    event_type: TimelineEventType
    occurred_at: datetime
    title: str
    description: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    anomaly_id: Optional[uuid.UUID] = None
    evidence_id: Optional[uuid.UUID] = None
    #: True when the entry is temporal context, not an observed fact (§31).
    is_context_only: bool = False
    provenance: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class TimelineEventCreate(BaseSchema):
    """Manually add a timeline note (notes are the only hand-authored kind)."""

    event_type: TimelineEventType = TimelineEventType.NOTE
    occurred_at: datetime
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    actor: Optional[str] = Field(None, max_length=255)


class LifecycleActionRequest(BaseSchema):
    """Acknowledge / resolve an incident or anomaly (auditable actor)."""

    actor: Optional[str] = Field(None, max_length=255)
    note: Optional[str] = None


class TimelineEventList(BaseSchema):
    """List of timeline entries."""

    items: List[TimelineEventResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)
