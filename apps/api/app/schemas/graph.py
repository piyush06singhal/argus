"""ARGUS Software Knowledge Graph Schemas.

Request/response schemas for the Phase 2 knowledge-graph API (§§ 40–62).
Follows the shared ``BaseSchema`` conventions (``from_attributes``,
``use_enum_values``, ``extra="forbid"``) so ORM rows validate directly into
response models and unknown request fields are rejected.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import Field

from app.models.graph import (
    DataQualitySeverity,
    DiscoveredComponentStatus,
    GraphCriticality,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNodeStatus,
    GraphNodeType,
    ReconciliationStatus,
)
from app.models.system import DependencyType
from app.schemas.base import BaseSchema, IDMixin, PaginatedResponse, TimestampMixin


class GraphNodeBase(BaseSchema):
    """Common node fields (create + response)."""

    name: str = Field(..., min_length=1, max_length=255)
    node_type: GraphNodeType
    environment_id: Optional[uuid.UUID] = None
    external_identifier: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    status: GraphNodeStatus = GraphNodeStatus.ACTIVE
    criticality: GraphCriticality = GraphCriticality.UNKNOWN
    language: Optional[str] = Field(None, max_length=64)
    framework: Optional[str] = Field(None, max_length=128)
    runtime: Optional[str] = Field(None, max_length=128)
    version: Optional[str] = Field(None, max_length=100)
    repository_url: Optional[str] = Field(None, max_length=512)
    documentation_url: Optional[str] = Field(None, max_length=512)
    ownership_team: Optional[str] = Field(None, max_length=255)
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class GraphNodeCreate(GraphNodeBase):
    """Create a node. ``entity_kind``/``entity_id`` optional — generic nodes
    (no canonical mirror) get an auto-generated ``entity_id``."""

    entity_kind: Optional[str] = Field(None, max_length=64)
    entity_id: Optional[uuid.UUID] = None


class GraphNodeUpdate(BaseSchema):
    """Update a node — every field optional (``exclude_unset`` at route)."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    node_type: Optional[GraphNodeType] = None
    environment_id: Optional[uuid.UUID] = None
    external_identifier: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    status: Optional[GraphNodeStatus] = None
    criticality: Optional[GraphCriticality] = None
    language: Optional[str] = Field(None, max_length=64)
    framework: Optional[str] = Field(None, max_length=128)
    runtime: Optional[str] = Field(None, max_length=128)
    version: Optional[str] = Field(None, max_length=100)
    repository_url: Optional[str] = Field(None, max_length=512)
    documentation_url: Optional[str] = Field(None, max_length=512)
    ownership_team: Optional[str] = Field(None, max_length=255)
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class GraphNodeResponse(IDMixin, TimestampMixin, GraphNodeBase):
    """Node response with identity + timestamps."""

    project_id: uuid.UUID
    entity_kind: str
    entity_id: Optional[uuid.UUID] = None
    # Response side: read the `metadata_` attribute and serialize as
    # `metadata` (validation_alias on the base is for dict/request input).
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class GraphEdgeBase(BaseSchema):
    """Common edge fields."""

    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: GraphEdgeType
    dependency_type: Optional[DependencyType] = None
    confidence: float = Field(1.0, ge=0, le=1)
    source: GraphEdgeSource = GraphEdgeSource.CONFIGURATION
    status: GraphEdgeStatus = GraphEdgeStatus.ACTIVE
    environment_id: Optional[uuid.UUID] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class GraphEdgeResponse(IDMixin, TimestampMixin, GraphEdgeBase):
    """Edge response with identity + timestamps."""

    project_id: uuid.UUID
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class GraphData(BaseSchema):
    """A full graph payload (nodes + edges), used by the explorer."""

    nodes: List[GraphNodeResponse]
    edges: List[GraphEdgeResponse]


NodeListPaged = PaginatedResponse[GraphNodeResponse]
"""Paginated node list (generic alias)."""
EdgeListPaged = PaginatedResponse[GraphEdgeResponse]
"""Paginated edge list (generic alias)."""


