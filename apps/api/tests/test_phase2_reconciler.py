"""Phase 2 — reconciler: mirroring, provenance (configured-wins), stale policy."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    DiscoveredComponentStatus,
    GraphDiscoveryRecord,
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    GraphReconciliationRun,
)
from app.models.observability import SpanRecord, TraceStatus
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentDependency, DependencyType, SystemComponent
from app.services.graph_discovery import GraphDiscoveryEngine
from app.services.graph_reconciler import GraphReconciler
from app.services.graph_registry import ComponentRegistry


async def _seed(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Project + env + two components; returns (project_id, env_id, checkout_id)."""
    project = SoftwareProject(name="Rec Proj", slug="rec-proj")
    db_session.add(project)
    await db_session.flush()
    env = Environment(project_id=project.id, name="prod", environment_type="PRODUCTION")
    db_session.add(env)
    await db_session.flush()
    checkout = SystemComponent(
        project_id=project.id,
        environment_id=env.id,
        name="Checkout",
        component_type="SERVICE",
    )
    inventory = SystemComponent(
        project_id=project.id,
        environment_id=env.id,
        name="Inventory",
        component_type="SERVICE",
    )
    db_session.add_all([checkout, inventory])
    await db_session.flush()
    db_session.add(
        ComponentDependency(
            source_component_id=checkout.id,
            target_component_id=inventory.id,
            dependency_type=DependencyType.HTTP,
        )
    )
    await db_session.flush()
    return project.id, env.id, checkout.id


