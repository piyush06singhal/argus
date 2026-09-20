"""ARGUS Graph Validator.

Runs deterministic data-quality checks over the knowledge-graph overlay and
returns findings as ``DataQualityIssue`` records. Checks are read-only; the
caller (``GraphDataQualityService``) persists each finding as a
``GraphDataQualityRecord`` and aggregates them into a project health summary.

Severity semantics:

- ``ERROR`` — structural contradictions the graph must not have (edges dangling
  into missing nodes, environment-scope mismatches).
- ``WARNING`` — ambiguity or drift worth reviewing (orphaned component nodes,
  duplicate component names, unresolved endpoints, conflicting aliases).
- ``INFO`` — observable state, not a defect (stale edges, lone repositories).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    DataQualitySeverity,
    GraphEdge,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNode,
    GraphNodeAlias,
    GraphNodeType,
    ServiceEndpoint,
)

#: Node kinds that legitimately exist without incident edges.
_ANCHOR_TYPES = (GraphNodeType.PROJECT, GraphNodeType.ENVIRONMENT)


@dataclass
class DataQualityIssue:
    """A single validator finding."""

    check_type: str
    severity: DataQualitySeverity
    detail: dict


class GraphValidator:
    """Deterministic, bounded data-quality checks over one project's graph."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    async def run_checks(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> List[DataQualityIssue]:
        """Run every check for the project/environment and return findings."""
        issues: List[DataQualityIssue] = []
        for check in (
            self._check_orphan_nodes,
            self._check_orphan_edges,
            self._check_duplicate_components,
            self._check_unresolved_endpoints,
            self._check_env_scope,
            self._check_stale_relationships,
            self._check_conflicting_identity,
            self._check_lone_repository,
        ):
            issues.extend(
                await check(project_id=project_id, environment_id=environment_id)
            )
        return issues

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    async def _check_orphan_nodes(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        stmt = select(GraphNode).where(
            GraphNode.project_id == project_id,
            GraphNode.node_type.not_in(_ANCHOR_TYPES),
            ~select(GraphEdge.id)
            .where(
                or_(
                    GraphEdge.source_node_id == GraphNode.id,
                    GraphEdge.target_node_id == GraphNode.id,
                )
            )
            .exists(),
        )
        if environment_id is not None:
            stmt = stmt.where(GraphNode.environment_id == environment_id)
        rows = (await self._db.execute(stmt)).scalars().all()
        return [
            DataQualityIssue(
                check_type="orphan_nodes",
                severity=DataQualitySeverity.WARNING,
                detail={
                    "node_id": str(node.id),
                    "name": node.name,
                    "node_type": node.node_type.value if node.node_type else None,
                },
            )
            for node in rows
        ]

    async def _check_orphan_edges(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        issues: List[DataQualityIssue] = []
        for missing, node_column in (
            ("source", GraphEdge.source_node_id),
            ("target", GraphEdge.target_node_id),
        ):
            stmt = (
                select(GraphEdge)
                .outerjoin(GraphNode, node_column == GraphNode.id)
                .where(GraphEdge.project_id == project_id, GraphNode.id.is_(None))
            )
            if environment_id is not None:
                stmt = stmt.where(GraphEdge.environment_id == environment_id)
            for edge in (await self._db.execute(stmt)).scalars().all():
                issues.append(
                    self._edge_issue(
                        edge,
                        "orphan_edges",
                        DataQualitySeverity.ERROR,
                        extra={
                            "missing": missing,
                            "note": "edge references a node that does not exist",
                        },
                    )
                )
        return issues

    async def _check_duplicate_components(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        stmt = (
            select(
                func.lower(GraphNode.name).label("key"),
                func.count(GraphNode.id).label("count"),
            )
            .where(
                GraphNode.project_id == project_id,
                # Duplicate detection keys on the canonical mirror kind, not a
                # single node_type: §7 projects components onto typed nodes.
                GraphNode.entity_kind == "system_component",
            )
            .group_by(func.lower(GraphNode.name))
            .having(func.count(GraphNode.id) > 1)
        )
        if environment_id is not None:
            stmt = stmt.where(GraphNode.environment_id == environment_id)
        rows = (await self._db.execute(stmt)).all()
        issues: List[DataQualityIssue] = []
        for key, count in rows:
            dup_rows = (
                (
                    await self._db.execute(
                        select(GraphNode.id).where(
                            GraphNode.project_id == project_id,
                            GraphNode.entity_kind == "system_component",
                            func.lower(GraphNode.name) == key,
                        )
                    )
                )
                .scalars()
                .all()
            )
            issues.append(
                DataQualityIssue(
                    check_type="duplicate_components",
                    severity=DataQualitySeverity.WARNING,
                    detail={
                        "name": key,
                        "count": count,
                        "node_ids": [str(i) for i in dup_rows],
                    },
                )
            )
        return issues

    async def _check_unresolved_endpoints(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        ep_stmt = select(ServiceEndpoint).where(
            ServiceEndpoint.project_id == project_id
        )
        if environment_id is not None:
            ep_stmt = ep_stmt.where(ServiceEndpoint.environment_id == environment_id)
        endpoints = (await self._db.execute(ep_stmt)).scalars().all()
        component_ids = {e.component_id for e in endpoints}
        if not component_ids:
            return []
        node_rows = await self._db.execute(
            select(GraphNode.entity_id).where(
                GraphNode.project_id == project_id,
                GraphNode.entity_kind == "system_component",
                GraphNode.entity_id.in_(component_ids),
            )
        )
        resolved = set(node_rows.scalars().all())
        missing = component_ids - resolved
        if not missing:
            return []
        return [
            DataQualityIssue(
                check_type="unresolved_endpoints",
                severity=DataQualitySeverity.WARNING,
                detail={
                    "component_id": str(cid),
                    "endpoint_count": sum(
                        1 for e in endpoints if e.component_id == cid
                    ),
                },
            )
            for cid in sorted(missing, key=str)
        ]

    async def _check_env_scope(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        """Flag edges whose environment disagrees with an incident node's."""
        edge_rows = await self._db.execute(
            select(GraphEdge).where(
                GraphEdge.project_id == project_id,
                GraphEdge.environment_id.isnot(None),
            )
        )
        edges = edge_rows.scalars().all()
        if environment_id is not None:
            edges = [e for e in edges if e.environment_id == environment_id]
        issues: List[DataQualityIssue] = []
        for edge in edges:
            mismatches: List[str] = []
            for node_id, side in (
                (edge.source_node_id, "source"),
                (edge.target_node_id, "target"),
            ):
                node = await self._db.get(GraphNode, node_id)
                if (
                    node is not None
                    and node.environment_id is not None
                    and node.environment_id != edge.environment_id
                ):
                    mismatches.append(side)
            if mismatches:
                issues.append(
                    self._edge_issue(
                        edge,
                        "env_scope_mismatch",
                        DataQualitySeverity.ERROR,
                        extra={
                            "edge_environment_id": str(edge.environment_id),
                            "mismatching_sides": mismatches,
                        },
                    )
                )
        return issues

    async def _check_stale_relationships(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        stmt = (
            select(func.count())
            .select_from(GraphEdge)
            .where(
                GraphEdge.project_id == project_id,
                GraphEdge.status == GraphEdgeStatus.STALE,
            )
        )
        if environment_id is not None:
            stmt = stmt.where(GraphEdge.environment_id == environment_id)
        count = (await self._db.execute(stmt)).scalar() or 0
        if count == 0:
            return []
        return [
            DataQualityIssue(
                check_type="stale_relationships",
                severity=DataQualitySeverity.INFO,
                detail={
                    "count": count,
                    "note": (
                        "edges whose last_seen_at is older than "
                        "GRAPH_STALE_AFTER_DAYS are flagged, never deleted"
                    ),
                },
            )
        ]

    async def _check_conflicting_identity(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        rows = (
            await self._db.execute(
                select(
                    func.lower(GraphNodeAlias.alias).label("alias"),
                    func.count(func.distinct(GraphNodeAlias.node_id)).label("nodes"),
                )
                .where(GraphNodeAlias.project_id == project_id)
                .group_by(func.lower(GraphNodeAlias.alias))
                .having(func.count(func.distinct(GraphNodeAlias.node_id)) > 1)
            )
        ).all()
        issues: List[DataQualityIssue] = []
        for alias, nodes in rows:
            node_rows = await self._db.execute(
                select(GraphNodeAlias.node_id, GraphNode.name)
                .join(GraphNode, GraphNode.id == GraphNodeAlias.node_id)
                .where(
                    GraphNodeAlias.project_id == project_id,
                    func.lower(GraphNodeAlias.alias) == alias,
                )
            )
            issues.append(
                DataQualityIssue(
                    check_type="conflicting_alias",
                    severity=DataQualitySeverity.WARNING,
                    detail={
                        "alias": alias,
                        "node_count": nodes,
                        "nodes": [
                            {"node_id": str(nid), "name": name}
                            for nid, name in node_rows.all()
                        ],
                    },
                )
            )
        return issues

    async def _check_lone_repository(
        self, *, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> List[DataQualityIssue]:
        stmt = select(GraphNode).where(
            GraphNode.project_id == project_id,
            GraphNode.node_type == GraphNodeType.REPOSITORY,
            ~select(GraphEdge.id)
            .where(
                and_(
                    GraphEdge.edge_type == GraphEdgeType.IMPLEMENTS,
                    or_(
                        GraphEdge.source_node_id == GraphNode.id,
                        GraphEdge.target_node_id == GraphNode.id,
                    ),
                )
            )
            .exists(),
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return [
            DataQualityIssue(
                check_type="lone_repository",
                severity=DataQualitySeverity.INFO,
                detail={"name": node.name, "node_id": str(node.id)},
            )
            for node in rows
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _edge_issue(
        self,
        edge: GraphEdge,
        check_type: str,
        severity: DataQualitySeverity,
        extra: Optional[dict] = None,
    ) -> DataQualityIssue:
        detail = {
            "edge_id": str(edge.id),
            "edge_type": edge.edge_type.value if edge.edge_type else None,
            "source_node_id": str(edge.source_node_id),
            "target_node_id": str(edge.target_node_id),
        }
        if extra:
            detail.update(extra)
        return DataQualityIssue(check_type=check_type, severity=severity, detail=detail)
