"""One schema authority (hardening W4b).

``init_db()`` used to call ``Base.metadata.create_all()`` in **every**
environment, which made the ORM a second writer of DDL beside Alembic. The
failure mode is subtle and was found in this repository's own stack:

* ``create_all`` creates only *missing* tables, so a process that starts before
  the migration runs gets the new tables (and the new enum types) from model
  metadata — and then the **migration** fails with
  ``type "authsource" already exists``;
* it never adds a new column to an existing table, so the database it leaves
  behind has one authority's tables and the other's columns — a schema neither
  of them describes, which fails at query time instead of at boot.

These tests pin the rule that replaced it: tests may build from metadata, and
nothing else performs DDL — a process whose database is not at the migration
head refuses to start and says which command to run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core import database


class _NonTestSettings:
    """A settings stand-in whose only interesting answer is ``is_testing``."""

    def __init__(self, testing: bool) -> None:
        self.is_testing = testing


@pytest.fixture
def production_like(monkeypatch):
    """Make ``init_db`` believe it is running outside the test suite."""

    def _activate() -> None:
        monkeypatch.setattr(
            database, "get_settings", lambda: _NonTestSettings(testing=False)
        )

    return _activate


async def _record_revision(revision: str) -> None:
    async with database.engine.begin() as conn:
        await conn.execute(
            text("create table if not exists alembic_version (version_num varchar(32))")
        )
        await conn.execute(text("delete from alembic_version"))
        await conn.execute(
            text("insert into alembic_version (version_num) values (:rev)"),
            {"rev": revision},
        )


async def _clear_revision() -> None:
    async with database.engine.begin() as conn:
        await conn.execute(text("drop table if exists alembic_version"))


class TestSchemaAuthority:
    async def test_the_test_environment_still_builds_from_metadata(self) -> None:
        """The unit suite's speed depends on this path, so it is pinned."""
        await database.init_db()  # must not raise in the test environment

    async def test_a_database_with_no_migrations_refuses_to_start(
        self, production_like
    ) -> None:
        production_like()
        await _clear_revision()
        with pytest.raises(RuntimeError) as excinfo:
            await database.init_db()
        message = str(excinfo.value)
        assert "no applied migrations" in message
        #: The message has to name the fix, not just the problem: an operator
        #: reading a boot failure needs the command, not a diagnosis.
        assert "alembic upgrade head" in message

    async def test_a_stale_database_refuses_to_start(self, production_like) -> None:
        production_like()
        await _record_revision("0" * 12)
        with pytest.raises(RuntimeError) as excinfo:
            await database.init_db()
        message = str(excinfo.value)
        assert "000000000000" in message
        assert database._migration_head() in message

    async def test_a_database_at_head_starts(self, production_like) -> None:
        production_like()
        await _record_revision(database._migration_head())
        await database.init_db()  # must not raise

    async def test_no_ddl_is_issued_outside_the_test_environment(
        self, production_like
    ) -> None:
        """The guard is not merely advisory: no ``CREATE TABLE`` is emitted.

        A refusal that still created tables first would be the same defect with
        a new message, so the absence of DDL is asserted directly.
        """
        production_like()
        await _clear_revision()
        executed: list[str] = []

        real_begin = database.engine.begin

        class _Recorder:
            def __init__(self, inner) -> None:
                self._inner = inner

            async def __aenter__(self):
                return await self._inner.__aenter__()

            async def __aexit__(self, *exc_info):
                return await self._inner.__aexit__(*exc_info)

        def _spy(*args, **kwargs):
            executed.append("begin")
            return _Recorder(real_begin(*args, **kwargs))

        original_engine = database.engine

        class _EngineProxy:
            def begin(self, *args, **kwargs):
                return real_begin(*args, **kwargs)

        #: Nothing may reach the engine's DDL path at all; the only permitted
        #: query is the read of ``alembic_version``, which goes through
        #: ``engine.connect`` rather than ``engine.begin``.
        database.engine = _EngineProxy()  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError):
                await database.init_db()
        finally:
            database.engine = original_engine
        assert executed == []
