"""Regression tests for seed data idempotency + Phase 2 graph seeding.

The seed runs on every container boot (docker-entrypoint.sh). It must tolerate
pre-existing rows — a user who created projects via the API, or whose DB was
seeded by an earlier run, must not crash the API on restart.

The full dataset runs against the suite's SQLite engine here (same models and
services the live stack uses), so the Phase 2 graph seeding path is exercised
directly, not only its idempotency decision.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.database import async_session_factory
from app.models.deployment import CodeRepository
from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    GraphSnapshot,
    ServiceEndpoint,
)
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


async def test_seed_populates_graph_overlay() -> None:
    """§72–75: a fresh seed materializes the demo knowledge graph."""
    await seed_demo_data()

    async with async_session_factory() as db:
        nodes = (await db.execute(select(GraphNode))).scalars().all()
        edges = (await db.execute(select(GraphEdge))).scalars().all()
        node_names = {n.name for n in nodes}
        node_types = {n.node_type for n in nodes}

        # Demo topology mirrored: components + anchors + external API + repo.
        assert {
            "Web Frontend",
            "API Gateway",
            "Checkout Service",
            "Inventory Service",
            "Payment Service",
            "PostgreSQL",
            "Redis",
            "External Payment API",
        } <= node_names
        assert GraphNodeType.EXTERNAL_API in node_types
        assert GraphNodeType.REPOSITORY in node_types

        # Configured dependency edges + trace-derived edges + IMPLEMENTS.
        assert any(e.edge_type == GraphEdgeType.DEPENDS_ON for e in edges)
        assert any(e.source == GraphEdgeSource.TRACE for e in edges)
        assert any(e.edge_type == GraphEdgeType.IMPLEMENTS for e in edges)
        assert any(e.source == GraphEdgeSource.REPOSITORY for e in edges)

        # Endpoints + repos + snapshot v1.
        endpoint_count = (
            await db.execute(select(func.count(ServiceEndpoint.id)))
        ).scalar()
        assert (endpoint_count or 0) >= 7
        repo_count = (await db.execute(select(func.count(CodeRepository.id)))).scalar()
        assert (repo_count or 0) == 2
        snapshot_count = (
            await db.execute(select(func.count(GraphSnapshot.id)))
        ).scalar()
        assert (snapshot_count or 0) == 1


async def test_seed_rerun_adds_no_duplicate_graph_rows() -> None:
    """A re-run is a no-op — no duplicate nodes/edges/endpoints."""
    await seed_demo_data()
    async with async_session_factory() as db:
        nodes_before = (await db.execute(select(func.count(GraphNode.id)))).scalar()
        edges_before = (await db.execute(select(func.count(GraphEdge.id)))).scalar()
        endpoints_before = (
            await db.execute(select(func.count(ServiceEndpoint.id)))
        ).scalar()

    await seed_demo_data()  # early-skip on the marker

    async with async_session_factory() as db:
        nodes_after = (await db.execute(select(func.count(GraphNode.id)))).scalar()
        edges_after = (await db.execute(select(func.count(GraphEdge.id)))).scalar()
        endpoints_after = (
            await db.execute(select(func.count(ServiceEndpoint.id)))
        ).scalar()

    assert nodes_after == nodes_before
    assert edges_after == edges_before
    assert endpoints_after == endpoints_before
