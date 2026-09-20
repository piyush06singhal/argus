"""Phase 2 — trace extraction: typed edges, idempotency, orphans, env isolation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeType,
    GraphNode,
)
from app.models.observability import LogRecord, SpanRecord, TraceStatus
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.graph_extractor import GraphExtractor
from app.services.graph_reconciler import GraphReconciler
from app.services.graph_registry import ComponentRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_topology(
    db_session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, Dict[str, uuid.UUID]]:
    """Web -> Gateway -> Checkout -> Inventory -> Postgres + Redis.

    Returns (project_id, env_id, {name: component_id}).
    """
    project = SoftwareProject(name="Trace Proj", slug="trace-proj")
    db_session.add(project)
    await db_session.flush()
    env = Environment(project_id=project.id, name="prod", environment_type="PRODUCTION")
    db_session.add(env)
    await db_session.flush()

    specs = [
        ("web", "SERVICE"),
        ("gateway", "SERVICE"),
        ("checkout", "SERVICE"),
        ("inventory", "SERVICE"),
        ("postgres", "DATABASE"),
        ("redis", "CACHE"),
    ]
    pending: list[tuple[str, SystemComponent]] = []
    for name, ctype in specs:
        comp = SystemComponent(
            project_id=project.id,
            environment_id=env.id,
            name=name,
            component_type=ctype,
        )
        db_session.add(comp)
        pending.append((name, comp))
    await db_session.flush()
    # PK defaults (uuid.uuid4) are applied at flush — capture ids afterwards.
    comps: Dict[str, uuid.UUID] = {name: comp.id for name, comp in pending}

    spans = [
        ("root-trace-1", "sp-web-1", None, "web", 10),
        ("root-trace-1", "sp-gw-1", "sp-web-1", "gateway", 20),
        ("root-trace-1", "sp-ck-1", "sp-gw-1", "checkout", 30),
        ("root-trace-1", "sp-inv-1", "sp-ck-1", "inventory", 40),
        ("root-trace-1", "sp-pg-1", "sp-inv-1", "postgres", 50),
        ("root-trace-1", "sp-rd-1", "sp-ck-1", "redis", 60),
    ]
    rows = [
        _span(project.id, comps, t, s, parent, cname, ms)
        for t, s, parent, cname, ms in spans
    ]
    db_session.add_all(rows)
    await db_session.flush()
    return project.id, env.id, comps


def _span(
    project_id: uuid.UUID,
    comps: Dict[str, uuid.UUID],
    trace: str,
    span_id: str,
    parent: str | None,
    comp_name: str | None,
    offset_ms: int,
) -> SpanRecord:
    now = _now()
    return SpanRecord(
        trace_id=trace,
        span_id=span_id,
        parent_span_id=parent,
        project_id=project_id,
        component_id=comps.get(comp_name) if comp_name else None,
        operation=span_id,
        start_time=now,
        end_time=now,
        duration_ms=offset_ms,
        status=TraceStatus.OK,
        metadata_=(None if not comp_name else {}),
    )


async def _edge_tuples(db_session: AsyncSession) -> set[tuple]:
    rows = await db_session.execute(select(GraphEdge))
    out: set[tuple] = set()
    for e in rows.scalars().all():
        out.add(
            (
                e.edge_type.value,
                e.source.value,
                e.source_node_id,
                e.target_node_id,
                e.environment_id,
            )
        )
    return out


async def _graph_node_ids(
    db_session: AsyncSession, project_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """Graph node ids (mirrored under the component's name) for edge assertions."""
    rows = await db_session.execute(
        select(GraphNode.name, GraphNode.id).where(GraphNode.project_id == project_id)
    )
    return {name: nid for name, nid in rows.all()}


class TestTraceEdges:
    async def test_trace_builds_typed_edges(self, db_session: AsyncSession) -> None:
        project_id, _, comps = await _seed_topology(db_session)
        extractor = GraphExtractor(db_session, ComponentRegistry(db_session))
        spans = (await db_session.execute(select(SpanRecord))).scalars().all()
        stats = await extractor.extract_trace_graph(
            project_id=project_id, environment_id=None, spans=spans
        )
        await db_session.flush()

        assert stats.spans_seen == 6
        gids = await _graph_node_ids(db_session, project_id)
        assert "web" in gids
        edges = await _edge_tuples(db_session)
        # web -> gateway      CALLS
        # inventory -> postgres READS_FROM (target DATABASE)
        # checkout -> redis   READS_FROM (target CACHE)
        assert ("CALLS", "TRACE", gids["web"], gids["gateway"], None) in edges
        assert (
            "READS_FROM",
            "TRACE",
            gids["inventory"],
            gids["postgres"],
            None,
        ) in edges
        assert ("READS_FROM", "TRACE", gids["checkout"], gids["redis"], None) in edges

        all_sources = await db_session.execute(
            select(func.count())
            .select_from(GraphEdge)
            .where(
                GraphEdge.source == GraphEdgeSource.TRACE,
                GraphEdge.confidence == 0.9,
            )
        )
        assert all_sources.scalar_one() == 5  # 6 spans -> 5 edges

    async def test_repeated_trace_upserts_not_duplicates(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, _ = await _seed_topology(db_session)
        extractor = GraphExtractor(db_session, ComponentRegistry(db_session))
        spans = (await db_session.execute(select(SpanRecord))).scalars().all()
        await extractor.extract_trace_graph(
            project_id=project_id, environment_id=None, spans=spans
        )
        await db_session.flush()
        first_count = (
            await db_session.execute(
                select(func.count())
                .select_from(GraphEdge)
                .where(GraphEdge.source == GraphEdgeSource.TRACE)
            )
        ).scalar_one()

        await extractor.extract_trace_graph(
            project_id=project_id, environment_id=None, spans=spans
        )
        await db_session.flush()
        second_count = (
            await db_session.execute(
                select(func.count())
                .select_from(GraphEdge)
                .where(GraphEdge.source == GraphEdgeSource.TRACE)
            )
        ).scalar_one()

        assert first_count == 5
        assert second_count == 5  # no duplicates

    async def test_orphan_and_missing_parent_handled(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, comps = await _seed_topology(db_session)
        extractor = GraphExtractor(db_session, ComponentRegistry(db_session))

        # A span with a dangling parent id (parent not present anywhere), and a
        # root-only span (no parent) with a component — must produce no edges.
        orphan = _span(
            project_id,
            comps,
            "orphan-trace",
            "sp-orphan",
            "sp-missing-parent",
            "web",
            1,
        )
        standalone = _span(
            project_id, comps, "standalone-trace", "sp-standalone", None, "checkout", 2
        )
        db_session.add_all([orphan, standalone])
        await db_session.flush()

        stats = await extractor.extract_trace_graph(
            project_id=project_id, environment_id=None, spans=[orphan, standalone]
        )
        await db_session.flush()

        assert stats.edges_created == 0
        assert stats.edges_updated == 0
        # No edge may be minted for an unresolved parent (orphan) or a bare
        # standalone root — the graph must stay untouched.
        edges = (await db_session.execute(select(GraphEdge))).scalars().all()
        assert edges == []

    async def test_late_spans(self, db_session: AsyncSession) -> None:
        project_id, _, comps = await _seed_topology(db_session)
        extractor = GraphExtractor(db_session, ComponentRegistry(db_session))

        # Parent gran already ingested; only the child arrives now.
        parent = _span(
            project_id, comps, "late-trace", "sp-late-parent", None, "web", 1
        )
        db_session.add(parent)
        await db_session.flush()
        child = _span(
            project_id,
            comps,
            "late-trace",
            "sp-late-child",
            "sp-late-parent",
            "gateway",
            2,
        )
        db_session.add(child)
        await db_session.flush()

        await extractor.extract_trace_graph(
            project_id=project_id, environment_id=None, spans=[child]
        )
        await db_session.flush()

        gids = await _graph_node_ids(db_session, project_id)
        edges = await _edge_tuples(db_session)
        assert ("CALLS", "TRACE", gids["web"], gids["gateway"], None) in edges

    async def test_multiple_environments_isolated(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, comps = await _seed_topology(db_session)
        env2 = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(env2)
        await db_session.flush()

        extractor = GraphExtractor(db_session, ComponentRegistry(db_session))
        spans = (await db_session.execute(select(SpanRecord))).scalars().all()
        await extractor.extract_trace_graph(
            project_id=project_id, environment_id=env_id, spans=spans
        )
        await extractor.extract_trace_graph(
            project_id=project_id, environment_id=env2.id, spans=spans
        )
        await db_session.flush()

        gids = await _graph_node_ids(db_session, project_id)
        edges = await _edge_tuples(db_session)
        assert ("CALLS", "TRACE", gids["web"], gids["gateway"], env_id) in edges
        assert ("CALLS", "TRACE", gids["web"], gids["gateway"], env2.id) in edges

    async def test_trace_does_not_override_configured(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, comps = await _seed_topology(db_session)
        registry = ComponentRegistry(db_session)
        reconciler = GraphReconciler(db_session, registry)

        # Configure a MANUAL READS_FROM edge on the SAME key trace evidence
        # targets (inventory -> postgres), then feed spans at it.
        inv = await db_session.get(SystemComponent, comps["inventory"])
        pg = await db_session.get(SystemComponent, comps["postgres"])
        assert inv is not None and pg is not None
        inv_node = await registry.get_or_create_component_node(inv)
        pg_node = await registry.get_or_create_component_node(pg)
        manual, _ = await reconciler.upsert_edge(
            project_id=project_id,
            source_node_id=inv_node.id,
            target_node_id=pg_node.id,
            edge_type=GraphEdgeType.READS_FROM,
            source=GraphEdgeSource.MANUAL,
            confidence=1.0,
        )
        await db_session.flush()

        extractor = GraphExtractor(db_session, registry, reconciler=reconciler)
        spans = (await db_session.execute(select(SpanRecord))).scalars().all()
        await extractor.extract_trace_graph(
            project_id=project_id, environment_id=None, spans=spans
        )
        await db_session.flush()

        edge = (
            await db_session.execute(
                select(GraphEdge).where(
                    GraphEdge.source_node_id == inv_node.id,
                    GraphEdge.target_node_id == pg_node.id,
                    GraphEdge.edge_type == GraphEdgeType.READS_FROM,
                )
            )
        ).scalar_one()
        assert edge.id == manual.id
        assert edge.source == GraphEdgeSource.MANUAL  # configured-wins
        sources = (edge.metadata_ or {}).get("sources", [])
        assert "TRACE" in sources


class TestLogReferences:
    async def test_log_reference_edges(self, db_session: AsyncSession) -> None:
        project_id, _, comps = await _seed_topology(db_session)
        extractor = GraphExtractor(db_session, ComponentRegistry(db_session))

        # Graph nodes must exist for name-based target resolution.
        registry = ComponentRegistry(db_session)
        checkout = await db_session.get(SystemComponent, comps["checkout"])
        inventory = await db_session.get(SystemComponent, comps["inventory"])
        assert checkout is not None and inventory is not None
        await registry.get_or_create_component_node(checkout)
        await registry.get_or_create_component_node(inventory)
        await db_session.flush()

        log = LogRecord(
            project_id=project_id,
            component_id=comps["checkout"],
            timestamp=_now(),
            level="INFO",
            message="sent order to checkout aggregator",
            metadata_={"peer_service": "inventory"},
        )
        db_session.add(log)
        await db_session.flush()

        stats = await extractor.extract_log_references(project_id=project_id)
        await db_session.flush()

        assert stats.edges_created >= 1
        gids = await _graph_node_ids(db_session, project_id)
        edges = await _edge_tuples(db_session)
        assert ("CALLS", "LOG", gids["checkout"], gids["inventory"], None) in edges
