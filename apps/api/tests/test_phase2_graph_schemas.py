"""Phase 2 — graph schema invariants: validation, enum values, round-trips."""

from __future__ import annotations

import uuid

import pytest

from app.models.graph import (
    DiscoveredComponentStatus,
    GraphCriticality,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNodeStatus,
    GraphNodeType,
    ReconciliationStatus,
)
from app.schemas.graph import (
    AliasCreate,
    AliasResponse,
    DiscoveryRegister,
    DiscoveryResponse,
    EndpointCreate,
    EnvComparisonResponse,
    EnvDiffItem,
    EnvSummary,
    GraphData,
    GraphDependenciesResponse,
    GraphEdgeBase,
    GraphEdgeResponse,
    GraphHealthResponse,
    GraphNodeCreate,
    GraphNodeList,
    GraphNodeResponse,
    GraphNodeUpdate,
    ImpactResponse,
    OwnerCreate,
    OwnerResponse,
    PathResponse,
    ReconcileResponse,
    SearchResponse,
    SnapshotDiffResponse,
    SnapshotDetailResponse,
    SnapshotList,
    SnapshotResponse,
)


NOW = "2026-09-17T12:00:00.000Z"


def _u() -> uuid.UUID:
    return uuid.uuid4()


def _node_payload(**overrides) -> dict:
    payload = {
        "name": "Checkout",
        "node_type": GraphNodeType.COMPONENT.value,
        "project_id": _u(),
        "entity_kind": "system_component",
        "entity_id": _u(),
        "environment_id": None,
        "external_identifier": None,
        "description": None,
        "status": GraphNodeStatus.ACTIVE.value,
        "criticality": GraphCriticality.HIGH.value,
        "language": None,
        "framework": None,
        "runtime": None,
        "version": None,
        "repository_url": None,
        "documentation_url": None,
        "ownership_team": None,
        "metadata_": None,
        "id": _u(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return payload


def _edge_payload(**overrides) -> dict:
    payload = {
        "id": _u(),
        "project_id": _u(),
        "source_node_id": _u(),
        "target_node_id": _u(),
        "edge_type": GraphEdgeType.CALLS.value,
        "dependency_type": None,
        "confidence": 0.9,
        "source": GraphEdgeSource.TRACE.value,
        "status": GraphEdgeStatus.ACTIVE.value,
        "environment_id": None,
        "metadata_": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return payload


class TestGraphNodeSchemas:
    def test_create_defaults(self) -> None:
        s = GraphNodeCreate(name="X", node_type="WORKER")
        assert s.status == "ACTIVE"
        assert s.criticality == "UNKNOWN"
        assert s.entity_kind is None

    def test_create_enum_values_serialize_to_strings(self) -> None:
        s = GraphNodeCreate(name="X", node_type=GraphNodeType.SERVICE)
        assert s.node_type == "SERVICE"

    def test_create_extra_forbidden(self) -> None:
        with pytest.raises(ValueError):
            GraphNodeCreate(name="X", node_type="SERVICE", bogus=1)

    def test_update_all_optional(self) -> None:
        # Untouched fields are excluded for partial update semantics.
        assert GraphNodeUpdate().model_dump(exclude_unset=True) == {}

    def test_response_round_trip_from_dict(self) -> None:
        s = GraphNodeResponse.model_validate({**_node_payload()})
        assert s.entity_kind == "system_component"
        assert s.name == "Checkout"

    def test_response_metadata_serializes_as_metadata(self) -> None:
        # Response input uses the python field name (ORM attribute); output
        # must present the `metadata` key to API clients (observability
        # convention for response models).
        s = GraphNodeResponse.model_validate(
            {**_node_payload(), "metadata_": {"team": "platform"}}
        )
        assert s.metadata_ == {"team": "platform"}
        dumped = s.model_dump(by_alias=True)
        assert dumped["metadata"] == {"team": "platform"}
        assert "metadata_" not in dumped

    def test_response_rejects_unknown(self) -> None:
        with pytest.raises(ValueError):
            GraphNodeResponse.model_validate({**_node_payload(), "nope": 1})

    def test_metadata_alias_request(self) -> None:
        s = GraphNodeCreate(name="X", node_type="SERVICE", metadata={"k": 1})
        assert s.metadata_ == {"k": 1}


class TestGraphEdgeSchemas:
    def test_edge_base_defaults(self) -> None:
        s = GraphEdgeBase(
            source_node_id=_u(),
            target_node_id=_u(),
            edge_type=GraphEdgeType.DEPENDS_ON,
        )
        assert s.source == "CONFIGURATION"
        assert s.status == "ACTIVE"
        assert s.confidence == 1.0

    def test_edge_confidence_bounds(self) -> None:
        with pytest.raises(ValueError):
            GraphEdgeBase(
                source_node_id=_u(),
                target_node_id=_u(),
                edge_type="CALLS",
                confidence=1.5,
            )
        GraphEdgeBase(
            source_node_id=_u(), target_node_id=_u(), edge_type="CALLS", confidence=0.0
        )

    def test_edge_response_round_trip(self) -> None:
        s = GraphEdgeResponse.model_validate({**_edge_payload()})
        assert s.edge_type == "CALLS"
        assert s.source == "TRACE"


class TestPagingContainer:
    def test_node_list_pagination(self) -> None:
        node = GraphNodeResponse.model_validate({**_node_payload()})
        s = GraphNodeList(
            items=[node],
            total=1,
            page=1,
            page_size=20,
            total_pages=1,
        )
        assert s.items[0].name == "Checkout"

    def test_snapshot_list_generic(self) -> None:
        snap = SnapshotResponse(
            id=_u(),
            project_id=_u(),
            environment_id=None,
            snapshot_version=1,
            node_count=3,
            edge_count=2,
            source="MANUAL",
            caption=None,
            previous_snapshot_id=None,
            created_at=NOW,
        )
        s = SnapshotList(items=[snap], total=1, page=1, page_size=10, total_pages=1)
        assert s.total_pages == 1


class TestCompositeSchemas:
    def test_graph_data(self) -> None:
        s = GraphData(
            nodes=[GraphNodeResponse.model_validate({**_node_payload()})],
            edges=[GraphEdgeResponse.model_validate({**_edge_payload()})],
        )
        assert len(s.nodes) == 1 and len(s.edges) == 1

    def test_dependencies_response(self) -> None:
        n = GraphNodeResponse.model_validate({**_node_payload()})
        s = GraphDependenciesResponse(
            component_id=_u(),
            direction="outgoing",
            direct=[n],
            transitive=[],
            direct_count=1,
            transitive_count=0,
        )
        assert s.direction == "outgoing"

    def test_impact_response(self) -> None:
        n = GraphNodeResponse.model_validate({**_node_payload()})
        s = ImpactResponse(
            source_id=_u(),
            count=1,
            items=[{"node": n, "path": [n.id], "hops": 1}],
        )
        assert s.relation == "downstream"
        assert s.label == "Dependency Impact"

    def test_path_response(self) -> None:
        n = GraphNodeResponse.model_validate({**_node_payload()})
        s = PathResponse(found=True, path=[n], edges=[], total_hops=1)
        assert s.found is True

    def test_env_diff_item_required_kinds(self) -> None:
        with pytest.raises(ValueError):
            EnvDiffItem(
                kind="nope",
                category={"name": "X", "combined_key": "k"},
                key="k",
                name="X",
                in_a=True,
                in_b=False,
            )

    def test_env_comparison(self) -> None:
        s = EnvComparisonResponse(
            project_id=_u(),
            environment_a=EnvSummary(
                environment_id=None,
                name="prod",
                node_count=3,
                edge_count=2,
                component_count=2,
            ),
            environment_b=EnvSummary(
                environment_id=None,
                name="staging",
                node_count=1,
                edge_count=0,
                component_count=1,
            ),
            added=[],
            removed=[],
            changed=[],
        )
        assert s.environment_a.name == "prod"
        assert s.labels["added"]

    def test_snapshot_diff(self) -> None:
        s = SnapshotDiffResponse(
            a_id=_u(),
            b_id=_u(),
            added_nodes=[],
            removed_nodes=[],
            added_edges=[],
            removed_edges=[],
            added_node_names=[],
            removed_node_names=[],
        )
        assert s.a_id

    def test_snapshot_detail(self) -> None:
        s = SnapshotDetailResponse(
            id=_u(),
            project_id=_u(),
            snapshot_version=1,
            node_count=1,
            edge_count=0,
            source="MANUAL",
            nodes=[],
            edges=[],
            signature=[],
            created_at=NOW,
        )
        assert s.snapshot_version == 1


class TestEndpointsOwnersAliasesDiscovery:
    def test_endpoint_create_method_validation(self) -> None:
        with pytest.raises(ValueError):
            EndpointCreate(method="FROBNICATE", path="/x")
        EndpointCreate(method="GET", path="/x")

    def test_owner_create_email(self) -> None:
        # contact_email is a plain (bare-essential) string with a length cap —
        # no format validation, no extra email-validation dependency.
        with pytest.raises(ValueError):
            OwnerCreate(team="Platform", contact_email="x" * 300)
        s = OwnerCreate(team="Platform", contact_email="a@b.co")
        assert s.team == "Platform"

    def test_owner_response(self) -> None:
        s = OwnerResponse(
            id=_u(),
            project_id=_u(),
            component_id=_u(),
            team="Platform",
            created_at=NOW,
            updated_at=NOW,
        )
        assert s.repository_owner is None

    def test_alias_create(self) -> None:
        s = AliasCreate(alias="checkout-service")
        assert s.source == "INFERENCE"

    def test_alias_response(self) -> None:
        s = AliasResponse(
            id=_u(),
            project_id=_u(),
            node_id=_u(),
            alias="ok",
            source="CONFIGURATION",
            created_at=NOW,
            updated_at=NOW,
        )
        assert s.alias == "ok"

    def test_discovery_register_defaults(self) -> None:
        assert DiscoveryRegister().name is None

    def test_discovery_response(self) -> None:
        s = DiscoveryResponse(
            id=_u(),
            project_id=_u(),
            discovered_name="ml-scorer",
            suggested_node_type="WORKER",
            evidence_count=1,
            status=DiscoveredComponentStatus.PENDING.value,
            created_at=NOW,
            updated_at=NOW,
        )
        assert s.status == "PENDING"


class TestOperationalSchemas:
    def test_reconcile_response(self) -> None:
        s = ReconcileResponse(
            reconciliation_run_id=_u(),
            project_id=_u(),
            input_source="CONFIGURATION",
            nodes_created=2,
            edges_created=1,
            edges_updated=0,
            edges_marked_stale=0,
            status=ReconciliationStatus.SUCCESS.value,
            started_at=NOW,
        )
        assert s.errors is None

    def test_graph_health(self) -> None:
        s = GraphHealthResponse(
            project_id=_u(),
            node_count=3,
            edge_count=2,
            data_quality=[],
            ok=True,
        )
        assert s.ok is True

    def test_search_response(self) -> None:
        s = SearchResponse(
            query="checkout",
            results=[],
        )
        assert s.query == "checkout"
