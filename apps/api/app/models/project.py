"""ARGUS Project Models."""
from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import String, Text, Enum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    from app.models.deployment import CodeRepository, DeploymentEvent
    from app.models.incident import Incident
    from app.models.observability import ObservabilityEvent
    from app.models.system import SystemComponent


class ProjectStatus(str, enum.Enum):
    """Project lifecycle status."""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class EnvironmentType(str, enum.Enum):
    """Environment types."""
    DEVELOPMENT = "DEVELOPMENT"
    TEST = "TEST"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"
    CUSTOM = "CUSTOM"


class SoftwareProject(BaseModel):
    """A software system being analyzed by ARGUS."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.ACTIVE, nullable=False
    )
    repository_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    repository_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    default_branch: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)

    # Relationships
    environments: Mapped[List["Environment"]] = relationship(
        "Environment", back_populates="project", cascade="all, delete-orphan"
    )
    components: Mapped[List["SystemComponent"]] = relationship(
        "SystemComponent", back_populates="project", cascade="all, delete-orphan"
    )
    incidents: Mapped[List["Incident"]] = relationship(
        "Incident", back_populates="project", cascade="all, delete-orphan"
    )
    deployment_events: Mapped[List["DeploymentEvent"]] = relationship(
        "DeploymentEvent", back_populates="project", cascade="all, delete-orphan"
    )
    observability_events: Mapped[List["ObservabilityEvent"]] = relationship(
        "ObservabilityEvent", back_populates="project", cascade="all, delete-orphan"
    )
    code_repositories: Mapped[List["CodeRepository"]] = relationship(
        "CodeRepository", back_populates="project", cascade="all, delete-orphan"
    )


class Environment(BaseModel):
    """An environment within a project."""

    __tablename__ = "environments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    environment_type: Mapped[EnvironmentType] = mapped_column(
        Enum(EnvironmentType), nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONType, nullable=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="environments"
    )
    components: Mapped[List["SystemComponent"]] = relationship(
        "SystemComponent", back_populates="environment"
    )