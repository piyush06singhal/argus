"""ARGUS Deployment Models."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, Text, Enum, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    from app.models.project import SoftwareProject


class DeploymentStatus(str, enum.Enum):
    """Deployment status."""
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


class DeploymentEvent(BaseModel):
    """A deployment event."""

    __tablename__ = "deployment_events"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("environments.id"), nullable=True, index=True
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_components.id"), nullable=True, index=True
    )
    deployment_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus), default=DeploymentStatus.UNKNOWN, nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="deployment_events"
    )


class CodeRepository(BaseModel):
    """A code repository abstraction."""

    __tablename__ = "code_repositories"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    repository_url: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(100), default="main", nullable=False)
    connection_status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    configuration: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="code_repositories"
    )