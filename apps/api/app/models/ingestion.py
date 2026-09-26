"""ARGUS Ingestion / Observability Source models.

Phase 1 additions: persistent observability source registry with health state,
configuration-change events (secret-safe by design), health-check events, and
the ingestion dead-letter store.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, Enum, ForeignKey, func
from app.models.base import Guid as UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    pass


class ObservabilitySourceCategory(str, enum.Enum):
    """Categories of observability sources (provider-neutral, §5)."""

    APPLICATION = "APPLICATION"
    OTEL = "OTEL"
    PROMETHEUS = "PROMETHEUS"
    CLOUD = "CLOUD"
    CUSTOM = "CUSTOM"
    WEBHOOK = "WEBHOOK"
    FILE = "FILE"
    MOCK = "MOCK"


class ObservabilitySourceStatus(str, enum.Enum):
    """Health state of an ingestion source (§45)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class HealthStatus(str, enum.Enum):
    """Normalized component health state (§15)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class ObservabilitySource(BaseModel):
    """A registered observability ingestion source."""

    __tablename__ = "observability_sources"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[ObservabilitySourceCategory] = mapped_column(
        Enum(ObservabilitySourceCategory), nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Connection/configuration metadata. Never store secrets here — the API
    # schema rejects known secret keys before persistence (§46).
    configuration: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    status: Mapped[ObservabilitySourceStatus] = mapped_column(
        Enum(ObservabilitySourceStatus),
        default=ObservabilitySourceStatus.UNKNOWN,
        nullable=False,
    )
    # Health signals (§45). Derived, never inferred from a single event.
    last_event_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    # Ingestion trust (hardening W1). SHA-256 of the per-source ingest token;
    # the raw token is shown once at creation and never stored. ``None`` means
    # the source predates the hardening pass and has not been issued a token
    # yet — the ingestion auth dependency treats a configured-but-tokenless
    # source as closed until a token is rotated in.
    ingest_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    ingest_token_rotated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConfigurationChangeEvent(BaseModel):
    """A configuration change event.

    IMPORTANT: this table must never store sensitive configuration values
    (passwords, API keys, secrets). Only safe metadata and descriptions are
    persisted (§14, §46).
    """

    __tablename__ = "configuration_change_events"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    change_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Only safe metadata — sensitive values are rejected by the ingest schema.
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    source_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HealthCheckEvent(BaseModel):
    """A normalized health-check event (§15)."""

    __tablename__ = "health_check_events"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus), nullable=False, index=True
    )
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    source_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestionFailure(BaseModel):
    """Dead-letter record for events that exhausted ingestion retries (§44).

    Failed events are preserved for inspection — never silently discarded.
    The payload summary is redacted before persistence.
    """

    __tablename__ = "ingestion_failures"

    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True
    )
    event_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Redacted representation of the event payload for inspection.
    payload_summary: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
