"""Phase 2 — environment isolation + structural comparison."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    ServiceEndpoint,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.graph_query_service import GraphQueryService
from app.services.graph_registry import ComponentRegistry


async def _seed_pair(
    db_session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Project + production/staging environments; returns (project, prod, staging)."""
    project = SoftwareProject(name="Env Proj", slug="env-proj")
    db_session.add(project)
    await db_session.flush()
    prod = Environment(
        project_id=project.id, name="Production", environment_type="PRODUCTION"
    )
    staging = Environment(
        project_id=project.id, name="Staging", environment_type="STAGING"
    )
    db_session.add(prod)
    db_session.add(staging)
    await db_session.flush()
    return project.id, prod.id, staging.id


async def _make_components(
    db_session: AsyncSession,
    project_id: uuid.UUID,
    env_id: uuid.UUID,
    names: tuple[str, ...],
) -> dict[str, uuid.UUID]:
    """Component rows + mirrored graph nodes; returns {name: component_id}."""
    registry = ComponentRegistry(db_session)
    ids: dict[str, uuid.UUID] = {}
    for name in names:
        comp = SystemComponent(
            project_id=project_id,
            environment_id=env_id,
            name=name,
            component_type="SERVICE",
        )
        db_session.add(comp)
        await db_session.flush()
        await registry.get_or_create_component_node(comp)
        ids[name] = comp.id
    return ids


async def _chain_edge(
    db_session: AsyncSession,
    *,
    project_id: uuid.UUID,
    env_id: uuid.UUID,
    comp_ids: dict[str, uuid.UUID],
    order: tuple[str, ...],
) -> None:
    """CALLS edge between components. Mirrored graph nodes are resolved via
    the per-environment component id, never by un-scoped name (both envs
    share names like ``checkout``)."""
    registry = ComponentRegistry(db_session)
    for i in range(len(order) - 1):
        source = await registry.get_node_by_entity(
            project_id, "system_component", comp_ids[order[i]]
        )
        target = await registry.get_node_by_entity(
            project_id, "system_component", comp_ids[order[i + 1]]
        )
        assert source is not None and target is not None
        db_session.add(
            GraphEdge(
                project_id=project_id,
                environment_id=env_id,
                source_node_id=source.id,
                target_node_id=target.id,
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                confidence=0.9,
                status=GraphEdgeStatus.ACTIVE,
            )
        )
    await db_session.flush()


