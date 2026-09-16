"""ARGUS Incident Schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import Field

from app.models.incident import EvidenceType, IncidentSeverity, IncidentStatus
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
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class IncidentUpdate(BaseSchema):
    """Schema for updating an incident."""

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    severity: Optional[IncidentSeverity] = None
    status: Optional[IncidentStatus] = None
    started_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
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
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class EvidenceResponse(IDMixin, TimestampMixin):
    """Schema for evidence response."""

    incident_id: uuid.UUID
    evidence_type: EvidenceType
    source_id: str
    timestamp: datetime
    relevance_score: Optional[float] = None
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class EvidenceList(BaseSchema):
    """List of evidence items."""

    items: List[EvidenceResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)
