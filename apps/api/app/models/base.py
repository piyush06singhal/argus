"""ARGUS Base Models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, JSON, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import CHAR, TypeDecorator

from app.core.database import Base

# Use JSONB on PostgreSQL, fall back to JSON on other backends (e.g. SQLite in tests)
JSONType = JSON().with_variant(JSONB, "postgresql")


class Guid(TypeDecorator):
    """Platform-independent UUID type.

    Uses PostgreSQL's native UUID on PostgreSQL and CHAR(32) hex elsewhere
    (e.g. SQLite in tests). Accepts UUID objects or their string forms on
    bind, so raw string values never leak into the driver layer.
    """

    impl = CHAR
    cache_ok = True

    def __init__(self, *args, **kwargs):
        # Tolerate legacy UUID(as_uuid=True)-style construction; behavior is fixed.
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID

            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value if dialect.name == "postgresql" else value.hex

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BaseModel(Base, TimestampMixin):
    """Base model with UUID primary key and timestamps."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
