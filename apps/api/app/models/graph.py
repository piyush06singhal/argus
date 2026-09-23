"""ARGUS Software Knowledge Graph Models.

The knowledge graph is a *materialized overlay* over the canonical Phase 0/1
store: ``graph_nodes`` and ``graph_edges`` mirror ``system_components`` /
``component_dependencies`` (which remain the single source of truth) through an
``entity_kind + entity_id`` linkage, and add net-new entities the Phase 0/1
model lacks — endpoints, owners, aliases, snapshots, discovery records,
reconciliation runs, and data-quality records.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from app.models.base import Guid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType
from app.models.system import DependencyType

if TYPE_CHECKING:
    pass


class GraphNodeType(str, enum.Enum):
    """Node types in the software knowledge graph."""

    PROJECT = "PROJECT"
    ENVIRONMENT = "ENVIRONMENT"
    COMPONENT = "COMPONENT"
    APPLICATION = "APPLICATION"
    SERVICE = "SERVICE"
    WORKER = "WORKER"
    DATABASE = "DATABASE"
    CACHE = "CACHE"
    QUEUE = "QUEUE"
    EXTERNAL_API = "EXTERNAL_API"
    REPOSITORY = "REPOSITORY"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    ENDPOINT = "ENDPOINT"
    UNKNOWN = "UNKNOWN"


class GraphEdgeType(str, enum.Enum):
    """Relationship types between graph nodes."""

    CONTAINS = "CONTAINS"
    DEPENDS_ON = "DEPENDS_ON"
    DEPLOYS = "DEPLOYS"
    CALLS = "CALLS"
    READS_FROM = "READS_FROM"
    WRITES_TO = "WRITES_TO"
    PUBLISHES_TO = "PUBLISHES_TO"
    CONSUMES_FROM = "CONSUMES_FROM"
    DEPLOYED_AS = "DEPLOYED_AS"
    IMPLEMENTS = "IMPLEMENTS"
    HOSTS = "HOSTS"
    RELATED_TO = "RELATED_TO"


class GraphEdgeSource(str, enum.Enum):
    """Provenance of a graph edge — how the relationship was learned."""

    MANUAL = "MANUAL"
    CONFIGURATION = "CONFIGURATION"
    TRACE = "TRACE"
    LOG = "LOG"
    DEPLOYMENT = "DEPLOYMENT"
    REPOSITORY = "REPOSITORY"
    INFERENCE = "INFERENCE"
    MOCK = "MOCK"
    UNKNOWN = "UNKNOWN"


class GraphEdgeStatus(str, enum.Enum):
    """Lifecycle status of a graph edge."""

    ACTIVE = "ACTIVE"
    STALE = "STALE"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class GraphNodeStatus(str, enum.Enum):
    """Lifecycle status of a graph node."""

    ACTIVE = "ACTIVE"
    STALE = "STALE"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class GraphCriticality(str, enum.Enum):
    """Criticality classification of a node (informational)."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class DiscoveredComponentStatus(str, enum.Enum):
    """Lifecycle of a discovery record."""

    PENDING = "PENDING"
    REGISTERED = "REGISTERED"
    IGNORED = "IGNORED"


