"""ARGUS Incident Models."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import String, Text, Enum, DateTime, Float, ForeignKey, Index
from app.models.base import Guid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    from app.models.anomaly import Anomaly
    from app.models.project import SoftwareProject


class IncidentSeverity(str, enum.Enum):
    """Incident severity levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, enum.Enum):
    """Incident status.

    The legal transitions between these values live in one place —
    ``app.services.incident_state`` — shared by the API and the state machine
    so the UI can never offer a transition the backend rejects (§26).
    """

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class EvidenceType(str, enum.Enum):
    """Types of incident evidence."""

    LOG = "LOG"
    METRIC = "METRIC"
    TRACE = "TRACE"
    SPAN = "SPAN"
    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    HEALTH_CHECK = "HEALTH_CHECK"
    GRAPH = "GRAPH"
    ANOMALY = "ANOMALY"
    CODE_CHANGE = "CODE_CHANGE"
    DEPENDENCY_CHANGE = "DEPENDENCY_CHANGE"
    CUSTOM = "CUSTOM"


class TimelineEventType(str, enum.Enum):
    """Types of incident timeline event (§27)."""

    INCIDENT_CREATED = "INCIDENT_CREATED"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    ANOMALY_UPDATED = "ANOMALY_UPDATED"
    COMPONENT_AFFECTED = "COMPONENT_AFFECTED"
    DEPLOYMENT_OCCURRED = "DEPLOYMENT_OCCURRED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    HEALTH_CHANGED = "HEALTH_CHANGED"
    TRACE_FAILURE = "TRACE_FAILURE"
    LOG_PATTERN_SPIKE = "LOG_PATTERN_SPIKE"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    NOTE = "NOTE"
    INCIDENT_STATUS_CHANGED = "INCIDENT_STATUS_CHANGED"
    INCIDENT_ACKNOWLEDGED = "INCIDENT_ACKNOWLEDGED"
    INCIDENT_MITIGATED = "INCIDENT_MITIGATED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"


class Incident(BaseModel):
    """An incident detected or reported in a system."""

    __tablename__ = "incidents"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[IncidentSeverity] = mapped_column(
        Enum(IncidentSeverity), nullable=False, index=True
    )
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus), default=IncidentStatus.OPEN, nullable=False, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Phase 3 §25-§33: deterministic dedup key, generated summary, primary
    # affected component, and the *explainable* rationale for grouping.
    fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    correlation_rationale: Mapped[Optional[dict]] = mapped_column(
        JSONType, nullable=True
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_changed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="incidents"
    )
    evidence: Mapped[List["IncidentEvidence"]] = relationship(
        "IncidentEvidence", back_populates="incident", cascade="all, delete-orphan"
    )
    timeline: Mapped[List["IncidentTimelineEvent"]] = relationship(
        "IncidentTimelineEvent",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentTimelineEvent.occurred_at",
    )
    anomalies: Mapped[List["Anomaly"]] = relationship(
        "Anomaly", back_populates="incident"
    )


class IncidentEvidence(BaseModel):
    """Evidence associated with an incident."""

    __tablename__ = "incident_evidence"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_type: Mapped[EvidenceType] = mapped_column(
        Enum(EvidenceType), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Phase 3 §28-§29: structured, explainable evidence. Values are stored as
    # canonical strings so a log pattern and a numeric metric share one shape.
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    observed_value: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    expected_value: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    severity: Mapped[Optional[IncidentSeverity]] = mapped_column(
        Enum(IncidentSeverity), nullable=True, index=True
    )
    #: Evidence strength, NOT causal probability (§29).
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    provenance: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Why this evidence is *relevant* (never "why it is the cause").
    relevance_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    anomaly_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomalies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    incident: Mapped["Incident"] = relationship("Incident", back_populates="evidence")


class IncidentTimelineEvent(BaseModel):
    """One chronologically ordered entry in an incident's timeline (§27).

    Events are only ever recorded from real stored evidence — never fabricated
    to fill gaps. Ordering is by ``occurred_at`` (UTC), the time the underlying
    fact happened, which may differ from ``created_at`` (when we learned it).
    """

    __tablename__ = "incident_timeline_events"
    __table_args__ = (
        Index(
            "ix_incident_timeline_incident_occurred",
            "incident_id",
            "occurred_at",
        ),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[TimelineEventType] = mapped_column(
        Enum(TimelineEventType), nullable=False, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    anomaly_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anomalies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incident_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Timeline events are facts or explicitly-marked temporal context — the
    #: discriminator lets the UI render "context, not cause" correctly.
    is_context_only: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    provenance: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    incident: Mapped["Incident"] = relationship("Incident", back_populates="timeline")
