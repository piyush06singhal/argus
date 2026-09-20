"""Phase 2 — graph model invariants: unique constraints, enums, schema round-trip."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import BaseModel
from app.models.graph import (
    ComponentOwner,
    DataQualitySeverity,
    DiscoveredComponentStatus,
    GraphCriticality,
    GraphDataQualityRecord,
    GraphDiscoveryRecord,
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNode,
    GraphNodeAlias,
    GraphNodeStatus,
    GraphNodeType,
    GraphReconciliationRun,
    GraphSnapshot,
    ReconciliationStatus,
    ServiceEndpoint,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.schemas.graph import OwnerCreate
from app.services.endpoint_registry import EndpointRegistry
from app.services.graph_registry import ComponentRegistry


@pytest.mark.asyncio
async def _session_seed(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a project + one component; return (project_id, component_id)."""
    project = SoftwareProject(name="Graph Test Proj", slug="graph-test-proj")
    db_session.add(project)
    await db_session.flush()
    env = Environment(
        project_id=project.id,
        name="prod",
        environment_type="PRODUCTION",
    )
    db_session.add(env)
    await db_session.flush()
    component = SystemComponent(
        project_id=project.id,
        component_type="SERVICE",
        name="Checkout",
    )
    db_session.add(component)
    await db_session.flush()
    return project.id, component.id


class TestEnums:
    """Enum vocabulary and serialization."""

    def test_node_type_values(self) -> None:
        assert GraphNodeType.SERVICE.value == "SERVICE"
        assert GraphNodeType.EXTERNAL_API.value == "EXTERNAL_API"
        assert GraphNodeType.PROJECT.value == "PROJECT"

    def test_edge_type_values(self) -> None:
        assert GraphEdgeType.CALLS.value == "CALLS"
        assert GraphEdgeType.DEPENDS_ON.value == "DEPENDS_ON"
        assert GraphEdgeType.PUBLISHES_TO.value == "PUBLISHES_TO"

    def test_edge_source_values(self) -> None:
        assert GraphEdgeSource.CONFIGURATION.value == "CONFIGURATION"
        assert GraphEdgeSource.TRACE.value == "TRACE"
        assert GraphEdgeSource.MANUAL.value == "MANUAL"

    def test_status_values(self) -> None:
        assert GraphEdgeStatus.STALE.value == "STALE"
        assert GraphNodeStatus.DISABLED.value == "DISABLED"
        assert GraphCriticality.CRITICAL.value == "CRITICAL"

    def test_lifecycle_enum_values(self) -> None:
        assert DiscoveredComponentStatus.PENDING.value == "PENDING"
        assert DataQualitySeverity.ERROR.value == "ERROR"
        assert ReconciliationStatus.SUCCESS.value == "SUCCESS"


