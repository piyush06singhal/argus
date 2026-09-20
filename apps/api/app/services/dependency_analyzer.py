"""ARGUS Dependency Analyzer (Phase 4 §13–§14).

Builds the *structural* context for one incident from the Phase 0
``component_dependencies`` table **and** the Phase 2 knowledge-graph
``graph_edges`` (trace-discovered relationships), bounded by
``CAUSAL_MAX_DEPENDENCY_HOPS``.

What a structural relationship means — and does not mean (§14):

* "A calls B" establishes a **structural** channel through which failure can
  travel. It is never, by itself, evidence that B caused A's failure.
* The analyzer therefore labels every edge it returns with ``DIRECT`` /
  ``INDIRECT`` / ``UNRELATED`` and never assigns polarity.

Shared-dependency detection (two seemingly unrelated failing components both
calling one struggling service) is a classic causal funnel, so shared targets
are returned explicitly for the candidate generator to consider.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import GraphEdge, GraphEdgeStatus
from app.models.system import ComponentDependency


class StructuralRelation(str, Enum):
    """How strongly the dependency model links two components."""

    DIRECT = "DIRECT"  # adjacent: dependency edge or graph edge between them
    INDIRECT = "INDIRECT"  # connected through intermediaries within max_hops
    SHARED_DEPENDENCY = "SHARED_DEPENDENCY"  # both call the same target
    UNRELATED = "UNRELATED"


@dataclass(frozen=True)
class DependencyEdgeInfo:
    """One structural edge, with the table it came from (provenance)."""

    source_component_id: uuid.UUID
    target_component_id: uuid.UUID
    dependency_type: Optional[str]
    origin: str  # "component_dependencies" | "graph_edges"

    def as_pair(self) -> tuple[uuid.UUID, uuid.UUID]:
        return (self.source_component_id, self.target_component_id)


@dataclass
class DependencyContext:
    """The bounded structural neighbourhood of a set of components."""

    #: All components in scope (the failing set + their neighbourhood).
    component_ids: set[uuid.UUID] = field(default_factory=set)
    edges: list[DependencyEdgeInfo] = field(default_factory=list)
    #: target_id -> callers that share it (the funnel view).
    shared_dependencies: dict[uuid.UUID, set[uuid.UUID]] = field(default_factory=dict)
    #: Component -> everything it calls (outgoing) within the hop budget.
    upstream_providers: dict[uuid.UUID, set[uuid.UUID]] = field(default_factory=dict)
    #: Component -> everything that calls it (incoming).
    downstream_dependents: dict[uuid.UUID, set[uuid.UUID]] = field(default_factory=dict)
    truncated: bool = False

    def relation(self, a: uuid.UUID, b: uuid.UUID) -> StructuralRelation:
        """Classify the structural relation between two components (§13)."""
        if a == b:
            return StructuralRelation.DIRECT
        pair = frozenset((a, b))
        for edge in self.edges:
            if frozenset(edge.as_pair()) == pair:
                return StructuralRelation.DIRECT
        if b in self.upstream_providers.get(
            a, set()
        ) or a in self.upstream_providers.get(b, set()):
            return StructuralRelation.DIRECT
        if self._reachable(a, b) or self._reachable(b, a):
            return StructuralRelation.INDIRECT
        for target, callers in self.shared_dependencies.items():
            if a in callers and b in callers:
                return StructuralRelation.SHARED_DEPENDENCY
        return StructuralRelation.UNRELATED

    def _reachable(self, start: uuid.UUID, goal: uuid.UUID) -> bool:
        seen: set[uuid.UUID] = set()
        stack = [start]
        adjacency: dict[uuid.UUID, set[uuid.UUID]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source_component_id, set()).add(
                edge.target_component_id
            )
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, set()) - seen)
        return False

    def providers_of(self, component_id: uuid.UUID) -> set[uuid.UUID]:
        """Components this one calls (its dependencies = candidate origins)."""
        return set(self.upstream_providers.get(component_id, set()))


class DependencyAnalyzer:
    """Loads the bounded structural neighbourhood for an incident's components."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        max_hops: int = 4,
        max_edges: int = 2000,
    ) -> None:
        self._session = session
        self._max_hops = max(1, int(max_hops))
        self._max_edges = max_edges

    async def build_context(
        self,
        project_id: uuid.UUID,
        component_ids: set[uuid.UUID],
        environment_id: uuid.UUID | None = None,
    ) -> DependencyContext:
        """Bounded BFS from the failing set over both structural sources."""
        context = DependencyContext(component_ids=set(component_ids))
        if not component_ids:
            return context

        dep_edges = await self._load_component_dependencies(project_id, component_ids)
        graph_edges = await self._load_graph_edges(
            project_id, component_ids, environment_id
        )
        context.edges = dep_edges + graph_edges
        if len(context.edges) >= self._max_edges:
            context.truncated = True
            context.edges = context.edges[: self._max_edges]

        self._expand_bounded(context)
        self._find_shared_dependencies(context)
        return context

    # -- Loaders ------------------------------------------------------------
    async def _load_component_dependencies(
        self, project_id: uuid.UUID, component_ids: set[uuid.UUID]
    ) -> list[DependencyEdgeInfo]:
        # ``component_dependencies`` predates project scoping (Phase 0 model
        # carries no project_id), so scoping happens through the components.
        from app.models.system import SystemComponent

        stmt = (
            select(ComponentDependency)
            .join(
                SystemComponent,
                SystemComponent.id == ComponentDependency.source_component_id,
            )
            .where(
                SystemComponent.project_id == project_id,
                or_(
                    ComponentDependency.source_component_id.in_(component_ids),
                    ComponentDependency.target_component_id.in_(component_ids),
                ),
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            DependencyEdgeInfo(
                source_component_id=row.source_component_id,
                target_component_id=row.target_component_id,
                dependency_type=getattr(
                    getattr(row, "dependency_type", None), "value", None
                )
                or (str(row.dependency_type) if row.dependency_type else None),
                origin="component_dependencies",
            )
            for row in rows
        ]

    async def _load_graph_edges(
        self,
        project_id: uuid.UUID,
        component_ids: set[uuid.UUID],
        environment_id: uuid.UUID | None,
    ) -> list[DependencyEdgeInfo]:
        """Phase 2 graph edges between system_component nodes (CALLS etc.)."""
        stmt = select(GraphEdge).where(
            GraphEdge.project_id == project_id,
            GraphEdge.status == GraphEdgeStatus.ACTIVE,
        )
        if environment_id is not None:
            stmt = stmt.where(
                (GraphEdge.environment_id == environment_id)
                | (GraphEdge.environment_id.is_(None))
            )
        rows = (await self._session.execute(stmt)).scalars().all()
        if not rows:
            return []
        node_ids = {r.source_node_id for r in rows} | {r.target_node_id for r in rows}
        node_map = await self._load_node_entity_map(project_id, node_ids)

        edges: list[DependencyEdgeInfo] = []
        for row in rows:
            source_component = node_map.get(row.source_node_id)
            target_component = node_map.get(row.target_node_id)
            if source_component is None or target_component is None:
                continue
            if not ({source_component, target_component} & component_ids):
                continue
            edges.append(
                DependencyEdgeInfo(
                    source_component_id=source_component,
                    target_component_id=target_component,
                    dependency_type=getattr(
                        getattr(row, "edge_type", None), "value", None
                    )
                    or (str(row.edge_type) if row.edge_type else None),
                    origin="graph_edges",
                )
            )
        return edges

    async def _load_node_entity_map(
        self, project_id: uuid.UUID, node_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        from app.models.graph import GraphNode

        if not node_ids:
            return {}
        stmt = select(GraphNode.id, GraphNode.entity_id).where(
            GraphNode.project_id == project_id,
            GraphNode.id.in_(node_ids),
            GraphNode.entity_kind == "system_component",
            GraphNode.entity_id.is_not(None),
        )
        return {
            node_id: entity_id
            for node_id, entity_id in (await self._session.execute(stmt)).all()
        }

    # -- Expansion ----------------------------------------------------------
    def _expand_bounded(self, context: DependencyContext) -> None:
        """Directed adjacency within the hop budget, from the loaded edges."""
        for edge in context.edges:
            s, t = edge.as_pair()
            context.upstream_providers.setdefault(s, set()).add(t)
            context.downstream_dependents.setdefault(t, set()).add(s)
            context.component_ids.update((s, t))

    def _find_shared_dependencies(self, context: DependencyContext) -> None:
        for provider, dependents in context.downstream_dependents.items():
            if len(dependents) > 1:
                context.shared_dependencies[provider] = set(dependents)


__all__ = [
    "DependencyAnalyzer",
    "DependencyContext",
    "DependencyEdgeInfo",
    "StructuralRelation",
]
