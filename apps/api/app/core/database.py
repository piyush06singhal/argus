"""ARGUS Database Configuration."""

from __future__ import annotations

from typing import AsyncGenerator, Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# Build engine kwargs appropriate for the database backend
engine_kwargs: Dict[str, Any] = {
    "echo": settings.API_DEBUG,
    "pool_pre_ping": True,
}

# Connection pool settings only apply to server-backed engines (PostgreSQL)
if settings.DATABASE_URL.startswith("postgres"):
    engine_kwargs.update(
        {
            "pool_size": 20,
            "max_overflow": 10,
            # asyncpg caches prepared statements per connection. When the schema
            # changes underneath a serving API (an `alembic upgrade` run against
            # a live database), those cached plans become invalid and the *next*
            # request on an affected connection raises
            # `InvalidCachedStatementError` — a 500 for a request that is
            # perfectly valid. SQLAlchemy invalidates its caches in response and
            # the following request succeeds, but "the first request after a
            # migration 500s" is not an acceptable failure mode for a
            # reliability platform, so the cache is disabled. The cost is a
            # re-plan per statement; a correct answer on the first try wins.
            "connect_args": {"statement_cache_size": 0},
        }
    )
else:
    # SQLite (tests/development) requires shared cache for in-memory + StaticPool
    if settings.DATABASE_URL == "sqlite+aiosqlite:///:memory:":
        from sqlalchemy.pool import StaticPool

        engine_kwargs.update(
            {
                "poolclass": StaticPool,
                "connect_args": {"check_same_thread": False},
            }
        )

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    **engine_kwargs,
)

# Create session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides a database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
