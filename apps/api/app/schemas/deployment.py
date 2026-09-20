"""ARGUS Deployment Schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import Field

from app.models.deployment import DeploymentStatus
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin


class DeploymentCreate(BaseSchema):
    """Schema for creating a deployment event."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    deployment_id: str = Field(..., min_length=1, max_length=255)
    version: Optional[str] = None
    commit_sha: Optional[str] = Field(None, min_length=7, max_length=40)
    deployed_at: datetime
    status: DeploymentStatus = DeploymentStatus.UNKNOWN
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class DeploymentUpdate(BaseSchema):
    """Schema for updating a deployment event."""

    status: Optional[DeploymentStatus] = None
    version: Optional[str] = None
    commit_sha: Optional[str] = None
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class DeploymentResponse(IDMixin, TimestampMixin):
    """Schema for deployment response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    deployment_id: str
    version: Optional[str] = None
    commit_sha: Optional[str] = None
    deployed_at: datetime
    status: DeploymentStatus
    description: Optional[str] = None
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class DeploymentList(BaseSchema):
    """List of deployments."""

    items: List[DeploymentResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)
