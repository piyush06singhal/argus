"""ARGUS Incident Models."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import String, Text, Enum, DateTime, Float, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    from app.models.project import SoftwareProject


class IncidentSeverity(str, enum.Enum):
    """Incident severity levels."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, enum.Enum):
    """Incident status."""
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class EvidenceType(str, enum.Enum):
    """Types of incident evidence."""
    LOG = "LOG"
    METRIC = "METRIC"
    TRACE = "TRACE"
    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    CODE_CHANGE = "CODE_CHANGE"
    DEPENDENCY_CHANGE = "DEPENDENCY_CHANGE"
    CUSTOM = "CUSTOM"


class Incident(BaseModel):
    """An incident detected or reported in a system."""

    __tablename__ = "incidents"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[IncidentSeverity] = mapped_column(
        Enum(IncidentSeverity), nullable=False, index=True
    )
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus), default=IncidentStatus.OPEN, nullable=False, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="incidents"
    )
    evidence: Mapped[List["IncidentEvidence"]] = relationship(
        "IncidentEvidence", back_populates="incident", cascade="all, delete-orphan"
    )


class IncidentEvidence(BaseModel):
    """Evidence associated with an incident."""

    __tablename__ = "incident_evidence"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    evidence_type: Mapped[EvidenceType] = mapped_column(Enum(EvidenceType), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)

    # Relationships
    incident: Mapped["Incident"] = relationship(
        "Incident", back_populates="evidence"
    )