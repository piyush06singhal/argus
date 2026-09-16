"""Regression tests for seed data idempotency.

The seed runs on every container boot (docker-entrypoint.sh). It must tolerate
pre-existing rows — a user who created projects via the API, or whose DB was
seeded by an earlier run, must not crash the API on restart.

The full dataset is only inserted against PostgreSQL (Docker); SQLite is used
for the test suite. These tests therefore exercise the idempotency DECISION
(the code that was actually broken) and the early-skip path, not the whole
insert graph.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.core.database import async_session_factory
from app.models.project import SoftwareProject
from seed_data import _seed_data_exists, seed_demo_data

SEED_SLUG = "argus-demo-commerce"


async def _count_projects() -> int:
    async with async_session_factory() as db:
        result = await db.execute(select(func.count(SoftwareProject.id)))
        return int(result.scalar() or 0)


async def _seed_detected() -> bool:
    async with async_session_factory() as db:
        return await _seed_data_exists(db)


async def test_seed_not_present_on_empty_db() -> None:
    """A fresh database has no seed marker yet."""
    assert await _count_projects() == 0
    assert not await _seed_detected()


async def test_seed_marker_detected_after_seeding() -> None:
    """The marker resolves to True once the seed project exists."""
    async with async_session_factory() as db:
        db.add(SoftwareProject(name="ARGUS Demo Commerce", slug=SEED_SLUG))
        await db.commit()
    assert await _seed_detected()


async def test_seed_skips_when_present_with_other_projects() -> None:
    """Re-seeding with user projects present must no-op cleanly.

    Regression: the old ``select(SoftwareProject)`` + ``scalar_one_or_none()``
    check raised MultipleResultsFound once a second project existed, crash-looping
    the API container on every boot after a user created projects.
    """
    async with async_session_factory() as db:
        db.add(SoftwareProject(name="ARGUS Demo Commerce", slug=SEED_SLUG))
        db.add(SoftwareProject(name="User Project A", slug="user-project-a"))
        db.add(SoftwareProject(name="User Project B", slug="user-project-b"))
        await db.commit()

    # Seed must return early — no crash, no duplicates.
    await seed_demo_data()
    await seed_demo_data()
    assert await _count_projects() == 3


async def test_seed_marker_ignores_other_projects() -> None:
    """User projects alone must not be mistaken for the demo data."""
    async with async_session_factory() as db:
        db.add(SoftwareProject(name="User Project", slug="user-project"))
        await db.commit()
    assert not await _seed_detected()