async def _node_ids(
    db_session: AsyncSession, project_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    rows = await db_session.execute(
        select(GraphNode.name, GraphNode.id).where(GraphNode.project_id == project_id)
    )
    return {name: rid for name, rid in rows.all()}


class TestReconcileMirror:
    async def test_reconcile_mirrors_components_and_deps(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout_id = await _seed(db_session)
        reconciler = GraphReconciler(db_session, ComponentRegistry(db_session))
        result = await reconciler.reconcile(project_id=project_id)
        await db_session.flush()

        assert result.nodes_created >= 3  # project + env + 2 components
        assert result.edges_created >= 1  # DEPENDS_ON checkout -> inventory

        ids = await _node_ids(db_session, project_id)
        assert "Checkout" in ids and "Inventory" in ids
        dep_edges = (
            (
                await db_session.execute(
                    select(GraphEdge).where(
                        GraphEdge.edge_type == GraphEdgeType.DEPENDS_ON,
                        GraphEdge.source == GraphEdgeSource.CONFIGURATION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(dep_edges) >= 1

    async def test_reconcile_is_idempotent(self, db_session: AsyncSession) -> None:
        project_id, _, _ = await _seed(db_session)
        reconciler = GraphReconciler(db_session, ComponentRegistry(db_session))
        first = await reconciler.reconcile(project_id=project_id)
        await db_session.flush()
        second = await reconciler.reconcile(project_id=project_id)
        await db_session.flush()

        assert second.nodes_created == 0
        assert second.edges_created == 0
        assert first.nodes_created >= 3

    async def test_run_recorded(self, db_session: AsyncSession) -> None:
        project_id, _, _ = await _seed(db_session)
        reconciler = GraphReconciler(db_session, ComponentRegistry(db_session))
        result = await reconciler.reconcile(project_id=project_id)
        await db_session.flush()

        run = await db_session.get(GraphReconciliationRun, result.run_id)
        assert run is not None
        assert run.nodes_created == result.nodes_created
        assert run.edges_created == result.edges_created


class TestProvenance:
    async def test_configured_wins_over_trace(self, db_session: AsyncSession) -> None:
        project_id, _, _ = await _seed(db_session)
        reconciler = GraphReconciler(db_session, ComponentRegistry(db_session))
        await reconciler.reconcile(project_id=project_id)
        await db_session.flush()
        ids = await _node_ids(db_session, project_id)
        a, b = ids["Checkout"], ids["Inventory"]

        # A MANUAL CALLS edge on a key the reconcile mirror did not touch
        # (reconcile only writes DEPENDS_ON for this pair).
        manual, _ = await reconciler.upsert_edge(
            project_id=project_id,
            source_node_id=a,
            target_node_id=b,
            edge_type=GraphEdgeType.CALLS,
            source=GraphEdgeSource.MANUAL,
            confidence=0.5,
        )
        await db_session.flush()

        # Trace evidence on the SAME key must not override the manual edge.
        trace_edge, mode = await reconciler.upsert_edge(
            project_id=project_id,
            source_node_id=a,
            target_node_id=b,
            edge_type=GraphEdgeType.CALLS,
            source=GraphEdgeSource.TRACE,
            confidence=0.9,
        )
        await db_session.flush()

        assert trace_edge.id == manual.id
        assert trace_edge.source == GraphEdgeSource.MANUAL
        assert mode in ("updated_nop", "updated_evidence")
        sources = (trace_edge.metadata_ or {}).get("sources", [])
        assert "TRACE" in sources


class TestStale:
    async def test_stale_marking_and_never_delete(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, _ = await _seed(db_session)
        # stale_after_days is a constructor knob; reconcile() applies it.
        reconciler = GraphReconciler(
            db_session, ComponentRegistry(db_session), stale_after_days=30
        )
        await reconciler.reconcile(project_id=project_id)
        await db_session.flush()
        ids = await _node_ids(db_session, project_id)
        a, b = ids["Checkout"], ids["Inventory"]

        # We create a CALLS edge far in the past that reconcile won't touch.
        edge, _ = await reconciler.upsert_edge(
            project_id=project_id,
            source_node_id=a,
            target_node_id=b,
            edge_type=GraphEdgeType.CALLS,
            source=GraphEdgeSource.TRACE,
            confidence=0.9,
        )
        old = datetime.now(timezone.utc) - timedelta(days=40)
        edge.last_seen_at = old
        await db_session.flush()

        result = await reconciler.reconcile(project_id=project_id)
        await db_session.flush()

        assert result.edges_marked_stale >= 1
        refreshed = (
            await db_session.execute(select(GraphEdge).where(GraphEdge.id == edge.id))
        ).scalar_one()
        assert refreshed.status == GraphEdgeStatus.STALE
        # Never deleted on staling.
        raw = await db_session.execute(
            select(func.count()).select_from(GraphEdge).where(GraphEdge.id == edge.id)
        )
        assert raw.scalar_one() == 1


class TestNoDelete:
    async def test_no_delete_on_missing_evidence(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, _ = await _seed(db_session)
        reconciler = GraphReconciler(db_session, ComponentRegistry(db_session))
        await reconciler.reconcile(project_id=project_id)
        await db_session.flush()

        # Delete the canonical dependency — reconcile must retain the edge.
        dep = await db_session.execute(select(ComponentDependency))
        dep_row = dep.scalar_one()
        await db_session.delete(dep_row)
        await db_session.flush()

        await reconciler.reconcile(project_id=project_id)
        await db_session.flush()

        edges = (
            (
                await db_session.execute(
                    select(GraphEdge).where(
                        GraphEdge.edge_type == GraphEdgeType.DEPENDS_ON
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(edges) >= 1


def _evidence_span(
    project_id: uuid.UUID,
    component_id: uuid.UUID | None,
    metadata_: dict | None,
    span_id: str = "sp-ev-1",
) -> SpanRecord:
    now = datetime.now(timezone.utc)
    return SpanRecord(
        trace_id="disc-trace-1",
        span_id=span_id,
        parent_span_id=None,
        project_id=project_id,
        component_id=component_id,
        operation="internal",
        start_time=now,
        end_time=now,
        duration_ms=1,
        status=TraceStatus.OK,
        metadata_=metadata_,
    )


class TestDiscoveryEngine:
    async def _seed_disc(
        self, db_session: AsyncSession
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        """Project + env + Checkout/Inventory; returns ids + mirrored graph nodes."""
        project = SoftwareProject(name="Disc Proj", slug="disc-proj")
        db_session.add(project)
        await db_session.flush()
        env = Environment(
            project_id=project.id, name="prod", environment_type="PRODUCTION"
        )
        db_session.add(env)
        await db_session.flush()
        checkout = SystemComponent(
            project_id=project.id,
            environment_id=env.id,
            name="Checkout",
            component_type="SERVICE",
        )
        inventory = SystemComponent(
            project_id=project.id,
            environment_id=env.id,
            name="Inventory",
            component_type="SERVICE",
        )
        db_session.add_all([checkout, inventory])
        await db_session.flush()
        # Mirror the components so their names resolve as known nodes.
        registry = ComponentRegistry(db_session)
        await registry.get_or_create_component_node(checkout)
        await registry.get_or_create_component_node(inventory)
        await db_session.flush()
        return project.id, env.id, checkout.id, inventory.id

    async def test_suggest_creates_pending_and_skips_known(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout_id, inventory_id = await self._seed_disc(
            db_session
        )
        engine = GraphDiscoveryEngine(db_session, ComponentRegistry(db_session))
        db_session.add(
            _evidence_span(
                project_id, checkout_id, {"peer_service": "ml-scorer", "http.host": "x"}
            )
        )
        # Referencing a mirrored component by name — resolvable, not a candidate.
        db_session.add(
            _evidence_span(
                project_id,
                inventory_id,
                {"peer_service": "Checkout"},
                span_id="sp-ev-2",
            )
        )
        await db_session.flush()

        records, stats = await engine.suggest(
            project_id=project_id, environment_id=env_id
        )
        await db_session.flush()

        assert stats.candidates_seen == 2
        assert stats.records_created == 1
        assert len(records) == 1
        (record,) = records
        assert record.discovered_name == "ml-scorer"
        assert record.status == DiscoveredComponentStatus.PENDING
        assert record.evidence_count >= 1
        assert "peer_service" in (record.evidence_sources or [])

        # No automatic node was created.
        rows = await db_session.execute(
            select(func.count())
            .select_from(GraphNode)
            .where(GraphNode.name == "ml-scorer")
        )
        assert rows.scalar_one() == 0

    async def test_suggest_is_incremental(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout_id, _ = await self._seed_disc(db_session)
        engine = GraphDiscoveryEngine(db_session, ComponentRegistry(db_session))
        db_session.add(
            _evidence_span(project_id, checkout_id, {"service.name": "ml-scorer"})
        )
        await db_session.flush()

        await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()
        _, stats = await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()

        assert stats.records_created == 0
        assert stats.records_updated == 1
        row = await db_session.execute(select(GraphDiscoveryRecord))
        (record,) = row.scalars().all()
        assert record.status == DiscoveredComponentStatus.PENDING
        assert "service.name" in (record.evidence_sources or [])

    async def test_register_creates_generic_node_once(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout_id, _ = await self._seed_disc(db_session)
        engine = GraphDiscoveryEngine(db_session, ComponentRegistry(db_session))
        db_session.add(
            _evidence_span(project_id, checkout_id, {"peer_service": "ml-scorer"})
        )
        await db_session.flush()
        records, _ = await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()

        # Default registration adopts the discovered name, so the raw evidence
        # name now resolves to the node.
        node = await engine.register(records[0].id, node_type=GraphNodeType.SERVICE)
        await db_session.flush()

        assert node.name == "ml-scorer"
        assert node.node_type == GraphNodeType.SERVICE
        assert node.entity_kind == "generic"
        assert node.entity_id is not None
        status = await db_session.get(GraphDiscoveryRecord, records[0].id)
        assert (
            status is not None and status.status == DiscoveredComponentStatus.REGISTERED
        )

        # Registering again is rejected — resolution is one-shot.
        with pytest.raises(ValueError, match="not found or not pending"):
            await engine.register(records[0].id)

        # A REGISTERED name no longer resurfaces as a candidate: now that a
        # node carries the name, re-seeing it resolves rather than re-bumps.
        db_session.add(
            _evidence_span(
                project_id,
                checkout_id,
                {"peer_service": "ml-scorer"},
                span_id="sp-ev-r2",
            )
        )
        await db_session.flush()
        _, stats = await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()
        assert stats.records_created == 0
        assert stats.records_updated == 0

    async def test_ignore_is_terminal(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout_id, _ = await self._seed_disc(db_session)
        engine = GraphDiscoveryEngine(db_session, ComponentRegistry(db_session))
        db_session.add(
            _evidence_span(project_id, checkout_id, {"peer_service": "crypto-worker"})
        )
        await db_session.flush()
        records, _ = await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()

        await engine.ignore(records[0].id)
        await db_session.flush()
        status = await db_session.get(GraphDiscoveryRecord, records[0].id)
        assert status is not None and status.status == DiscoveredComponentStatus.IGNORED

        # Ignored records cannot be registered and do not reappear.
        with pytest.raises(ValueError, match="not found or not pending"):
            await engine.register(records[0].id)
        db_session.add(
            _evidence_span(
                project_id,
                checkout_id,
                {"peer_service": "crypto-worker"},
                span_id="sp-ev-ig2",
            )
        )
        await db_session.flush()
        _, stats = await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()
        assert stats.records_created == 0
        assert stats.records_updated == 0

    async def test_list_records_filters_by_status(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout_id, _ = await self._seed_disc(db_session)
        engine = GraphDiscoveryEngine(db_session, ComponentRegistry(db_session))
        for i, peer in enumerate(("ml-scorer", "crypto-worker", "rec-notifier")):
            db_session.add(
                _evidence_span(
                    project_id,
                    checkout_id,
                    {"peer_service": peer},
                    span_id=f"sp-ev-l{i}",
                )
            )
        await db_session.flush()
        records, _ = await engine.suggest(project_id=project_id, environment_id=env_id)
        await db_session.flush()
        await engine.ignore(
            next(r for r in records if r.discovered_name == "crypto-worker").id
        )
        await db_session.flush()

        pending, total = await engine.list_records(
            project_id=project_id, status=DiscoveredComponentStatus.PENDING
        )
        assert total == 2
        assert {r.discovered_name for r in pending} == {"ml-scorer", "rec-notifier"}
        ignored, _ = await engine.list_records(
            project_id=project_id, status=DiscoveredComponentStatus.IGNORED
        )
        assert {r.discovered_name for r in ignored} == {"crypto-worker"}