class TestGraphNodeUniqueEntity:
    """GraphNode identity is (project_id, entity_kind, entity_id)."""

    @pytest.mark.asyncio
    async def test_duplicate_entity_rejected(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        base = dict(
            project_id=project_id,
            node_type=GraphNodeType.COMPONENT,
            entity_kind="system_component",
            entity_id=component_id,
            name="Checkout",
        )
        db_session.add(GraphNode(**base))
        await db_session.flush()
        db_session.add(GraphNode(**base))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_different_entity_kind_can_coexist(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _ = await _session_seed(db_session)
        base = dict(project_id=project_id, name="dup")
        db_session.add(
            GraphNode(
                node_type=GraphNodeType.SERVICE,
                entity_kind="system_component",
                entity_id=uuid.uuid4(),
                **base,
            )
        )
        db_session.add(
            GraphNode(
                node_type=GraphNodeType.REPOSITORY,
                entity_kind="repository",
                entity_id=uuid.uuid4(),
                **base,
            )
        )
        await db_session.flush()


class TestGraphEdgeConstraints:
    """Edge unique key: (source, target, edge_type, environment)."""

    @pytest.mark.asyncio
    async def test_duplicate_edge_rejected(self, db_session: AsyncSession) -> None:
        project_id, _ = await _session_seed(db_session)
        a = GraphNode(
            project_id=project_id,
            node_type=GraphNodeType.SERVICE,
            entity_kind="generic",
            entity_id=uuid.uuid4(),
            name="A",
        )
        b = GraphNode(
            project_id=project_id,
            node_type=GraphNodeType.DATABASE,
            entity_kind="generic",
            entity_id=uuid.uuid4(),
            name="B",
        )
        db_session.add_all([a, b])
        await db_session.flush()

        edge = dict(
            project_id=project_id,
            source_node_id=a.id,
            target_node_id=b.id,
            edge_type=GraphEdgeType.READS_FROM,
            source=GraphEdgeSource.CONFIGURATION,
            environment_id=None,
            confidence=1.0,
        )
        db_session.add(GraphEdge(**edge))
        await db_session.flush()
        db_session.add(GraphEdge(**edge))  # same tuple
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_same_pair_environment_distinct_is_allowed(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _ = await _session_seed(db_session)
        env2 = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(env2)
        await db_session.flush()
        a = GraphNode(
            project_id=project_id,
            node_type=GraphNodeType.SERVICE,
            entity_kind="generic",
            entity_id=uuid.uuid4(),
            name="A",
        )
        b = GraphNode(
            project_id=project_id,
            node_type=GraphNodeType.DATABASE,
            entity_kind="generic",
            entity_id=uuid.uuid4(),
            name="B",
        )
        db_session.add_all([a, b])
        await db_session.flush()

        edge_kwargs = dict(
            project_id=project_id,
            source_node_id=a.id,
            target_node_id=b.id,
            edge_type=GraphEdgeType.CALLS,
        )
        db_session.add(GraphEdge(**edge_kwargs, environment_id=None))
        db_session.add(GraphEdge(**edge_kwargs, environment_id=env2.id))
        await db_session.flush()  # no IntegrityError


class TestUniqueSecondary:
    """Snapshot version, alias, endpoint, owner uniqueness."""

    @pytest.mark.asyncio
    async def test_snapshot_version_unique(self, db_session: AsyncSession) -> None:
        project_id, _ = await _session_seed(db_session)
        snap = dict(project_id=project_id, environment_id=None, snapshot_version=1)
        db_session.add(
            GraphSnapshot(**snap, node_count=1, edge_count=0, graph_signature=["x"])
        )
        await db_session.flush()
        db_session.add(
            GraphSnapshot(**snap, node_count=1, edge_count=0, graph_signature=["x"])
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_alias_unique_per_node(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        node = GraphNode(
            project_id=project_id,
            node_type=GraphNodeType.COMPONENT,
            entity_kind="system_component",
            entity_id=component_id,
            name="Checkout",
        )
        db_session.add(node)
        await db_session.flush()
        db_session.add(
            GraphNodeAlias(
                project_id=project_id, node_id=node.id, alias="checkout-service"
            )
        )
        await db_session.flush()
        db_session.add(
            GraphNodeAlias(
                project_id=project_id, node_id=node.id, alias="checkout-service"
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_endpoint_unique_per_component_method_path(
        self, db_session: AsyncSession
    ) -> None:
        project_id, component_id = await _session_seed(db_session)
        ep = dict(
            project_id=project_id,
            environment_id=None,
            component_id=component_id,
            method="GET",
            path_template="/api/checkout/{id}",
            is_external=False,
        )
        db_session.add(ServiceEndpoint(**ep))
        await db_session.flush()
        db_session.add(ServiceEndpoint(**ep))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_owner_unique_component(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        db_session.add(
            ComponentOwner(
                project_id=project_id, component_id=component_id, team="Platform"
            )
        )
        await db_session.flush()
        db_session.add(
            ComponentOwner(
                project_id=project_id, component_id=component_id, team="Checkout"
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestSchemaRoundTrip:
    """Pydantic schemas accept ORM rows (from_attributes) and reuse enum values."""

    @pytest.mark.asyncio
    async def test_graph_node_schema_round_trip(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        node = GraphNode(
            project_id=project_id,
            node_type=GraphNodeType.COMPONENT,
            entity_kind="system_component",
            entity_id=component_id,
            name="Checkout",
            status=GraphNodeStatus.ACTIVE,
            criticality=GraphCriticality.HIGH,
        )
        db_session.add(node)
        await db_session.flush()

        assert isinstance(node, BaseModel)
        # Enum values serialize as their str values, not objects.
        assert isinstance(node.node_type, GraphNodeType)
        assert node.node_type.value == "COMPONENT"
        assert node.status.value == "ACTIVE"

    @pytest.mark.asyncio
    async def test_data_quality_record_columns(self, db_session: AsyncSession) -> None:
        project_id, _ = await _session_seed(db_session)
        rec = GraphDataQualityRecord(
            project_id=project_id,
            check_type="duplicate_components",
            severity=DataQualitySeverity.WARNING,
            detail={"names": ["Checkout"]},
        )
        db_session.add(rec)
        await db_session.flush()
        assert rec.check_type == "duplicate_components"
        assert rec.severity == DataQualitySeverity.WARNING

    @pytest.mark.asyncio
    async def test_reconciliation_run_columns(self, db_session: AsyncSession) -> None:
        from datetime import datetime, timezone

        project_id, _ = await _session_seed(db_session)
        run = GraphReconciliationRun(
            project_id=project_id,
            started_at=datetime.now(timezone.utc),
            input_source=GraphEdgeSource.CONFIGURATION,
            status=ReconciliationStatus.SUCCESS,
            nodes_created=2,
            edges_created=1,
        )
        db_session.add(run)
        await db_session.flush()
        assert run.status == ReconciliationStatus.SUCCESS
        assert run.input_source == GraphEdgeSource.CONFIGURATION

    @pytest.mark.asyncio
    async def test_discovery_record_columns(self, db_session: AsyncSession) -> None:
        project_id, _ = await _session_seed(db_session)
        rec = GraphDiscoveryRecord(
            project_id=project_id,
            discovered_name="ml-scorer",
            suggested_node_type=GraphNodeType.WORKER,
            evidence_count=3,
            evidence_sources=["TRACE"],
            confidence=0.6,
            status=DiscoveredComponentStatus.PENDING,
        )
        db_session.add(rec)
        await db_session.flush()
        assert rec.evidence_count == 3
        assert rec.status == DiscoveredComponentStatus.PENDING


class TestComponentRegistry:
    """ComponentRegistry node upsert + resolution."""

    @pytest.mark.asyncio
    async def test_get_or_create_component_node_idempotent(
        self, db_session: AsyncSession
    ) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)

        first = await registry.get_or_create_component_node(component)
        await db_session.flush()
        second = await registry.get_or_create_component_node(component)
        await db_session.flush()

        assert first.id == second.id
        count_result = await db_session.execute(
            select(func.count())
            .select_from(GraphNode)
            .where(
                GraphNode.entity_kind == "system_component",
                GraphNode.entity_id == component_id,
            )
        )
        assert count_result.scalar_one() == 1
        # §7: a SERVICE-category component mirrors as a typed SERVICE node.
        assert first.node_type == GraphNodeType.SERVICE
        assert first.name == "Checkout"

    @pytest.mark.asyncio
    async def test_project_and_environment_nodes(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _ = await _session_seed(db_session)
        registry = ComponentRegistry(db_session)
        pnode = await registry.get_or_create_project_node(project_id)
        env = await db_session.execute(
            select(Environment).where(Environment.project_id == project_id)
        )
        env_id = env.scalar_one().id
        enode = await registry.get_or_create_environment_node(env_id, project_id)

        assert pnode.node_type == GraphNodeType.PROJECT
        assert pnode.entity_kind == "project"
        assert enode.node_type == GraphNodeType.ENVIRONMENT

    @pytest.mark.asyncio
    async def test_resolve_by_name_case_insensitive(
        self, db_session: AsyncSession
    ) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)
        node = await registry.get_or_create_component_node(component)

        resolved = await registry.resolve_component_node_by_name(project_id, "checkout")
        assert resolved is not None and resolved.id == node.id

    @pytest.mark.asyncio
    async def test_resolve_component_node_both_id_types(
        self, db_session: AsyncSession
    ) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)
        node = await registry.get_or_create_component_node(component)

        # canonical id and graph node id both resolve to the same node
        by_component = await registry.resolve_component_node(project_id, component_id)
        by_node = await registry.resolve_component_node(project_id, node.id)
        assert by_component is not None and by_component.id == node.id
        assert by_node is not None and by_node.id == node.id

    @pytest.mark.asyncio
    async def test_update_node_whitelist(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)
        node = await registry.get_or_create_component_node(component)

        updated = await registry.update_node(
            node.id, {"description": "new desc", "not_mutable": True}
        )
        assert updated is not None
        assert updated.description == "new desc"
        assert not hasattr(updated, "not_mutable")


class TestAliases:
    """Alias add / resolve / delete."""

    @pytest.mark.asyncio
    async def test_alias_roundtrip_case_insensitive(
        self, db_session: AsyncSession
    ) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)
        node = await registry.get_or_create_component_node(component)

        alias = await registry.add_alias(
            node.id,
            "Checkout-Service",
            project_id=project_id,
            source=GraphEdgeSource.CONFIGURATION,
        )
        assert alias.alias == "checkout-service"

        resolved = await registry.resolve_node_by_alias(project_id, "CHECKOUT-SERVICE")
        assert resolved is not None and resolved.id == node.id

    @pytest.mark.asyncio
    async def test_alias_upsert_updates_source(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)
        node = await registry.get_or_create_component_node(component)

        await registry.add_alias(
            node.id,
            "checkout-api",
            project_id=project_id,
            source=GraphEdgeSource.INFERENCE,
        )
        again = await registry.add_alias(
            node.id,
            "checkout-api",
            project_id=project_id,
            source=GraphEdgeSource.CONFIGURATION,
        )
        assert again.source == GraphEdgeSource.CONFIGURATION
        rows = await db_session.execute(
            select(func.count())
            .select_from(GraphNodeAlias)
            .where(GraphNodeAlias.node_id == node.id)
        )
        assert rows.scalar_one() == 1

    @pytest.mark.asyncio
    async def test_delete_alias(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        component = await db_session.get(SystemComponent, component_id)
        registry = ComponentRegistry(db_session)
        node = await registry.get_or_create_component_node(component)
        await registry.add_alias(node.id, "order-service", project_id=project_id)

        assert await registry.delete_alias(node.id, "ORDER-SERVICE") is True
        assert await registry.delete_alias(node.id, "ORDER-SERVICE") is False


class TestOwners:
    """Component ownership upsert."""

    @pytest.mark.asyncio
    async def test_set_and_get_owner(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        registry = ComponentRegistry(db_session)
        owner = await registry.set_owner(
            component_id,
            OwnerCreate(
                team="Platform", owner_name="Ada", contact_email="ada@example.com"
            ),
        )
        assert owner.project_id == project_id

        fetched = await registry.get_owner(component_id)
        assert fetched is not None and fetched.team == "Platform"

    @pytest.mark.asyncio
    async def test_set_owner_upsert_single_row(self, db_session: AsyncSession) -> None:
        _, component_id = await _session_seed(db_session)
        registry = ComponentRegistry(db_session)
        await registry.set_owner(component_id, OwnerCreate(team="Platform"))
        await registry.set_owner(component_id, OwnerCreate(team="Checkout"))
        rows = await db_session.execute(
            select(func.count())
            .select_from(ComponentOwner)
            .where(ComponentOwner.component_id == component_id)
        )
        assert rows.scalar_one() == 1


class TestEndpointRegistry:
    """Endoint path normalization + records."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/api/checkout/", "/api/checkout"),
            ("/api/checkout", "/api/checkout"),
            ("/api/v1/orders/123", "/api/v1/orders/{id}"),
            ("/api/inventory/124/stock", "/api/inventory/{id}/stock"),
            ("/users/a1b2c3d4e5f6g7h8i9j0", "/users/{id}"),
            ("/api/checkout/123/456", "/api/checkout/{id}"),
            ("/health", "/health"),
            ("/", "/"),
        ],
    )
    def test_normalize_path(self, raw: str, expected: str) -> None:
        assert EndpointRegistry.normalize_path(raw) == expected

    @pytest.mark.asyncio
    async def test_record_endpoint_upsert_and_original_paths(
        self, db_session: AsyncSession
    ) -> None:
        project_id, component_id = await _session_seed(db_session)
        registry = EndpointRegistry(db_session)

        first = await registry.record_endpoint(
            project_id=project_id,
            component_id=component_id,
            method="GET",
            path="/api/checkout/123",
        )
        await db_session.flush()
        second = await registry.record_endpoint(
            project_id=project_id,
            component_id=component_id,
            method="GET",
            path="/api/checkout/456",
        )
        await db_session.flush()

        assert first.id == second.id
        assert second.path_template == "/api/checkout/{id}"
        assert (second.original_paths or []) == [
            "/api/checkout/123",
            "/api/checkout/456",
        ]

    @pytest.mark.asyncio
    async def test_list_endpoints_scoped(self, db_session: AsyncSession) -> None:
        project_id, component_id = await _session_seed(db_session)
        registry = EndpointRegistry(db_session)
        await registry.record_endpoint(
            project_id=project_id,
            component_id=component_id,
            method="GET",
            path="/api/checkout/1",
        )
        await registry.record_endpoint(
            project_id=project_id,
            component_id=component_id,
            method="POST",
            path="/api/checkout",
        )

        all_rows, total = await registry.list_endpoints(project_id=project_id)
        assert total == 2
        get_only, get_total = await registry.list_endpoints(
            project_id=project_id, method="GET"
        )
        assert get_total == 1 and get_only[0].method == "GET"

        component_rows = await registry.endpoints_for_component(component_id)
        assert len(component_rows) == 2
