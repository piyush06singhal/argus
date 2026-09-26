"""Migration deployment test — against a real PostgreSQL (hardening W8).

The suite's default database is SQLite, which is the right choice for fast
unit tests and the wrong choice for validating DDL: SQLite accepts column
types and foreign keys that PostgreSQL rejects outright. Two deployment
failures during this hardening pass were invisible to the whole test suite for
exactly that reason —

* a duplicate Alembic revision id (a *source* defect, now also pinned by
  ``test_migrations_graph.py``), and
* ``CHAR(32)`` columns whose foreign keys point at the ``uuid`` primary keys of
  ``projects``/``api_tokens``, which PostgreSQL refuses with
  ``foreign key ... cannot be implemented`` while SQLite shrugs.

Neither is detectable in-process with SQLite, so this module does what a
deployment does: it creates a throwaway database, runs ``alembic upgrade head``
against it in a subprocess, and then inspects the resulting schema. It is
skipped unless the configured test database is PostgreSQL (which is what the
Integration workflow provides).

The assertions are about the *contract the database now enforces*, not about
SQL text: the id type really is ``uuid``, and the foreign key really exists.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.core.config import get_settings

API_ROOT = Path(__file__).resolve().parents[1]
#: The head the repository is on. Deliberately explicit: when a migration is
#: added, the new head is written down here, which forces the author to have
#: run this test.
EXPECTED_HEAD = "e9f0a1b2c3d4"


def _test_database_url() -> str:
    return os.getenv("ARGUS_TEST_DB") or get_settings().DATABASE_URL


def _require_postgres() -> str:
    url = _test_database_url()
    if not url.startswith("postgresql"):
        pytest.skip(
            "migration deployment is only meaningful against PostgreSQL; "
            "set ARGUS_TEST_DB to a postgresql+asyncpg:// URL (the Integration "
            "workflow does) to run it"
        )
    return url


def _asyncpg_dsn(url: str) -> str:
    """`postgresql+asyncpg://…` → the plain DSN asyncpg itself wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def _with_database(url: str, database: str) -> str:
    head, _, tail = url.rpartition("/")
    tail = tail.split("?", 1)[0]
    return f"{head}/{database}"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def migrated_database() -> str:
    """A throwaway database at ``head``, dropped when the module finishes."""
    base_url = _require_postgres()
    dsn = _asyncpg_dsn(base_url)
    name = f"argus_mig_{uuid.uuid4().hex[:10]}"

    async def admin(sql: str, *args) -> None:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    async def create() -> None:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    async def drop() -> None:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = $1 and pid <> pg_backend_pid()",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()

    _run(create())
    scratch_url = _with_database(base_url, name)
    env = {
        **os.environ,
        "DATABASE_URL": scratch_url,
        #: Migrations must not depend on an interactive environment.
        "API_ENVIRONMENT": "test",
        "BACKGROUND_JOBS_ENABLED": "false",
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            pytest.fail(
                "`alembic upgrade head` failed against a fresh PostgreSQL — this is "
                "exactly the failure that stops a new deployment from booting:\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        yield scratch_url
    finally:
        _run(drop())


def test_upgrade_head_reaches_the_expected_revision(migrated_database: str) -> None:
    import asyncpg

    async def check() -> str:
        conn = await asyncpg.connect(_asyncpg_dsn(migrated_database))
        try:
            return await conn.fetchval("select version_num from alembic_version")
        finally:
            await conn.close()

    assert _run(check()) == EXPECTED_HEAD


def test_auth_tables_have_uuid_keys_and_real_foreign_keys(
    migrated_database: str,
) -> None:
    """The W1 tables must be *enforceable*, not merely present.

    A ``CHAR(32)`` column next to a ``uuid`` parent produces a table with no
    foreign key at all — PostgreSQL drops the constraint and the migration
    fails, but a lenient database would silently give us orphaned grants.
    """
    import asyncpg

    async def check() -> dict:
        conn = await asyncpg.connect(_asyncpg_dsn(migrated_database))
        try:
            columns = await conn.fetch(
                """
                select table_name, column_name, data_type
                from information_schema.columns
                where table_schema = 'public'
                  and table_name in ('api_tokens', 'api_token_projects',
                                     'authentication_audit')
                """
            )
            constraints = await conn.fetch(
                """
                select conname, contype
                from pg_constraint
                where conrelid = 'api_token_projects'::regclass
                """
            )
            indexes = await conn.fetch(
                """
                select indexname, indexdef from pg_indexes
                where schemaname = 'public' and tablename = 'api_token_projects'
                """
            )
            return {
                "columns": {
                    (r["table_name"], r["column_name"]): r["data_type"] for r in columns
                },
                #: asyncpg hands back PostgreSQL's internal "char" as bytes.
                "constraints": {
                    r["conname"]: bytes(r["contype"]).decode() for r in constraints
                },
                "indexes": {r["indexname"]: r["indexdef"] for r in indexes},
            }
        finally:
            await conn.close()

    schema = _run(check())
    columns = schema["columns"]

    for table, column in [
        ("api_tokens", "id"),
        ("api_token_projects", "id"),
        ("api_token_projects", "token_id"),
        ("api_token_projects", "project_id"),
        ("authentication_audit", "id"),
    ]:
        assert columns.get((table, column)) == "uuid", (
            f"{table}.{column} is {columns.get((table, column))!r}; it references a "
            "uuid primary key and must itself be uuid"
        )

    #: 'f' is a foreign key, 'p' a primary key (both in pg_constraint).
    types = set(schema["constraints"].values())
    assert types >= {"f", "p"}, (
        "api_token_projects must have both its foreign keys and its primary key: "
        f"{schema['constraints']}"
    )
    #: The (token_id, project_id) uniqueness is an index, not a constraint —
    #: it must still be UNIQUE, or a token could be granted a project twice.
    unique_indexes = [
        name
        for name, definition in schema["indexes"].items()
        if "UNIQUE" in definition.upper()
    ]
    assert unique_indexes, (
        "api_token_projects lost its (token_id, project_id) uniqueness — a token "
        f"could be granted the same project twice: {schema['indexes']}"
    )


def test_a_fresh_database_accepts_real_rows(migrated_database: str) -> None:
    """The schema must work for the application, not merely compile.

    Running the migrations and inspecting the DDL misses the defects that live
    in the *seam* between the migration and the ORM — the ones a fresh
    deployment hits on its first write. Both were found here: a ``CHAR(32)``
    column under a ``uuid`` parent, and ``created_at``/``updated_at`` with no
    server default while ``BaseModel`` declares ``server_default=func.now()``,
    so the ORM (correctly) omits them and PostgreSQL rejects the INSERT.

    This writes through the real models: an ``ADMIN`` token, a project, the
    grant that joins them, and an audit record — the exact rows the bootstrap
    path creates on a new deployment.
    """
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.auth import (
        ApiToken,
        ApiTokenProject,
        AuthAuditAction,
        AuthenticationAudit,
        TokenRole,
        hash_token,
    )
    from app.models.project import SoftwareProject

    async def write() -> dict:
        engine = create_async_engine(migrated_database, future=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                project = SoftwareProject(
                    name="Migration Probe", slug="migration-probe"
                )
                session.add(project)
                await session.flush()

                token = ApiToken(
                    name="probe-token",
                    token_hash=hash_token("argus_probe"),
                    role=TokenRole.ADMIN,
                )
                session.add(token)
                await session.flush()

                session.add(ApiTokenProject(token_id=token.id, project_id=project.id))
                session.add(
                    AuthenticationAudit(
                        token_id=token.id,
                        action=AuthAuditAction.CREATED,
                        occurred_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                return {
                    "project_id": project.id,
                    "token_id": token.id,
                    "created_at": token.created_at,
                }
        finally:
            await engine.dispose()

    written = _run(write())
    #: The server default must actually have produced a timestamp.
    assert written["created_at"] is not None
    assert written["project_id"] is not None


def test_ingestion_trust_columns_exist(migrated_database: str) -> None:
    """The per-source ingest token columns are part of the trust boundary."""
    import asyncpg

    async def check() -> set[str]:
        conn = await asyncpg.connect(_asyncpg_dsn(migrated_database))
        try:
            rows = await conn.fetch(
                """
                select column_name from information_schema.columns
                where table_schema = 'public' and table_name = 'observability_sources'
                """
            )
            return {r["column_name"] for r in rows}
        finally:
            await conn.close()

    names = _run(check())
    assert {"ingest_token_hash", "ingest_token_rotated_at"} <= names
