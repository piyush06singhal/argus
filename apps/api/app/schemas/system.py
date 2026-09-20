"""ARGUS System Schemas."""

from __future__ import annotations

import uuid
from typing import List, Optional

from pydantic import Field

from app.models.system import ComponentCategory, ComponentStatus, DependencyType
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin


# Component Schemas
class ComponentCreate(BaseSchema):
    """Schema for creating a component."""

    name: str = Field(..., min_length=1, max_length=255)
    component_type: ComponentCategory
    environment_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    status: ComponentStatus = ComponentStatus.UNKNOWN
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class ComponentUpdate(BaseSchema):
    """Schema for updating a component."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    component_type: Optional[ComponentCategory] = None
    environment_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    status: Optional[ComponentStatus] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class ComponentResponse(IDMixin, TimestampMixin):
    """Schema for component response."""

    project_id: uuid.UUID
    name: str
    component_type: ComponentCategory
    environment_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    status: ComponentStatus
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class ComponentList(BaseSchema):
    """List of components."""

    items: List[ComponentResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# Dependency Schemas
class DependencyCreate(BaseSchema):
    """Schema for creating a dependency."""

    source_component_id: uuid.UUID
    target_component_id: uuid.UUID
    dependency_type: DependencyType
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class DependencyResponse(IDMixin, TimestampMixin):
    """Schema for dependency response."""

    source_component_id: uuid.UUID
    target_component_id: uuid.UUID
    dependency_type: DependencyType
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class DependencyList(BaseSchema):
    """List of dependencies."""

    items: List[DependencyResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# System Map
class SystemMapNode(BaseSchema):
    """A node in the system map."""

    id: uuid.UUID
    name: str
    component_type: ComponentCategory
    status: ComponentStatus


class SystemMapEdge(BaseSchema):
    """An edge in the system map."""

    source: uuid.UUID
    target: uuid.UUID
    dependency_type: DependencyType


class SystemMap(BaseSchema):
    """System map response."""

    nodes: List[SystemMapNode]
    edges: List[SystemMapEdge]
