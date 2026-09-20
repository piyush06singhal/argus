"""Phase 2 — graph snapshot service: create/list/diff/contents."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNode,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.graph_registry import ComponentRegistry
from app.services.graph_snapshot_service import GraphSnapshotService


async def _seed(
    db_session: AsyncSession,
    names: tuple[str, ...] = ("web", "gateway"),
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    project = SoftwareProject(name="Snap Proj", slug="snap-proj")
    db_session.add(project)
    await db_session.flush()
    env = Environment(project_id=project.id, name="prod", environment_type="PRODUCTION")
    db_session.add(env)
    await db_session.flush()

    registry = ComponentRegistry(db_session)
    node_ids: dict[str, uuid.UUID] = {}
    for name in names:
        comp = SystemComponent(
            project_id=project.id,
            environment_id=env.id,
            name=name,
            component_type="SERVICE",
        )
        db_session.add(comp)
        await db_session.flush()
        node = await registry.get_or_create_component_node(comp)
        node_ids[name] = node.id
    return project.id, env.id, node_ids


def _edge(project_id: uuid.UUID, source: uuid.UUID, target: uuid.UUID) -> GraphEdge:
    return GraphEdge(
        project_id=project_id,
        source_node_id=source,
        target_node_id=target,
        edge_type=GraphEdgeType.CALLS,
        source=GraphEdgeSource.TRACE,
        confidence=0.9,
        status=GraphEdgeStatus.ACTIVE,
    )


class TestSnapshotCreateList:
    async def test_create_and_list_version_increments(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, node_ids = await _seed(db_session)
        service = GraphSnapshotService(db_session)

        v1 = await service.create_snapshot(
            project_id=project_id, environment_id=env_id, caption="baseline"
        )
        assert v1.snapshot_version == 1
        assert v1.node_count == 2
        assert v1.previous_snapshot_id is None
        first_id = v1.id

        # Grow the graph and snapshot again.
        comp = SystemComponent(
            project_id=project_id,
            environment_id=env_id,
            name="checkout",
            component_type="SERVICE",
        )
        db_session.add(comp)
        await db_session.flush()
        registry = ComponentRegistry(db_session)
        checkout_node = await registry.get_or_create_component_node(comp)
        db_session.add(_edge(project_id, node_ids["web"], checkout_node.id))
        await db_session.flush()

        v2 = await service.create_snapshot(
            project_id=project_id, environment_id=env_id, source="CONFIGURATION"
        )
        assert v2.snapshot_version == 2
        assert v2.previous_snapshot_id == first_id
        assert v2.node_count == 3
        assert v2.edge_count == 1

        rows, total = await service.list_snapshots(
            project_id=project_id, environment_id=env_id
        )
        assert total == 2
        assert rows[0].snapshot_version == 2  # newest first

    async def test_separate_sequences_per_environment(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, _ = await _seed(db_session)
        service = GraphSnapshotService(db_session)

        # Project-level (environment_id NULL) and env-scoped sequences are
        # independent version chains.
        project_snap = await service.create_snapshot(project_id=project_id)
        env_snap = await service.create_snapshot(
            project_id=project_id, environment_id=env_id
        )
        assert project_snap.snapshot_version == 1
        assert env_snap.snapshot_version == 1

    async def test_no_graph_raises(self, db_session: AsyncSession) -> None:
        project = SoftwareProject(name="Empty", slug="empty")
        db_session.add(project)
        await db_session.flush()

        service = GraphSnapshotService(db_session)
        with pytest.raises(ValueError, match="No graph to snapshot"):
            await service.create_snapshot(project_id=project.id)


class TestSnapshotDiff:
    async def test_diff_captures_added_node(self, db_session: AsyncSession) -> None:
        project_id, env_id, _ = await _seed(db_session)
        service = GraphSnapshotService(db_session)
        v1 = await service.create_snapshot(project_id=project_id, environment_id=env_id)

        comp = SystemComponent(
            project_id=project_id,
            environment_id=env_id,
            name="new-service",
            component_type="SERVICE",
        )
        db_session.add(comp)
        await db_session.flush()
        node = await ComponentRegistry(db_session).get_or_create_component_node(comp)
        await db_session.flush()

        v2 = await service.create_snapshot(project_id=project_id, environment_id=env_id)
        diff = await service.diff_snapshots(v1.id, v2.id)
        assert str(node.id) in diff.added_nodes
        assert "new-service" in diff.added_node_names
        assert diff.removed_nodes == []

    async def test_diff_removed_after_node_deletion(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, node_ids = await _seed(db_session)
        service = GraphSnapshotService(db_session)
        v1 = await service.create_snapshot(project_id=project_id, environment_id=env_id)
        # Remove one node from the live graph, then snapshot again: the second
        # snapshot no longer contains it (nodes are edge-free here).
        gone = node_ids["gateway"]
        node = await db_session.get(GraphNode, gone)
        assert node is not None
        await db_session.delete(node)
        await db_session.flush()

        v2 = await service.create_snapshot(project_id=project_id, environment_id=env_id)
        diff = await service.diff_snapshots(v1.id, v2.id)
        # The deleted id must be classified as a *node* (not an edge) purely
        # from the signature+counts — it no longer exists in graph_nodes.
        assert str(gone) in diff.removed_nodes
        assert "gateway" not in diff.added_nodes
        # Name lookup is best-effort: a deleted node has no row to name.
        assert diff.removed_node_names == []
        assert diff.added_nodes == []


class TestSnapshotContents:
    async def test_contents_reflects_live_scope(self, db_session: AsyncSession) -> None:
        project_id, env_id, _ = await _seed(db_session)
        service = GraphSnapshotService(db_session)
        snap = await service.create_snapshot(
            project_id=project_id, environment_id=env_id
        )

        contents = await service.snapshot_contents(snap.id)
        assert contents.snapshot_version == 1
        assert len(contents.nodes) == 2
        assert {n.name for n in contents.nodes} == {"web", "gateway"}
        assert len(contents.signature) >= 2

    async def test_contents_not_found(self, db_session: AsyncSession) -> None:
        service = GraphSnapshotService(db_session)
        with pytest.raises(ValueError, match="snapshot not found"):
            await service.snapshot_contents(uuid.uuid4())