class DataQualitySeverity(str, enum.Enum):
    """Severity of a data-quality check result."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ReconciliationStatus(str, enum.Enum):
    """Status of a reconciliation run."""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class GraphNode(BaseModel):
    """A node in the software knowledge graph."""

    __tablename__ = "graph_nodes"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "entity_kind",
            "entity_id",
            name="uq_graph_nodes_project_entity",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    node_type: Mapped[GraphNodeType] = mapped_column(
        SAEnum(GraphNodeType), nullable=False, index=True
    )
    #: Discriminator for the canonical entity mirrored by this node
    #: (e.g. "system_component", "project", "environment", "repository",
    #: "generic", "endpoint").
    entity_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    external_identifier: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    status: Mapped[GraphNodeStatus] = mapped_column(
        SAEnum(GraphNodeStatus),
        default=GraphNodeStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    criticality: Mapped[GraphCriticality] = mapped_column(
        SAEnum(GraphCriticality), default=GraphCriticality.UNKNOWN, nullable=False
    )
    language: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    framework: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    runtime: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    repository_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    documentation_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ownership_team: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    outgoing_edges: Mapped[List["GraphEdge"]] = relationship(
        "GraphEdge",
        foreign_keys="[GraphEdge.source_node_id]",
        back_populates="source_node",
        cascade="all, delete-orphan",
    )
    incoming_edges: Mapped[List["GraphEdge"]] = relationship(
        "GraphEdge",
        foreign_keys="[GraphEdge.target_node_id]",
        back_populates="target_node",
        cascade="all, delete-orphan",
    )
    aliases: Mapped[List["GraphNodeAlias"]] = relationship(
        "GraphNodeAlias", back_populates="node", cascade="all, delete-orphan"
    )


class GraphEdge(BaseModel):
    """A directed relationship between two graph nodes."""

    __tablename__ = "graph_edges"
    __table_args__ = (
        # Unique incl. unspecified-environment (environment_id == NULL):
        # a plain UNIQUE constraint treats NULLs as distinct, letting duplicate
        # (source, target, edge_type, NULL) edges slip through. A unique
        # expression index on COALESCE collapses NULL -> sentinel so the key is
        # consistent on both PostgreSQL and SQLite (tests).
        Index(
            "uq_graph_edges_source_target_type_env",
            "source_node_id",
            "target_node_id",
            "edge_type",
            text("COALESCE(environment_id, '00000000-0000-0000-0000-000000000000')"),
            unique=True,
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    edge_type: Mapped[GraphEdgeType] = mapped_column(
        SAEnum(GraphEdgeType), nullable=False, index=True
    )
    #: Canonical dependency type when the edge mirrors a ComponentDependency.
    dependency_type: Mapped[Optional[DependencyType]] = mapped_column(
        SAEnum(DependencyType), nullable=True
    )
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[GraphEdgeSource] = mapped_column(
        SAEnum(GraphEdgeSource),
        default=GraphEdgeSource.CONFIGURATION,
        nullable=False,
        index=True,
    )
    status: Mapped[GraphEdgeStatus] = mapped_column(
        SAEnum(GraphEdgeStatus),
        default=GraphEdgeStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    source_node: Mapped["GraphNode"] = relationship(
        "GraphNode", foreign_keys=[source_node_id], back_populates="outgoing_edges"
    )
    target_node: Mapped["GraphNode"] = relationship(
        "GraphNode", foreign_keys=[target_node_id], back_populates="incoming_edges"
    )


class GraphSnapshot(BaseModel):
    """A point-in-time snapshot of a project/environment graph."""

    __tablename__ = "graph_snapshots"
    __table_args__ = (
        # Same NULL-collapsing unique key as graph_edges (environment_id NULL
        # must still be a single version sequence per project).
        Index(
            "uq_graph_snapshots_project_env_version",
            "project_id",
            text("COALESCE(environment_id, '00000000-0000-0000-0000-000000000000')"),
            "snapshot_version",
            unique=True,
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[GraphEdgeSource] = mapped_column(
        SAEnum(GraphEdgeSource), default=GraphEdgeSource.MANUAL, nullable=False
    )
    caption: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Sorted list of node + edge ids captured at snapshot time (historical).
    graph_signature: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    previous_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_snapshots.id"), nullable=True
    )


class GraphNodeAlias(BaseModel):
    """An alternative name for a graph node used for identity resolution."""

    __tablename__ = "graph_node_aliases"
    __table_args__ = (
        UniqueConstraint("node_id", "alias", name="uq_graph_node_aliases_node_alias"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source: Mapped[GraphEdgeSource] = mapped_column(
        SAEnum(GraphEdgeSource), default=GraphEdgeSource.INFERENCE, nullable=False
    )
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Relationships
    node: Mapped["GraphNode"] = relationship("GraphNode", back_populates="aliases")


class GraphDiscoveryRecord(BaseModel):
    """A weakly-evidenced component suggestion awaiting explicit resolution."""

    __tablename__ = "graph_discovery_records"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    discovered_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    suggested_node_type: Mapped[GraphNodeType] = mapped_column(
        SAEnum(GraphNodeType), default=GraphNodeType.UNKNOWN, nullable=False
    )
    identity_hint: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True
    )
    evidence_sources: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[DiscoveredComponentStatus] = mapped_column(
        SAEnum(DiscoveredComponentStatus),
        default=DiscoveredComponentStatus.PENDING,
        nullable=False,
        index=True,
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GraphReconciliationRun(BaseModel):
    """A recorded reconciliation pass over a project's graph."""

    __tablename__ = "graph_reconciliation_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_source: Mapped[GraphEdgeSource] = mapped_column(
        SAEnum(GraphEdgeSource), default=GraphEdgeSource.CONFIGURATION, nullable=False
    )
    nodes_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_marked_stale: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    status: Mapped[ReconciliationStatus] = mapped_column(
        SAEnum(ReconciliationStatus),
        default=ReconciliationStatus.RUNNING,
        nullable=False,
    )


class GraphDataQualityRecord(BaseModel):
    """A recorded data-quality finding over the graph."""

    __tablename__ = "graph_data_quality_records"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    check_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[DataQualitySeverity] = mapped_column(
        SAEnum(DataQualitySeverity),
        default=DataQualitySeverity.WARNING,
        nullable=False,
        index=True,
    )
    detail: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServiceEndpoint(BaseModel):
    """A normalized HTTP endpoint belonging to a system component."""

    __tablename__ = "service_endpoints"
    __table_args__ = (
        UniqueConstraint(
            "component_id",
            "method",
            "path_template",
            name="uq_service_endpoints_component_method_path",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    #: Normalized path template, e.g. "/api/inventory/{id}".
    path_template: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    #: The distinct raw paths this template was derived from.
    original_paths: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ComponentOwner(BaseModel):
    """Ownership metadata for a system component."""

    __tablename__ = "component_owners"
    __table_args__ = (
        UniqueConstraint("component_id", name="uq_component_owners_component"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    repository_owner: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Phase 11 §31 — the on-call group and the documentation link. Real columns
    #: rather than keys in a JSON blob: the catalog filters and displays both, and
    #: "which services have no on-call rota" is a question a blob cannot answer.
    #: NULL means *unknown*, which the catalog renders as ``UNKNOWN`` — ARGUS never
    #: infers an owner from repository or commit data (§31).
    on_call: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    documentation_url: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
