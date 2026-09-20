"""ARGUS Graph Snapshot Service.

Point-in-time snapshots of a project/environment graph. A snapshot freezes the
node/edge id sets it captured (``graph_signature``) along with counts and a
chronological version per (project, environment) sequence. Diffing compares
signatures only (historical record); ``snapshot_contents`` reads the live
tables at request time for the snapshot's scope.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional, Tuple, Type

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphNode,
    GraphSnapshot,
)
from app.schemas.graph import (
    GraphEdgeResponse,
    GraphNodeResponse,
    SnapshotDetailResponse,
    SnapshotDiffResponse,
)


class GraphSnapshotService:
    """Snapshot lifecycle for a project/environment graph."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    async def list_snapshots(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[GraphSnapshot], int]:
        """Snapshots newest-first; returns (rows, total)."""
        base = select(GraphSnapshot).where(GraphSnapshot.project_id == project_id)
        if environment_id is not None:
            base = base.where(GraphSnapshot.environment_id == environment_id)
        total = (
            await self._db.execute(select(func.count()).select_from(base.subquery()))
        ).scalar() or 0
        rows = list(
            (
                await self._db.execute(
                    base.order_by(GraphSnapshot.snapshot_version.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return rows, int(total)

    async def get_snapshot(self, snapshot_id: uuid.UUID) -> Optional[GraphSnapshot]:
        return await self._db.get(GraphSnapshot, snapshot_id)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    async def create_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        source: GraphEdgeSource = GraphEdgeSource.MANUAL,
        caption: Optional[str] = None,
    ) -> GraphSnapshot:
        node_count, nodes = await self._count_and_ids(
            project_id, environment_id, GraphNode
        )
        if node_count == 0:
            raise ValueError("No graph to snapshot")
        edge_count, edges = await self._count_and_ids(
            project_id, environment_id, GraphEdge
        )
        signature = [*sorted(str(i) for i in nodes), *sorted(str(i) for i in edges)]

        # Version sequence per (project, COALESCE(environment_id)) so the
        # NULL-collapsing unique index is respected.
        version_stmt = select(func.max(GraphSnapshot.snapshot_version)).where(
            GraphSnapshot.project_id == project_id
        )
        if environment_id is None:
            version_stmt = version_stmt.where(GraphSnapshot.environment_id.is_(None))
        else:
            version_stmt = version_stmt.where(
                GraphSnapshot.environment_id == environment_id
            )
        max_version = (await self._db.execute(version_stmt)).scalar() or 0
        version = int(max_version) + 1

        previous: Optional[uuid.UUID] = None
        if max_version:
            prev_stmt = select(GraphSnapshot.id).where(
                GraphSnapshot.project_id == project_id,
                GraphSnapshot.snapshot_version == max_version,
            )
            if environment_id is None:
                prev_stmt = prev_stmt.where(GraphSnapshot.environment_id.is_(None))
            else:
                prev_stmt = prev_stmt.where(
                    GraphSnapshot.environment_id == environment_id
                )
            previous = (await self._db.execute(prev_stmt.limit(1))).scalar_one_or_none()

        snapshot = GraphSnapshot(
            project_id=project_id,
            environment_id=environment_id,
            snapshot_version=version,
            node_count=node_count,
            edge_count=edge_count,
            source=source,
            caption=caption,
            graph_signature=signature,
            previous_snapshot_id=previous,
        )
        self._db.add(snapshot)
        await self._db.flush()
        return snapshot

    async def _count_and_ids(
        self,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        model: Type[Any],
    ) -> Tuple[int, List[uuid.UUID]]:
        env_col = getattr(model, "environment_id")
        stmt = select(model)
        if environment_id is not None:
            stmt = stmt.where(
                or_(
                    env_col == environment_id,
                    env_col.is_(None),
                )
            )
        rows = (await self._db.execute(stmt)).scalars().all()
        return len(rows), [getattr(row, "id") for row in rows]

    # ------------------------------------------------------------------
    # Diff + contents
    # ------------------------------------------------------------------
    async def diff_snapshots(
        self, a_id: uuid.UUID, b_id: uuid.UUID
    ) -> SnapshotDiffResponse:
        a = await self._db.get(GraphSnapshot, a_id)
        b = await self._db.get(GraphSnapshot, b_id)
        if a is None or b is None:
            raise ValueError("both snapshots must exist")

        # Signature layout is `sorted(node ids) + sorted(edge ids)`; each
        # snapshot records how many leading entries are nodes via node_count,
        # so the split is reliable even for ids deleted since capture.
        sig_a = a.graph_signature or []
        sig_b = b.graph_signature or []
        a_nodes = set(sig_a[: a.node_count])
        a_edges = set(sig_a[a.node_count :])
        b_nodes = set(sig_b[: b.node_count])
        b_edges = set(sig_b[b.node_count :])

        added_nodes = sorted(b_nodes - a_nodes)
        removed_nodes = sorted(a_nodes - b_nodes)
        added_edges = sorted(b_edges - a_edges)
        removed_edges = sorted(a_edges - b_edges)

        return SnapshotDiffResponse(
            a_id=a_id,
            b_id=b_id,
            added_nodes=added_nodes,
            removed_nodes=removed_nodes,
            added_edges=added_edges,
            removed_edges=removed_edges,
            added_node_names=await self._node_names(added_nodes),
            removed_node_names=await self._node_names(removed_nodes),
        )

    async def snapshot_contents(self, snapshot_id: uuid.UUID) -> SnapshotDetailResponse:
        snapshot = await self._db.get(GraphSnapshot, snapshot_id)
        if snapshot is None:
            raise ValueError("snapshot not found")

        node_stmt = select(GraphNode).where(GraphNode.project_id == snapshot.project_id)
        edge_stmt = select(GraphEdge).where(GraphEdge.project_id == snapshot.project_id)
        if snapshot.environment_id is not None:
            node_stmt = node_stmt.where(
                or_(
                    GraphNode.environment_id == snapshot.environment_id,
                    GraphNode.environment_id.is_(None),
                )
            )
            edge_stmt = edge_stmt.where(
                or_(
                    GraphEdge.environment_id == snapshot.environment_id,
                    GraphEdge.environment_id.is_(None),
                )
            )
        nodes = list(
            (await self._db.execute(node_stmt.order_by(GraphNode.name))).scalars().all()
        )
        edges = list(
            (await self._db.execute(edge_stmt.order_by(GraphEdge.edge_type)))
            .scalars()
            .all()
        )
        return SnapshotDetailResponse(
            id=snapshot.id,
            project_id=snapshot.project_id,
            environment_id=snapshot.environment_id,
            snapshot_version=snapshot.snapshot_version,
            node_count=snapshot.node_count,
            edge_count=snapshot.edge_count,
            source=snapshot.source,
            caption=snapshot.caption,
            previous_snapshot_id=snapshot.previous_snapshot_id,
            created_at=snapshot.created_at,
            nodes=[GraphNodeResponse.model_validate(n) for n in nodes],
            edges=[GraphEdgeResponse.model_validate(e) for e in edges],
            signature=snapshot.graph_signature or [],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _node_names(self, node_ids: List[str]) -> List[str]:
        if not node_ids:
            return []
        rows = await self._db.execute(
            select(GraphNode.name).where(
                GraphNode.id.in_([uuid.UUID(i) for i in node_ids])
            )
        )
        return sorted(set(rows.scalars().all()))
