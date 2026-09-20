"""ARGUS Dependency Impact Analyzer.

Downstream-impact analysis over the knowledge graph. Given a source component,
computes the set of nodes whose dependency chain reaches it — i.e. everything
transitively *depending on* the source (Redis → Checkout → API Gateway → Web).
This is the dependency-impact direction: if the source becomes unavailable,
these are the components whose wiring is affected. It is a structural
analysis — it does NOT claim or predict failure propagation (§32).

Traversal therefore follows *incoming* edges (dependents closure via
``GraphQueryService.get_dependents``), and path reconstruction reuses the
closure's spanning tree: the query service records exactly one discovery edge
per node, so working back from each node to the source yields a shortest path
(BFS discovery order).
"""

from __future__ import annotations

import uuid
from typing import Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.graph import GraphNodeResponse, ImpactItem, ImpactResponse
from app.services.graph_query_service import GraphQueryService


class DependencyImpactAnalyzer:
    """Compute the downstream impact closure for a graph node."""

    def __init__(self, db: AsyncSession, query: GraphQueryService) -> None:
        self._db = db
        self._query = query

    async def analyze_downstream(
        self,
        *,
        node_id: uuid.UUID,
        max_depth: int = 10,
        max_nodes: int = 2000,
    ) -> ImpactResponse:
        if await self._query.get_node(node_id) is None:
            return ImpactResponse(
                source_id=node_id,
                label="Dependency Impact",
                count=0,
                items=[],
                relation="downstream",
            )

        closure = await self._query.get_dependents(
            node_id=node_id,
            transitive=True,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        nodes = [*closure.direct, *closure.transitive]

        # In the dependents closure each edge's *target* is the discovery
        # parent of its source; a node is discovered once, so this map has
        # exactly one parent per child.
        parent: Dict[uuid.UUID, uuid.UUID] = {}
        for edge in closure.all_edges:
            parent.setdefault(edge.source_node_id, edge.target_node_id)

        def _path_ids(child: uuid.UUID) -> List[uuid.UUID]:
            ids: List[uuid.UUID] = []
            current = child
            while current != node_id:
                previous = parent.get(current)
                if previous is None:  # unreachable guard (should not happen)
                    return []
                ids.append(current)
                current = previous
            ids.reverse()
            return ids

        items: List[ImpactItem] = [
            ImpactItem(
                node=GraphNodeResponse.model_validate(node),
                path=[node_id, *_path_ids(node.id)],
                hops=len(_path_ids(node.id)),
            )
            for node in nodes
        ]
        # Deterministic ordering: closest impact first, then by name.
        items.sort(key=lambda item: (item.hops, item.node.name))
        return ImpactResponse(
            source_id=node_id,
            label="Dependency Impact",
            count=len(items),
            items=items,
            relation="downstream",
        )
