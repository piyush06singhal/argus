"""ARGUS Database Configuration."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

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


#: The session ``get_db`` opened for the request currently being served.
#:
#: Published so the edge can commit it **before the response is sent**. FastAPI
#: runs a dependency's teardown *after* the response has gone out, so the
#: ``await session.commit()`` below happens too late for a client that reads
#: straight after it writes: a ``DELETE`` could return ``204`` while the next
#: ``GET`` still saw the row. See ``CommitBeforeResponseMiddleware``.
current_request_session: ContextVar[Optional[AsyncSession]] = ContextVar(
    "argus_request_session", default=None
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides a database session.

    The commit in the normal path is the *second* place a request's work is
    committed, not the first: ``CommitBeforeResponseMiddleware`` commits the
    same session just before the response starts, so a caller can observe its
    own write. This one remains the authority for the error path (where nothing
    was sent, so the edge never committed) and a harmless no-op otherwise.
    """
    async with async_session_factory() as session:
        token = current_request_session.set(session)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            current_request_session.reset(token)
            await session.close()


async def commit_request_session() -> None:
    """Commit the in-flight request's session, if it has work pending.

    A no-op when no session is current (a response produced above the router) or
    when the session has already been committed. Used by
    ``CommitBeforeResponseMiddleware``; see :data:`current_request_session`.
    """
    session = current_request_session.get()
    if session is None:
        return
    if not session.in_transaction():
        return
    await session.commit()


def _migration_head() -> str:
    """The revision the migration scripts are on. Read from the files."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()  # type: ignore[return-value]


async def _recorded_revision() -> Optional[str]:
    """The revision this database has applied, or ``None`` if it has none."""
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("select version_num from alembic_version"))
            row = result.first()
            return str(row[0]) if row else None
    except Exception:  # noqa: BLE001 — a missing table is the answer, not an error
        return None


async def init_db() -> None:
    """Establish the database schema — with exactly **one** authority.

    What this used to do, in every environment, was
    ``Base.metadata.create_all()``. That is a second schema authority competing
    with Alembic, and because ``create_all`` only ever creates *missing*
    tables, the failure it produces is not a clean one:

    * it creates new tables (and new PostgreSQL enum types) from ORM metadata
      without recording a revision, so the *migration* that owns them then
      fails with ``type "authsource" already exists``;
    * it never adds a new column to an existing table, so the database it leaves
      behind has one authority's tables and the other's columns — a schema
      neither of them fully describes, which fails later at query time rather
      than at boot.

    Both happened in this repository's own stack: a worker container that
    skipped Alembic created the SSO tables from metadata, and the API
    container's migration then refused to run.

    So the rule is now explicit:

    * **tests** keep ``create_all`` — the unit suite builds its SQLite database
      from metadata on purpose, and that is what makes it fast;
    * **everything else** runs **no DDL at all**. It verifies the database is at
      the migration head and refuses to start if it is not, with a message that
      says which command to run. A process that cannot serve correct queries
      should not pretend to serve.
    """
    settings = get_settings()
    if settings.is_testing:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return

    head = _migration_head()
    recorded = await _recorded_revision()
    if recorded == head:
        return

    if recorded is None:
        detail = "the database has no applied migrations"
    else:
        detail = f"the database is at revision {recorded}"
    raise RuntimeError(
        f"Database schema is not at the migration head: {detail}, but the "
        f"application expects {head}. Run `alembic upgrade head` (in the "
        f"container: `docker compose run --rm api alembic upgrade head`) before "
        f"starting this process. ARGUS deliberately does not create schema from "
        f"model metadata outside the test suite — two schema authorities is how "
        f"a deployment ends up with tables one of them never described."
    )


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
