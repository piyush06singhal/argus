"""ARGUS System Component Models."""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import String, Text, Enum, ForeignKey
from app.models.base import Guid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

if TYPE_CHECKING:
    from app.models.project import Environment, SoftwareProject


class ComponentCategory(str, enum.Enum):
    """Component categories."""

    APPLICATION = "APPLICATION"
    SERVICE = "SERVICE"
    WORKER = "WORKER"
    DATABASE = "DATABASE"
    CACHE = "CACHE"
    QUEUE = "QUEUE"
    EXTERNAL_API = "EXTERNAL_API"
    FRONTEND = "FRONTEND"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    UNKNOWN = "UNKNOWN"


class ComponentStatus(str, enum.Enum):
    """Component status."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class DependencyType(str, enum.Enum):
    """Dependency types."""

    HTTP = "HTTP"
    DATABASE = "DATABASE"
    QUEUE = "QUEUE"
    CACHE = "CACHE"
    RPC = "RPC"
    FILE = "FILE"
    EXTERNAL_API = "EXTERNAL_API"
    UNKNOWN = "UNKNOWN"


class SystemComponent(BaseModel):
    """A component within a software system."""

    __tablename__ = "system_components"

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
    component_type: Mapped[ComponentCategory] = mapped_column(
        Enum(ComponentCategory), nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[ComponentStatus] = mapped_column(
        Enum(ComponentStatus), default=ComponentStatus.UNKNOWN, nullable=False
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="components"
    )
    environment: Mapped[Optional["Environment"]] = relationship(
        "Environment", back_populates="components"
    )
    outgoing_dependencies: Mapped[List["ComponentDependency"]] = relationship(
        "ComponentDependency",
        foreign_keys="[ComponentDependency.source_component_id]",
        back_populates="source_component",
        # Dependency rows are non-nullable in both FK directions; they must be
        # deleted with their component, not NULLed out (project delete regression).
        cascade="all, delete-orphan",
    )
    incoming_dependencies: Mapped[List["ComponentDependency"]] = relationship(
        "ComponentDependency",
        foreign_keys="[ComponentDependency.target_component_id]",
        back_populates="target_component",
        cascade="all, delete-orphan",
    )


class ComponentDependency(BaseModel):
    """A dependency between two components."""

    __tablename__ = "component_dependencies"

    source_component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id"),
        nullable=False,
        index=True,
    )
    target_component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id"),
        nullable=False,
        index=True,
    )
    dependency_type: Mapped[DependencyType] = mapped_column(
        Enum(DependencyType), nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    discovered_at: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    last_seen_at: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Relationships
    source_component: Mapped["SystemComponent"] = relationship(
        "SystemComponent",
        foreign_keys=[source_component_id],
        back_populates="outgoing_dependencies",
    )
    target_component: Mapped["SystemComponent"] = relationship(
        "SystemComponent",
        foreign_keys=[target_component_id],
        back_populates="incoming_dependencies",
    )
