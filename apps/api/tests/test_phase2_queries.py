"""Phase 2 — graph queries: dependencies, dependents, neighbors, paths, impact."""

from __future__ import annotations

import uuid
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.graph_impact import DependencyImpactAnalyzer
from app.services.graph_query_service import DependenciesResult, PathResult
from app.services.graph_query_service import GraphQueryService
from app.services.graph_registry import ComponentRegistry


async def _seed(
    db_session: AsyncSession,
    names: tuple[str, ...] = (
        "web",
        "gateway",
        "checkout",
        "inventory",
        "postgres",
    ),
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Project + env + mirrored components; returns ids and {name: node_id}."""
    project = SoftwareProject(name="Query Proj", slug="query-proj")
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


def _edge(
    project_id: uuid.UUID,
    source: uuid.UUID,
    target: uuid.UUID,
) -> GraphEdge:
    return GraphEdge(
        project_id=project_id,
        source_node_id=source,
        target_node_id=target,
        edge_type=GraphEdgeType.CALLS,
        source=GraphEdgeSource.TRACE,
        confidence=0.9,
        status=GraphEdgeStatus.ACTIVE,
    )


async def _chain(
    db_session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_ids: dict[str, uuid.UUID],
    order: tuple[str, ...],
) -> None:
    """Insert CALLS edges following ``order`` pairs (a->b, b->c, ...)."""
    for i in range(len(order) - 1):
        db_session.add(_edge(project_id, node_ids[order[i]], node_ids[order[i + 1]]))
    await db_session.flush()


def _names(nodes) -> List[str]:
    return [n.name for n in nodes]


class TestDependencies:
    async def test_direct_dependencies_dependents(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, node_ids = await _seed(
            db_session, names=("web", "gateway", "checkout", "inventory")
        )
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory"),
        )
        q = GraphQueryService(db_session)

        deps = await q.get_dependencies(node_id=node_ids["web"])
        assert _names(deps.direct) == ["gateway"]
        assert deps.transitive == []

        dependents = await q.get_dependents(node_id=node_ids["checkout"])
        assert _names(dependents.direct) == ["gateway"]
        assert dependents.transitive == []

        neighbors = await q.get_neighbors(node_id=node_ids["gateway"])
        assert set(_names(neighbors.direct)) == {"web", "checkout"}

    async def test_dependents_upstream_chain(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        q = GraphQueryService(db_session)

        deps = await q.get_dependents(node_id=node_ids["postgres"], transitive=True)
        assert set(_names(deps.direct)) == {"inventory"}
        assert set(_names(deps.transitive)) == {"web", "gateway", "checkout"}

    async def test_transitive_closure(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        q = GraphQueryService(db_session)

        deps = await q.get_dependencies(node_id=node_ids["web"], transitive=True)
        assert _names(deps.direct) == ["gateway"]
        assert set(_names(deps.transitive)) == {
            "checkout",
            "inventory",
            "postgres",
        }


class TestPaths:
    async def test_path_found(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        q = GraphQueryService(db_session)

        result: PathResult = await q.find_path(
            source_id=node_ids["web"], target_id=node_ids["postgres"]
        )
        assert result.found is True
        assert result.hops == 4
        assert _names(result.nodes) == [
            "web",
            "gateway",
            "checkout",
            "inventory",
            "postgres",
        ]
        assert len(result.edges) == 4

    async def test_path_bidirectional(self, db_session: AsyncSession) -> None:
        """A path may use edges in either direction (structural reachability)."""
        project_id, _, node_ids = await _seed(db_session)
        # Single edge checkout -> inventory; ask for the reverse pair and
        # expect the same edge to connect them.
        db_session.add(_edge(project_id, node_ids["checkout"], node_ids["inventory"]))
        await db_session.flush()
        q = GraphQueryService(db_session)

        result = await q.find_path(
            source_id=node_ids["inventory"], target_id=node_ids["checkout"]
        )
        assert result.found is True
        assert result.hops == 1
        assert result.edges[0].target_node_id == node_ids["inventory"]

    async def test_path_not_found(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(
            db_session, names=("web", "gateway", "db", "lone")
        )
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway"),
        )
        q = GraphQueryService(db_session)

        result = await q.find_path(source_id=node_ids["db"], target_id=node_ids["lone"])
        assert result.found is False
        assert result.nodes == []
        assert result.edges == []

    async def test_path_same_node(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway"),
        )
        q = GraphQueryService(db_session)

        result = await q.find_path(source_id=node_ids["web"], target_id=node_ids["web"])
        assert result.found is True
        assert result.hops == 0
        assert _names(result.nodes) == ["web"]


class TestGuards:
    async def test_cycle_no_infinite_loop(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        db_session.add(_edge(project_id, node_ids["web"], node_ids["gateway"]))
        db_session.add(_edge(project_id, node_ids["gateway"], node_ids["checkout"]))
        db_session.add(
            _edge(project_id, node_ids["checkout"], node_ids["web"])
        )  # cycle
        await db_session.flush()
        q = GraphQueryService(db_session)

        # Dependency closure from the cycle terminates.
        deps = await q.get_dependencies(node_id=node_ids["web"], transitive=True)
        assert set(_names(deps.direct)) | set(_names(deps.transitive)) == {
            "gateway",
            "checkout",
        }
        # Self-path and cross-pair path both terminate.
        assert (
            await q.find_path(source_id=node_ids["web"], target_id=node_ids["web"])
        ).found is True
        assert (
            await q.find_path(source_id=node_ids["gateway"], target_id=node_ids["web"])
        ).found is True

    async def test_depth_cap(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        q = GraphQueryService(db_session)

        deps = await q.get_dependencies(
            node_id=node_ids["web"], transitive=True, max_depth=2
        )
        assert set(_names(deps.direct)) | set(_names(deps.transitive)) == {
            "gateway",
            "checkout",
        }

        path = await q.find_path(
            source_id=node_ids["web"],
            target_id=node_ids["postgres"],
            max_depth=2,
        )
        assert path.found is False

    async def test_node_cap_truncates(self, db_session: AsyncSession) -> None:
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        q = GraphQueryService(db_session)

        deps = await q.get_dependencies(
            node_id=node_ids["web"], transitive=True, max_nodes=2
        )
        assert deps.truncated is True
        assert len([*deps.direct, *deps.transitive]) < 4

    async def test_missing_node_returns_empty(self, db_session: AsyncSession) -> None:
        q = GraphQueryService(db_session)
        result: DependenciesResult = await q.get_dependencies(
            node_id=uuid.uuid4(), transitive=True
        )
        assert result.direct == []
        assert result.transitive == []
        assert result.truncated is False


class TestImpact:
    async def test_downstream_impact_chain(self, db_session: AsyncSession) -> None:
        """Impact of a source = the transitive chain that DEPENDS on it.

        Chain: web → gateway → checkout → inventory → postgres. Selecting
        postgres yields postgres → inventory → checkout → gateway → web
        (master prompt §32/§75 demo direction).
        """
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        analyzer = DependencyImpactAnalyzer(db_session, GraphQueryService(db_session))

        impact = await analyzer.analyze_downstream(node_id=node_ids["postgres"])
        assert impact.label == "Dependency Impact"
        assert impact.relation == "downstream"
        assert impact.count == 4
        by_name = {item.node.name: item for item in impact.items}
        assert set(by_name) == {"gateway", "checkout", "inventory", "web"}
        assert by_name["inventory"].hops == 1
        assert by_name["web"].hops == 4
        assert by_name["web"].path == [
            node_ids["postgres"],
            node_ids["inventory"],
            node_ids["checkout"],
            node_ids["gateway"],
            node_ids["web"],
        ]

    async def test_impact_source_with_no_dependents(
        self, db_session: AsyncSession
    ) -> None:
        """The top of the chain (web) has nothing depending on it."""
        project_id, _, node_ids = await _seed(db_session)
        await _chain(
            db_session,
            project_id=project_id,
            node_ids=node_ids,
            order=("web", "gateway", "checkout", "inventory", "postgres"),
        )
        analyzer = DependencyImpactAnalyzer(db_session, GraphQueryService(db_session))

        impact = await analyzer.analyze_downstream(node_id=node_ids["web"])
        assert impact.count == 0
        assert impact.items == []

    async def test_impact_missing_node_empty(self, db_session: AsyncSession) -> None:
        analyzer = DependencyImpactAnalyzer(db_session, GraphQueryService(db_session))
        impact = await analyzer.analyze_downstream(node_id=uuid.uuid4())
        assert impact.count == 0
        assert impact.items == []