class GraphNodeList(BaseSchema):
    """Concrete paginated node list (kept for response_model ergonomics)."""

    items: List[GraphNodeResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


class GraphEdgeList(BaseSchema):
    """Concrete paginated edge list (kept for response_model ergonomics)."""

    items: List[GraphEdgeResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


class GraphDependenciesResponse(BaseSchema):
    """Direct/transitive dependencies of a component node."""

    component_id: uuid.UUID
    direction: Literal["outgoing", "incoming"]
    direct: List[GraphNodeResponse]
    transitive: List[GraphNodeResponse]
    direct_count: int
    transitive_count: int


class ImpactItem(BaseSchema):
    """A single downstream-impacted node with the path that reaches it."""

    node: GraphNodeResponse
    path: List[uuid.UUID]
    hops: int


class ImpactResponse(BaseSchema):
    """Downstream dependency impact for a node."""

    source_id: uuid.UUID
    label: str = "Dependency Impact"
    count: int
    items: List[ImpactItem]
    relation: Literal["downstream"] = "downstream"


class PathResponse(BaseSchema):
    """Shortest-path result between two nodes."""

    found: bool
    path: List[GraphNodeResponse]
    edges: List[GraphEdgeResponse]
    total_hops: int


class EnvDiffCategory(BaseSchema):
    """Category metadata for an environment-diff item."""

    node_type: Optional[str] = None
    name: str
    combined_key: str


class EnvDiffItem(BaseSchema):
    """A single added/removed/changed row in an environment comparison."""

    kind: Literal["node", "edge", "component", "endpoint", "version"]
    category: EnvDiffCategory
    key: str
    name: str
    in_a: bool
    in_b: bool
    environment: Literal["a", "b"] = "a"
    detail: Optional[dict] = None


class EnvSummary(BaseSchema):
    """Per-environment counts in a comparison."""

    environment_id: Optional[uuid.UUID]
    name: str
    node_count: int
    edge_count: int
    component_count: int


class EnvComparisonResponse(BaseSchema):
    """Structural diff between two environments of one project."""

    project_id: uuid.UUID
    environment_a: EnvSummary
    environment_b: EnvSummary
    added: List[EnvDiffItem]
    removed: List[EnvDiffItem]
    changed: List[EnvDiffItem]
    labels: dict = {
        "added": "Only in {b}",
        "removed": "Only in {a}",
        "changed": "Changed",
    }


class SnapshotCreate(BaseSchema):
    """Create a snapshot of a project/environment graph."""

    environment_id: Optional[uuid.UUID] = None
    source: GraphEdgeSource = GraphEdgeSource.MANUAL
    caption: Optional[str] = Field(None, max_length=255)


class SnapshotResponse(IDMixin):
    """Snapshot summary."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    snapshot_version: int
    node_count: int
    edge_count: int
    source: GraphEdgeSource
    caption: Optional[str] = None
    previous_snapshot_id: Optional[uuid.UUID] = None
    created_at: datetime


class SnapshotList(PaginatedResponse[SnapshotResponse]):
    """Paginated snapshot list."""


class SnapshotDiffResponse(BaseSchema):
    """Set-level diff between two snapshots (by graph signature)."""

    a_id: uuid.UUID
    b_id: uuid.UUID
    added_nodes: List[str]
    removed_nodes: List[str]
    added_edges: List[str]
    removed_edges: List[str]
    added_node_names: List[str]
    removed_node_names: List[str]


class SnapshotDetailResponse(SnapshotResponse):
    """Snapshot plus its current node/edge contents."""

    nodes: List[GraphNodeResponse]
    edges: List[GraphEdgeResponse]
    signature: List[str]


class EndpointCreate(BaseSchema):
    """Create a service endpoint for a component."""

    method: str = Field(
        ..., pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE)$"
    )
    path: str = Field(..., min_length=1, max_length=512)
    is_external: bool = False
    environment_id: Optional[uuid.UUID] = None
    metadata_: Optional[dict] = Field(None, validation_alias="metadata")


class EndpointResponse(IDMixin, TimestampMixin):
    """Service endpoint response."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: uuid.UUID
    method: str
    path_template: str
    original_paths: Optional[List[str]] = None
    is_external: bool
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class EndpointList(BaseSchema):
    """Paginated endpoint list."""

    items: List[EndpointResponse]
    total: int
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total_pages: int = Field(..., ge=0)


class OwnerCreate(BaseSchema):
    """Create/update component ownership."""

    team: str = Field(..., min_length=1, max_length=255)
    owner_name: Optional[str] = Field(None, max_length=255)
    contact_email: Optional[str] = Field(None, max_length=255)
    repository_owner: Optional[str] = Field(None, max_length=255)


class OwnerResponse(IDMixin, TimestampMixin):
    """Ownership response."""

    project_id: uuid.UUID
    component_id: uuid.UUID
    team: str
    owner_name: Optional[str] = None
    contact_email: Optional[str] = None
    repository_owner: Optional[str] = None


class AliasCreate(BaseSchema):
    """Create an alias for a graph node."""

    alias: str = Field(..., min_length=1, max_length=255)
    source: GraphEdgeSource = GraphEdgeSource.INFERENCE
    confidence: Optional[float] = Field(None, ge=0, le=1)


class AliasResponse(IDMixin, TimestampMixin):
    """Alias response."""

    project_id: uuid.UUID
    node_id: uuid.UUID
    alias: str
    source: GraphEdgeSource
    confidence: Optional[float] = None


class AliasList(PaginatedResponse[AliasResponse]):
    """Paginated alias list."""


class DiscoveryResponse(IDMixin, TimestampMixin):
    """Weak-evidence component suggestion awaiting explicit resolution."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    discovered_name: str
    suggested_node_type: GraphNodeType
    identity_hint: Optional[dict] = None
    evidence_count: int
    evidence_sources: Optional[list] = None
    confidence: Optional[float] = None
    status: DiscoveredComponentStatus
    metadata_: Optional[dict] = Field(None, serialization_alias="metadata")


class DiscoveryList(PaginatedResponse[DiscoveryResponse]):
    """Paginated discovery-record list."""


class DiscoveryRegister(BaseSchema):
    """Explicit registration of a discovery record as a real node."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    node_type: Optional[GraphNodeType] = None


class ReconcileResponse(BaseSchema):
    """Result of a reconciliation run."""

    reconciliation_run_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    input_source: GraphEdgeSource
    nodes_created: int
    edges_created: int
    edges_updated: int
    edges_marked_stale: int
    errors: Optional[list] = None
    status: ReconciliationStatus
    started_at: datetime
    finished_at: Optional[datetime] = None


class GraphHealthRow(BaseSchema):
    """Aggregated data-quality row in the health response."""

    check_type: str
    severity: DataQualitySeverity
    count: int
    latest_detected_at: Optional[datetime] = None


class GraphHealthResponse(BaseSchema):
    """Project graph health summary."""

    project_id: uuid.UUID
    node_count: int
    edge_count: int
    last_reconciled_at: Optional[datetime] = None
    data_quality: List[GraphHealthRow]
    ok: bool


class SearchHit(BaseSchema):
    """A single search result across graph entities."""

    kind: Literal["node", "endpoint", "repository", "alias"]
    id: uuid.UUID
    name: str
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    node_type: Optional[GraphNodeType] = None
    score: float


class SearchResponse(BaseSchema):
    """Search results."""

    query: str
    results: List[SearchHit]


class DataQualityResponse(IDMixin):
    """Persisted data-quality finding."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    check_type: str
    severity: DataQualitySeverity
    detail: Optional[dict] = None
    detected_at: datetime


class DataQualityList(PaginatedResponse[DataQualityResponse]):
    """Paginated data-quality record list."""
