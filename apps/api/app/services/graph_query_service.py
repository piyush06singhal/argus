"""ARGUS Graph Query Service.

Read-side traversal over the software knowledge graph. Every entry point is
bounded: traversals honor ``max_depth`` (cap 25) and ``max_nodes`` (cap 2000)
and are cycle-safe via a visited set. ``compare_environments`` produces a
structural diff (nodes, edges, canonical components, endpoints, deployed
versions) between two environments of one project, keyed on human-meaningful
identity (node name / component name) so equivalent entities across
environments are recognized as the same thing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Set, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeType,
    GraphNode,
    GraphNodeStatus,
    GraphNodeType,
    ServiceEndpoint,
)
from app.models.project import Environment
from app.models.system import SystemComponent
from app.schemas.graph import (
    EnvComparisonResponse,
    EnvDiffCategory,
    EnvDiffItem,
    EnvSummary,
)

#: Kind values allowed by ``EnvDiffItem.kind`` / side by ``EnvDiffItem.environment``.
EnvRowKind = Literal["node", "edge", "component", "endpoint", "version"]
EnvSide = Literal["a", "b"]

#: Hard traversal caps (also enforced server-side at the API layer).
MAX_DEPTH_CAP = 25
MAX_NODES_CAP = 2000

#: Edge types that carry dependency flow. Structural/hierarchical edges
#: (CONTAINS, DEPLOYED_AS, IMPLEMENTS, HOSTS, RELATED_TO) annotate the graph
#: but must not pollute dependency answers: the environment does not *depend
#: on* its components, and a repository is not a *dependent* of the service it
#: implements (edge semantics, §29/§30).
TRAVERSABLE_EDGE_TYPES: Tuple[GraphEdgeType, ...] = (
    GraphEdgeType.DEPENDS_ON,
    GraphEdgeType.CALLS,
    GraphEdgeType.READS_FROM,
    GraphEdgeType.WRITES_TO,
    GraphEdgeType.PUBLISHES_TO,
    GraphEdgeType.CONSUMES_FROM,
)


@dataclass
class DependenciesResult:
    """Downstream/upstream closure for a node."""

    node_id: uuid.UUID
    direction: str
    direct: List[GraphNode] = field(default_factory=list)
    transitive: List[GraphNode] = field(default_factory=list)
    all_edges: List[GraphEdge] = field(default_factory=list)
    truncated: bool = False


@dataclass
class PathResult:
    """Shortest-path outcome between two nodes."""

    found: bool
    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)
    hops: int = 0


class GraphQueryService:
    """Bounded, cycle-safe graph queries scoped to one project/environment."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Read graph
    # ------------------------------------------------------------------
    async def get_graph(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        node_type: Optional[GraphNodeType] = None,
        status: Optional[GraphNodeStatus] = None,
        edge_type: Optional[GraphEdgeType] = None,
        source: Optional[GraphEdgeSource] = None,
        node_limit: int = 500,
        edge_limit: int = 2000,
    ) -> Tuple[List[GraphNode], List[GraphEdge]]:
        node_stmt = select(GraphNode).where(GraphNode.project_id == project_id)
        if environment_id is not None:
            # Null environment_id implies "all environments": anchor nodes
            # (project/environment) always accompanied the scoped read.
            node_stmt = node_stmt.where(
                or_(
                    GraphNode.environment_id == environment_id,
                    GraphNode.environment_id.is_(None),
                )
            )
        if node_type is not None:
            node_stmt = node_stmt.where(GraphNode.node_type == node_type)
        if status is not None:
            node_stmt = node_stmt.where(GraphNode.status == status)
        nodes = list(
            (
                await self._db.execute(
                    node_stmt.order_by(GraphNode.name).limit(node_limit)
                )
            )
            .scalars()
            .all()
        )

        edge_stmt = select(GraphEdge).where(GraphEdge.project_id == project_id)
        if environment_id is not None:
            edge_stmt = edge_stmt.where(
                or_(
                    GraphEdge.environment_id == environment_id,
                    GraphEdge.environment_id.is_(None),
                )
            )
        if edge_type is not None:
            edge_stmt = edge_stmt.where(GraphEdge.edge_type == edge_type)
        if source is not None:
            edge_stmt = edge_stmt.where(GraphEdge.source == source)
        edges = list(
            (
                await self._db.execute(
                    edge_stmt.order_by(GraphEdge.edge_type).limit(edge_limit)
                )
            )
            .scalars()
            .all()
        )
        return nodes, edges

    async def get_node(self, node_id: uuid.UUID) -> Optional[GraphNode]:
        return await self._db.get(GraphNode, node_id)

    # ------------------------------------------------------------------
    # Adjacency
    # ------------------------------------------------------------------
    async def _neighbors_map(
        self, node_ids: Set[uuid.UUID], limit: int = 2000
    ) -> Dict[uuid.UUID, List[Tuple[GraphEdge, GraphNode]]]:
        """Bidirectional adjacency for the given node ids (one shared fetch).

        Only dependency-carrying edge types participate in traversal.
        """
        if not node_ids:
            return {}
        outgoing = (
            await self._db.execute(
                select(GraphEdge, GraphNode)
                .join(GraphNode, GraphEdge.target_node_id == GraphNode.id)
                .where(GraphEdge.source_node_id.in_(node_ids))
                .where(GraphEdge.edge_type.in_(TRAVERSABLE_EDGE_TYPES))
                .limit(limit)
            )
        ).all()
        incoming = (
            await self._db.execute(
                select(GraphEdge, GraphNode)
                .join(GraphNode, GraphEdge.source_node_id == GraphNode.id)
                .where(GraphEdge.target_node_id.in_(node_ids))
                .where(GraphEdge.edge_type.in_(TRAVERSABLE_EDGE_TYPES))
                .limit(limit)
            )
        ).all()
        adj: Dict[uuid.UUID, List[Tuple[GraphEdge, GraphNode]]] = {}
        for edge, node in outgoing:
            adj.setdefault(edge.source_node_id, []).append((edge, node))
        for edge, node in incoming:
            adj.setdefault(edge.target_node_id, []).append((edge, node))
        return adj

    # ------------------------------------------------------------------
    # Dependencies / dependents / neighbors
    # ------------------------------------------------------------------
    async def get_dependencies(
        self,
        *,
        node_id: uuid.UUID,
        transitive: bool = False,
        max_depth: int = 10,
        max_nodes: int = 2000,
    ) -> DependenciesResult:
        """Downstream closure (source -> target BFS)."""
        return await self._closure(
            node_id=node_id,
            forward=True,
            transitive=transitive,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

    async def get_dependents(
        self,
        *,
        node_id: uuid.UUID,
        transitive: bool = False,
        max_depth: int = 10,
        max_nodes: int = 2000,
    ) -> DependenciesResult:
        """Upstream closure (target -> source BFS)."""
        return await self._closure(
            node_id=node_id,
            forward=False,
            transitive=transitive,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

    async def _closure(
        self,
        *,
        node_id: uuid.UUID,
        forward: bool,
        transitive: bool,
        max_depth: int,
        max_nodes: int,
    ) -> DependenciesResult:
        max_depth = min(max_depth or 10, MAX_DEPTH_CAP)
        max_nodes = min(max_nodes or 2000, MAX_NODES_CAP)
        direction = "outgoing" if forward else "incoming"
        result = DependenciesResult(node_id=node_id, direction=direction)

        start = await self._db.get(GraphNode, node_id)
        if start is None:
            return result

        visited: Set[uuid.UUID] = {node_id}
        depths: Dict[uuid.UUID, int] = {node_id: 0}
        edges: List[GraphEdge] = []
        frontier: Set[uuid.UUID] = {node_id}
        depth = 1
        truncated = False

        while frontier and depth <= max_depth and len(visited) <= max_nodes:
            nxt_frontier: Set[uuid.UUID] = set()
            for frontier_id in frontier:
                if depth >= 2 and not transitive:
                    continue
                adj = await self._neighbors_map({frontier_id})
                for edge, node in adj.get(frontier_id, []):
                    if forward and edge.target_node_id == frontier_id:
                        continue
                    if not forward and edge.source_node_id == frontier_id:
                        continue
                    candidate = edge.target_node_id if forward else edge.source_node_id
                    if candidate not in visited:
                        if len(visited) >= max_nodes:
                            truncated = True
                            break
                        visited.add(candidate)
                        depths[candidate] = depth
                        edges.append(edge)
                        nxt_frontier.add(candidate)
                if truncated:
                    break
            frontier = nxt_frontier
            depth += 1
            if truncated:
                break

        # Order nodes by (depth, name) for deterministic output.
        by_depth: Dict[uuid.UUID, GraphNode] = {}
        for nid in visited - {node_id}:
            row = await self._db.get(GraphNode, nid)
            if row is not None:
                by_depth[nid] = row

        def _append(items: List[GraphNode]) -> None:
            for nid in sorted(
                by_depth, key=lambda i: (depths.get(i, 99), by_depth[i].name)
            ):
                items.append(by_depth[nid])

        result.direct = [node for nid, node in by_depth.items() if depths[nid] == 1]
        if transitive:
            result.transitive = [
                node for nid, node in by_depth.items() if depths[nid] >= 2
            ]
        result.all_edges = edges
        result.truncated = truncated
        return result

    async def get_neighbors(
        self,
        *,
        node_id: uuid.UUID,
        max_depth: int = 1,
        max_nodes: int = 500,
    ) -> DependenciesResult:
        """Both directions at one hop (default); deeper on request."""
        if max_depth <= 1:
            result = DependenciesResult(node_id=node_id, direction="neighbors")
            start = await self._db.get(GraphNode, node_id)
            if start is None:
                return result
            adj = await self._neighbors_map({node_id}, limit=max_nodes)
            nodes: List[GraphNode] = []
            edges: List[GraphEdge] = []
            for edge, node in adj.get(node_id, []):
                if edge.source_node_id != node_id and edge.target_node_id != node_id:
                    continue
                nodes.append(node)
                edges.append(edge)
            # Dedup nodes, keep deterministic order.
            seen: Set[uuid.UUID] = set()
            unique: List[GraphNode] = []
            for node in sorted(nodes, key=lambda n: n.name):
                if node.id in seen:
                    continue
                seen.add(node.id)
                unique.append(node)
            result.direct = unique
            result.all_edges = edges
            return result
        return await self._closure(
            node_id=node_id,
            forward=True,
            transitive=True,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

    # ------------------------------------------------------------------
    # Path finding
    # ------------------------------------------------------------------
    async def find_path(
        self,
        *,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        max_depth: int = 10,
        max_nodes: int = 2000,
    ) -> PathResult:
        """Bounded BFS for a shortest (directed) path between two nodes."""
        max_depth = min(max_depth or 10, MAX_DEPTH_CAP)
        max_nodes = min(max_nodes or 2000, MAX_NODES_CAP)

        if source_id == target_id:
            source = await self._db.get(GraphNode, source_id)
            if source is None:
                return PathResult(found=False)
            return PathResult(found=True, nodes=[source], hops=0)

        # Security (§57/§59): both endpoints must belong to the same project —
        # never traverse across project boundaries.
        source_row = await self._db.get(GraphNode, source_id)
        target_row = await self._db.get(GraphNode, target_id)
        if (
            source_row is None
            or target_row is None
            or source_row.project_id != target_row.project_id
        ):
            return PathResult(found=False)

        parent: Dict[uuid.UUID, Tuple[uuid.UUID, GraphEdge]] = {}
        visited: Set[uuid.UUID] = {source_id}
        frontier: List[uuid.UUID] = [source_id]
        found = False

        for _depth in range(1, max_depth + 1):
            if not frontier:
                break
            nxt: List[uuid.UUID] = []
            for current in frontier:
                adj = await self._neighbors_map({current})
                for edge, node in adj.get(current, []):
                    if edge.source_node_id == current:
                        candidate = edge.target_node_id
                    elif edge.target_node_id == current:
                        candidate = edge.source_node_id
                    else:
                        continue
                    if candidate in visited:
                        continue
                    if len(visited) >= max_nodes:
                        break
                    visited.add(candidate)
                    parent[candidate] = (current, edge)
                    if candidate == target_id:
                        found = True
                        break
                    nxt.append(candidate)
                if found:
                    break
            if found:
                break
            frontier = nxt

        if not found:
            source = await self._db.get(GraphNode, source_id)
            target = await self._db.get(GraphNode, target_id)
            if source is None or target is None:
                return PathResult(found=False)
            return PathResult(found=False)

        # Reconstruct path from target back to source using the stored parent
        # edges (correct in both traversal directions, no re-query).
        node_ids: List[uuid.UUID] = [target_id]
        edges: List[GraphEdge] = []
        current = target_id
        while current != source_id:
            prev, edge = parent[current]
            node_ids.insert(0, prev)
            edges.insert(0, edge)
            current = prev

        nodes: List[GraphNode] = []
        for nid in node_ids:
            row = await self._db.get(GraphNode, nid)
            if row is not None:
                nodes.append(row)

        return PathResult(
            found=True,
            nodes=nodes,
            edges=edges,
            hops=len(node_ids) - 1,
        )

    # ------------------------------------------------------------------
    # Environment comparison
    # ------------------------------------------------------------------
    async def compare_environments(
        self,
        *,
        project_id: uuid.UUID,
        environment_a: uuid.UUID,
        environment_b: uuid.UUID,
    ) -> EnvComparisonResponse:
        a = await self._db.get(Environment, environment_a)
        b = await self._db.get(Environment, environment_b)
        if a is None or b is None:
            raise ValueError("comparison environments must exist")

        summary_a = await self._env_summary(project_id, a)
        summary_b = await self._env_summary(project_id, b)

        set_a = await self._env_keyed_sets(project_id, a)
        set_b = await self._env_keyed_sets(project_id, b)

        added: List[EnvDiffItem] = []
        removed: List[EnvDiffItem] = []
        changed: List[EnvDiffItem] = []

        all_keys = sorted(set(set_a.keys()) | set(set_b.keys()))
        for key in all_keys:
            row_a = set_a.get(key)
            row_b = set_b.get(key)
            if row_b is None:
                assert row_a is not None
                removed.append(row_a.to_diff(in_a=True, in_b=False, environment="a"))
            elif row_a is None:
                assert row_b is not None
                added.append(row_b.to_diff(in_a=False, in_b=True, environment="b"))
            else:
                assert row_a is not None and row_b is not None
                if row_a.version_identity != row_b.version_identity:
                    detail = {
                        f"{a.name}": row_a.version_detail(),
                        f"{b.name}": row_b.version_detail(),
                    }
                    changed.append(
                        row_b.to_diff(
                            in_a=True,
                            in_b=True,
                            environment="b",
                            detail=detail,
                        )
                    )

        return EnvComparisonResponse(
            project_id=project_id,
            environment_a=summary_a,
            environment_b=summary_b,
            added=added,
            removed=removed,
            changed=changed,
            labels={
                "added": f"Only in {b.name}",
                "removed": f"Only in {a.name}",
                "changed": "Version differs",
            },
        )

    async def _env_summary(self, project_id: uuid.UUID, env: Environment) -> EnvSummary:
        node_count = (
            await self._db.execute(
                select(func.count())
                .select_from(GraphNode)
                .where(
                    GraphNode.project_id == project_id,
                    or_(
                        GraphNode.environment_id == env.id,
                        GraphNode.environment_id.is_(None),
                    ),
                )
            )
        ).scalar() or 0
        edge_count = (
            await self._db.execute(
                select(func.count())
                .select_from(GraphEdge)
                .where(
                    GraphEdge.project_id == project_id,
                    or_(
                        GraphEdge.environment_id == env.id,
                        GraphEdge.environment_id.is_(None),
                    ),
                )
            )
        ).scalar() or 0
        component_count = (
            await self._db.execute(
                select(func.count())
                .select_from(SystemComponent)
                .where(
                    SystemComponent.project_id == project_id,
                    SystemComponent.environment_id == env.id,
                )
            )
        ).scalar() or 0
        return EnvSummary(
            environment_id=env.id,
            name=env.name,
            node_count=node_count,
            edge_count=edge_count,
            component_count=component_count,
        )

    async def _env_keyed_sets(
        self, project_id: uuid.UUID, env: Environment
    ) -> Dict[str, "EnvRow"]:
        """Structural fingerprints for one environment, keyed by identity."""
        rows: Dict[str, EnvRow] = {}

        nodes = (
            (
                await self._db.execute(
                    select(GraphNode).where(
                        GraphNode.project_id == project_id,
                        or_(
                            GraphNode.environment_id == env.id,
                            GraphNode.environment_id.is_(None),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        name_by_id: Dict[uuid.UUID, str] = {}
        for node in nodes:
            key = self._node_key(node)
            rows[key] = EnvRow(
                kind="node",
                category=EnvDiffCategory(
                    node_type=node.node_type.value if node.node_type else None,
                    name=node.name,
                    combined_key=key,
                ),
                name=node.name,
                key=key,
            )
            name_by_id[node.id] = node.name

        edges = (
            (
                await self._db.execute(
                    select(GraphEdge).where(
                        GraphEdge.project_id == project_id,
                        or_(
                            GraphEdge.environment_id == env.id,
                            GraphEdge.environment_id.is_(None),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for edge in edges:
            src_name = name_by_id.get(edge.source_node_id, str(edge.source_node_id)[:8])
            tgt_name = name_by_id.get(edge.target_node_id, str(edge.target_node_id)[:8])
            key = f"edge:{edge.edge_type.value}:{src_name}:{tgt_name}"
            if key not in rows:
                rows[key] = EnvRow(
                    kind="edge",
                    category=EnvDiffCategory(
                        node_type=None,
                        name=f"{src_name} -> {tgt_name}",
                        combined_key=key,
                    ),
                    name=f"{src_name} -> {tgt_name}",
                    key=key,
                )

        components = (
            (
                await self._db.execute(
                    select(SystemComponent).where(
                        SystemComponent.project_id == project_id,
                        SystemComponent.environment_id == env.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for comp in components:
            key = f"component:{comp.name}"
            rows[key] = EnvRow(
                kind="component",
                category=EnvDiffCategory(
                    node_type=GraphNodeType.COMPONENT.value,
                    name=comp.name,
                    combined_key=key,
                ),
                name=comp.name,
                key=key,
            )

        endpoints = (
            await self._db.execute(
                select(ServiceEndpoint, SystemComponent)
                .join(
                    SystemComponent, SystemComponent.id == ServiceEndpoint.component_id
                )
                .where(
                    ServiceEndpoint.project_id == project_id,
                    ServiceEndpoint.environment_id == env.id,
                )
            )
        ).all()
        for endpoint, comp in endpoints:
            key = f"endpoint:{comp.name}:{endpoint.method} {endpoint.path_template}"
            rows[key] = EnvRow(
                kind="endpoint",
                category=EnvDiffCategory(
                    node_type=None,
                    name=f"{comp.name} {endpoint.method} {endpoint.path_template}",
                    combined_key=key,
                ),
                name=f"{endpoint.method} {endpoint.path_template}",
                key=key,
            )

        # Latest deployed version per component (deterministic: newest first).
        deploys = (
            (
                await self._db.execute(
                    select(DeploymentEvent)
                    .where(
                        DeploymentEvent.project_id == project_id,
                        DeploymentEvent.environment_id == env.id,
                        DeploymentEvent.component_id.isnot(None),
                        DeploymentEvent.status.in_(
                            [DeploymentStatus.SUCCESS, DeploymentStatus.STARTED]
                        ),
                    )
                    .order_by(DeploymentEvent.deployed_at.desc())
                )
            )
            .scalars()
            .all()
        )
        latest_by_comp: Dict[uuid.UUID, DeploymentEvent] = {}
        for event in deploys:
            if event.component_id is not None:
                latest_by_comp.setdefault(event.component_id, event)
        for comp in components:
            latest_dep = latest_by_comp.get(comp.id)
            version = (latest_dep.version or "") if latest_dep else ""
            key = f"version:{comp.name}"
            row = EnvRow(
                kind="version",
                category=EnvDiffCategory(
                    node_type=None,
                    name=comp.name,
                    combined_key=key,
                ),
                name=comp.name,
                key=key,
                version_identity=version,
            )
            row._version_detail = {"version": version}
            rows[key] = row

        return rows

    @staticmethod
    def _node_key(node: GraphNode) -> str:
        external = node.external_identifier or ""
        return (
            f"node:{node.node_type.value if node.node_type else ''}"
            f":{node.name}:{external}"
        )


class EnvRow:
    """Internal fingerprint used to diff one environment's structure."""

    __slots__ = (
        "kind",
        "category",
        "name",
        "key",
        "version_identity",
        "_version_detail",
    )
    kind: EnvRowKind
    environment: EnvSide

    def __init__(
        self,
        *,
        kind: EnvRowKind,
        category: EnvDiffCategory,
        name: str,
        key: str,
        version_identity: str = "",
    ) -> None:
        self.kind = kind
        self.category = category
        self.name = name
        self.key = key
        self.version_identity = version_identity
        self._version_detail: Optional[dict] = None

    def version_detail(self) -> dict:
        return self._version_detail or {"version": self.version_identity}

    def to_diff(
        self,
        *,
        in_a: bool,
        in_b: bool,
        environment: EnvSide,
        detail: Optional[dict] = None,
    ) -> EnvDiffItem:
        return EnvDiffItem(
            kind=self.kind,
            category=self.category,
            key=self.key,
            name=self.name,
            in_a=in_a,
            in_b=in_b,
            environment=environment,
            detail=detail,
        )