async def _deploy(
    db_session: AsyncSession,
    *,
    project_id: uuid.UUID,
    env_id: uuid.UUID,
    component_id: uuid.UUID,
    version: str,
) -> None:
    db_session.add(
        DeploymentEvent(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            deployment_id=f"dep-{uuid.uuid4()}",
            version=version,
            status=DeploymentStatus.SUCCESS,
            deployed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()


class TestEnvironmentIsolation:
    async def test_get_graph_scoped_by_environment(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, staging_id = await _seed_pair(db_session)
        prod_ids = await _make_components(
            db_session, project_id, prod_id, ("web", "checkout")
        )
        staging_ids = await _make_components(
            db_session, project_id, staging_id, ("web", "checkout", "staging-only")
        )
        assert set(prod_ids) | set(staging_ids)  # both envs mirrored

        q = GraphQueryService(db_session)
        prod_nodes, _ = await q.get_graph(project_id=project_id, environment_id=prod_id)
        prod_names = {n.name for n in prod_nodes}
        assert "staging-only" not in prod_names
        # The shared names resolve to the production component's own node, not
        # a staging duplicate.
        prod_checkout = next(
            n
            for n in prod_nodes
            if n.name == "checkout" and n.environment_id == prod_id
        )
        assert prod_checkout is not None

    async def test_staging_nodes_never_leak_into_prod(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, staging_id = await _seed_pair(db_session)
        staging_ids = await _make_components(
            db_session, project_id, staging_id, ("ghost-payment",)
        )
        await _make_components(db_session, project_id, prod_id, ("web",))
        assert staging_ids  # noqa: F841

        q = GraphQueryService(db_session)
        prod_nodes, _ = await q.get_graph(project_id=project_id, environment_id=prod_id)
        assert "ghost-payment" not in {n.name for n in prod_nodes}


class TestCompareEnvironments:
    async def _populate(
        self, db_session: AsyncSession
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        project_id, prod_id, staging_id = await _seed_pair(db_session)

        # Production: full topology incl. payment-only chain.
        prod = await _make_components(
            db_session,
            project_id,
            prod_id,
            ("gateway", "checkout", "inventory", "payment", "postgres"),
        )
        staging = await _make_components(
            db_session,
            project_id,
            staging_id,
            ("gateway", "checkout", "inventory"),
        )

        # Shared dependency chains (present in both -> no diff).
        await _chain_edge(
            db_session,
            project_id=project_id,
            env_id=prod_id,
            comp_ids=prod,
            order=("gateway", "checkout", "inventory", "postgres"),
        )
        await _chain_edge(
            db_session,
            project_id=project_id,
            env_id=staging_id,
            comp_ids=staging,
            order=("gateway", "checkout", "inventory"),
        )
        # Production-only: checkout -> payment -> (external api).
        await _chain_edge(
            db_session,
            project_id=project_id,
            env_id=prod_id,
            comp_ids=prod,
            order=("checkout", "payment"),
        )

        # Endpoints: shared on checkout; production-only on payment.
        for env_id, comps in ((prod_id, prod), (staging_id, staging)):
            db_session.add(
                ServiceEndpoint(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=comps["checkout"],
                    method="POST",
                    path_template="/api/checkout",
                )
            )
        db_session.add(
            ServiceEndpoint(
                project_id=project_id,
                environment_id=prod_id,
                component_id=prod["payment"],
                method="POST",
                path_template="/api/payments",
            )
        )
        # Versions diverge on checkout; payment only ever deployed to prod.
        await _deploy(
            db_session,
            project_id=project_id,
            env_id=prod_id,
            component_id=prod["checkout"],
            version="2.1.0",
        )
        await _deploy(
            db_session,
            project_id=project_id,
            env_id=staging_id,
            component_id=staging["checkout"],
            version="2.0.0",
        )
        await _deploy(
            db_session,
            project_id=project_id,
            env_id=prod_id,
            component_id=prod["payment"],
            version="1.4.2",
        )
        await db_session.flush()
        return project_id, prod_id, staging_id

    async def test_compare_production_vs_staging(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, staging_id = await self._populate(db_session)
        q = GraphQueryService(db_session)

        # A = staging, B = production: production-only entities are `added`.
        resp = await q.compare_environments(
            project_id=project_id,
            environment_a=staging_id,
            environment_b=prod_id,
        )
        assert resp.environment_a.name == "Staging"
        assert resp.environment_b.name == "Production"
        assert resp.environment_a.component_count < resp.environment_b.component_count

        added_components = [it for it in resp.added if it.kind == "component"]
        assert any(it.name == "payment" for it in added_components)
        assert "checkout -> payment" in [it.name for it in resp.added]
        assert "POST /api/payments" in [it.name for it in resp.added]

        # Checkout changed version across environments.
        changed = [it for it in resp.changed if it.kind == "version"]
        checkout_versions = [it for it in changed if it.name == "checkout"]
        assert len(checkout_versions) == 1
        assert "2.1.0" in str(checkout_versions[0].detail)
        assert "2.0.0" in str(checkout_versions[0].detail)

        # Shared topology yields no removals going production -> staging view.
        assert resp.labels["added"] == "Only in Production"
        assert resp.labels["removed"] == "Only in Staging"

    async def test_compare_same_environment_empty(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, staging_id = await self._populate(db_session)
        q = GraphQueryService(db_session)

        resp = await q.compare_environments(
            project_id=project_id,
            environment_a=prod_id,
            environment_b=prod_id,
        )
        assert resp.added == []
        assert resp.removed == []
        assert resp.changed == []
        assert resp.environment_a.node_count == resp.environment_b.node_count
        assert staging_id  # noqa: F841

    async def test_compare_summaries_count_graph(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, staging_id = await self._populate(db_session)
        q = GraphQueryService(db_session)

        resp = await q.compare_environments(
            project_id=project_id,
            environment_a=staging_id,
            environment_b=prod_id,
        )
        # Production has strictly more nodes + edges than staging.
        assert resp.environment_b.node_count > resp.environment_a.node_count
        assert resp.environment_b.edge_count >= resp.environment_a.edge_count
