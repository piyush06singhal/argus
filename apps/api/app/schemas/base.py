"""ARGUS Base Schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class BaseSchema(BaseModel):
    """Base schema with common configuration.

    `extra="forbid"` rejects unknown request fields, enforcing strict request
    validation at the API boundary.
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        use_enum_values=True,
        extra="forbid",
    )


class TimestampMixin(BaseSchema):
    """Mixin for timestamp fields."""

    created_at: datetime
    updated_at: datetime


class IDMixin(BaseSchema):
    """Mixin for ID field."""

    id: uuid.UUID


class PaginationParams(BaseSchema):
    """Pagination parameters."""

    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(20, ge=1, le=100, description="Items per page")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class PaginationMeta(BaseSchema):
    """Pagination metadata included in every paginated list response."""

    total: int = Field(..., description="Total items matching the filter")
    page: int = Field(..., ge=1, description="Current page number")
    page_size: int = Field(..., ge=1, le=100, description="Items per page")
    total_pages: int = Field(..., ge=0, description="Total number of pages")


class PaginatedResponse(PaginationMeta, Generic[T]):
    """Generic paginated response."""

    items: List[T]

    @classmethod
    def create(cls, items: List[T], total: int, page: int, page_size: int) -> "PaginatedResponse[T]":
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )


class ErrorResponse(BaseSchema):
    """Error response schema."""

    detail: str
    error_code: Optional[str] = None
    metadata: Optional[dict] = None


class HealthResponse(BaseSchema):
    """Health check response."""

    status: str
    timestamp: datetime
    version: str
    environment: str


class DependencyHealth(BaseSchema):
    """Health status of a dependency."""

    name: str
    status: str
    latency_ms: Optional[float] = None
    error: Optional[str] = None
