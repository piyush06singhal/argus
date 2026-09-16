"""ARGUS Project Schemas."""
from __future__ import annotations

import uuid
from typing import List, Optional

from pydantic import Field

from app.models.project import EnvironmentType, ProjectStatus
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin


# Project Schemas
class ProjectCreate(BaseSchema):
    """Schema for creating a project."""

    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255, pattern=r"^[a-z0-9-]+$")
    description: Optional[str] = None
    repository_url: Optional[str] = None
    repository_provider: Optional[str] = None
    default_branch: Optional[str] = "main"
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class ProjectUpdate(BaseSchema):
    """Schema for updating a project."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[ProjectStatus] = None
    repository_url: Optional[str] = None
    repository_provider: Optional[str] = None
    default_branch: Optional[str] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class ProjectResponse(IDMixin, TimestampMixin):
    """Schema for project response."""

    name: str
    slug: str
    description: Optional[str] = None
    status: ProjectStatus
    repository_url: Optional[str] = None
    repository_provider: Optional[str] = None
    default_branch: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class ProjectList(BaseSchema):
    """List of projects."""

    items: List[ProjectResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


# Environment Schemas
class EnvironmentCreate(BaseSchema):
    """Schema for creating an environment."""

    name: str = Field(..., min_length=1, max_length=255)
    environment_type: EnvironmentType
    description: Optional[str] = None
    is_active: bool = True
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class EnvironmentResponse(IDMixin, TimestampMixin):
    """Schema for environment response."""

    project_id: uuid.UUID
    name: str
    environment_type: EnvironmentType
    description: Optional[str] = None
    is_active: bool
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class EnvironmentList(BaseSchema):
    """List of environments."""

    items: List[EnvironmentResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)
