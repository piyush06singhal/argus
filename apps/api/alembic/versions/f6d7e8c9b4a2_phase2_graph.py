"""Phase 2: software knowledge graph tables

Revision ID: f6d7e8c9b4a2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-17 19:50:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f6d7e8c9b4a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


GRAPH_NODE_TYPES = [
    "PROJECT",
    "ENVIRONMENT",
    "COMPONENT",
    "APPLICATION",
    "SERVICE",
    "WORKER",
    "DATABASE",
    "CACHE",
    "QUEUE",
    "EXTERNAL_API",
    "REPOSITORY",
    "INFRASTRUCTURE",
    "ENDPOINT",
    "UNKNOWN",
]

GRAPH_EDGE_TYPES = [
    "CONTAINS",
    "DEPENDS_ON",
    "DEPLOYS",
    "CALLS",
    "READS_FROM",
    "WRITES_TO",
    "PUBLISHES_TO",
    "CONSUMES_FROM",
    "DEPLOYED_AS",
    "IMPLEMENTS",
    "HOSTS",
    "RELATED_TO",
]

GRAPH_EDGE_SOURCES = [
    "MANUAL",
    "CONFIGURATION",
    "TRACE",
    "LOG",
    "DEPLOYMENT",
    "REPOSITORY",
    "INFERENCE",
    "MOCK",
    "UNKNOWN",
]

GRAPH_EDGE_STATUSES = ["ACTIVE", "STALE", "DISABLED", "UNKNOWN"]
GRAPH_NODE_STATUSES = ["ACTIVE", "STALE", "DISABLED", "UNKNOWN"]
GRAPH_CRITICALITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
DISCOVERED_STATUSES = ["PENDING", "REGISTERED", "IGNORED"]
DATA_QUALITY_SEVERITIES = ["INFO", "WARNING", "ERROR"]
RECONCILIATION_STATUSES = ["RUNNING", "SUCCESS", "FAILED"]


def upgrade() -> None:
    # ---- graph_nodes ----
    op.create_table(
        "graph_nodes",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column(
            "node_type",
            sa.Enum(*GRAPH_NODE_TYPES, name="graphnodetype"),
            nullable=False,
        ),
        sa.Column("entity_kind", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("external_identifier", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.Enum(*GRAPH_NODE_STATUSES, name="graphnodestatus"),
            nullable=False,
        ),
        sa.Column(
            "criticality",
            sa.Enum(*GRAPH_CRITICALITIES, name="graphcriticality"),
            nullable=False,
        ),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("framework", sa.String(length=128), nullable=True),
        sa.Column("runtime", sa.String(length=128), nullable=True),
        sa.Column("version", sa.String(length=100), nullable=True),
        sa.Column("repository_url", sa.String(length=512), nullable=True),
        sa.Column("documentation_url", sa.String(length=512), nullable=True),
        sa.Column("ownership_team", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.UniqueConstraint(
            "project_id",
            "entity_kind",
            "entity_id",
            name="uq_graph_nodes_project_entity",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_nodes_project_id"), "graph_nodes", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_graph_nodes_environment_id"),
        "graph_nodes",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_nodes_node_type"), "graph_nodes", ["node_type"], unique=False
    )
    op.create_index(
        op.f("ix_graph_nodes_entity_kind"), "graph_nodes", ["entity_kind"], unique=False
    )
    op.create_index(op.f("ix_graph_nodes_name"), "graph_nodes", ["name"], unique=False)
    op.create_index(
        op.f("ix_graph_nodes_external_identifier"),
        "graph_nodes",
        ["external_identifier"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_nodes_status"), "graph_nodes", ["status"], unique=False
    )

    # ---- graph_edges ----
    op.create_table(
        "graph_edges",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("source_node_id", sa.UUID(), nullable=False),
        sa.Column("target_node_id", sa.UUID(), nullable=False),
        sa.Column(
            "edge_type",
            sa.Enum(*GRAPH_EDGE_TYPES, name="graphedgetype"),
            nullable=False,
        ),
        sa.Column(
            "dependency_type",
            postgresql.ENUM(
                "HTTP",
                "DATABASE",
                "QUEUE",
                "CACHE",
                "RPC",
                "FILE",
                "EXTERNAL_API",
                "UNKNOWN",
                name="dependencytype",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "source",
            sa.Enum(*GRAPH_EDGE_SOURCES, name="graphedgesource"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*GRAPH_EDGE_STATUSES, name="graphedgestatus"),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.ForeignKeyConstraint(["source_node_id"], ["graph_nodes.id"]),
        sa.ForeignKeyConstraint(["target_node_id"], ["graph_nodes.id"]),
        sa.Index(
            "uq_graph_edges_source_target_type_env",
            "source_node_id",
            "target_node_id",
            "edge_type",
            sa.text(
                "COALESCE(environment_id, '00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            unique=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_edges_project_id"), "graph_edges", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_graph_edges_environment_id"),
        "graph_edges",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_edges_source_node_id"),
        "graph_edges",
        ["source_node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_edges_target_node_id"),
        "graph_edges",
        ["target_node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_edges_edge_type"), "graph_edges", ["edge_type"], unique=False
    )
    op.create_index(
        op.f("ix_graph_edges_source"), "graph_edges", ["source"], unique=False
    )
    op.create_index(
        op.f("ix_graph_edges_status"), "graph_edges", ["status"], unique=False
    )

    # ---- graph_snapshots ----
    op.create_table(
        "graph_snapshots",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("edge_count", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(*GRAPH_EDGE_SOURCES, name="graphedgesource"),
            nullable=False,
        ),
        sa.Column("caption", sa.String(length=255), nullable=True),
        sa.Column(
            "graph_signature", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("previous_snapshot_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.ForeignKeyConstraint(["previous_snapshot_id"], ["graph_snapshots.id"]),
        sa.Index(
            "uq_graph_snapshots_project_env_version",
            "project_id",
            sa.text(
                "COALESCE(environment_id, '00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            "snapshot_version",
            unique=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_snapshots_project_id"),
        "graph_snapshots",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_snapshots_environment_id"),
        "graph_snapshots",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_snapshots_snapshot_version"),
        "graph_snapshots",
        ["snapshot_version"],
        unique=False,
    )

    # ---- graph_node_aliases ----
    op.create_table(
        "graph_node_aliases",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.Enum(*GRAPH_EDGE_SOURCES, name="graphedgesource"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["node_id"], ["graph_nodes.id"]),
        sa.UniqueConstraint(
            "node_id", "alias", name="uq_graph_node_aliases_node_alias"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_node_aliases_project_id"),
        "graph_node_aliases",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_node_aliases_node_id"),
        "graph_node_aliases",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_node_aliases_alias"),
        "graph_node_aliases",
        ["alias"],
        unique=False,
    )

    # ---- graph_discovery_records ----
    op.create_table(
        "graph_discovery_records",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("discovered_name", sa.String(length=255), nullable=False),
        sa.Column(
            "suggested_node_type",
            sa.Enum(*GRAPH_NODE_TYPES, name="graphnodetype"),
            nullable=False,
        ),
        sa.Column(
            "identity_hint", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column(
            "evidence_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(*DISCOVERED_STATUSES, name="discoveredcomponentstatus"),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_discovery_records_project_id"),
        "graph_discovery_records",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_discovery_records_environment_id"),
        "graph_discovery_records",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_discovery_records_discovered_name"),
        "graph_discovery_records",
        ["discovered_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_discovery_records_evidence_count"),
        "graph_discovery_records",
        ["evidence_count"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_discovery_records_status"),
        "graph_discovery_records",
        ["status"],
        unique=False,
    )

    # ---- graph_reconciliation_runs ----
    op.create_table(
        "graph_reconciliation_runs",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "input_source",
            sa.Enum(*GRAPH_EDGE_SOURCES, name="graphedgesource"),
            nullable=False,
        ),
        sa.Column("nodes_created", sa.Integer(), nullable=False),
        sa.Column("edges_created", sa.Integer(), nullable=False),
        sa.Column("edges_updated", sa.Integer(), nullable=False),
        sa.Column("edges_marked_stale", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.Enum(*RECONCILIATION_STATUSES, name="reconciliationstatus"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_reconciliation_runs_project_id"),
        "graph_reconciliation_runs",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_reconciliation_runs_environment_id"),
        "graph_reconciliation_runs",
        ["environment_id"],
        unique=False,
    )

    # ---- graph_data_quality_records ----
    op.create_table(
        "graph_data_quality_records",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("check_type", sa.String(length=100), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(*DATA_QUALITY_SEVERITIES, name="dataqualityseverity"),
            nullable=False,
        ),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_graph_data_quality_records_project_id"),
        "graph_data_quality_records",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_data_quality_records_environment_id"),
        "graph_data_quality_records",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_data_quality_records_check_type"),
        "graph_data_quality_records",
        ["check_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_graph_data_quality_records_severity"),
        "graph_data_quality_records",
        ["severity"],
        unique=False,
    )

    # ---- service_endpoints ----
    op.create_table(
        "service_endpoints",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path_template", sa.String(length=512), nullable=False),
        sa.Column(
            "original_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("is_external", sa.Boolean(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.ForeignKeyConstraint(["component_id"], ["system_components.id"]),
        sa.UniqueConstraint(
            "component_id",
            "method",
            "path_template",
            name="uq_service_endpoints_component_method_path",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_service_endpoints_project_id"),
        "service_endpoints",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_endpoints_environment_id"),
        "service_endpoints",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_endpoints_component_id"),
        "service_endpoints",
        ["component_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_endpoints_method"),
        "service_endpoints",
        ["method"],
        unique=False,
    )
    op.create_index(
        op.f("ix_service_endpoints_path_template"),
        "service_endpoints",
        ["path_template"],
        unique=False,
    )

    # ---- component_owners ----
    op.create_table(
        "component_owners",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("component_id", sa.UUID(), nullable=False),
        sa.Column("team", sa.String(length=255), nullable=False),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("repository_owner", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["component_id"], ["system_components.id"]),
        sa.UniqueConstraint("component_id", name="uq_component_owners_component"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_component_owners_project_id"),
        "component_owners",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_component_owners_component_id"),
        "component_owners",
        ["component_id"],
        unique=False,
    )


def downgrade() -> None:
    # ---- component_owners ----
    op.drop_index(
        op.f("ix_component_owners_component_id"), table_name="component_owners"
    )
    op.drop_index(op.f("ix_component_owners_project_id"), table_name="component_owners")
    op.drop_table("component_owners")

    # ---- service_endpoints ----
    op.drop_index(
        op.f("ix_service_endpoints_path_template"), table_name="service_endpoints"
    )
    op.drop_index(op.f("ix_service_endpoints_method"), table_name="service_endpoints")
    op.drop_index(
        op.f("ix_service_endpoints_component_id"), table_name="service_endpoints"
    )
    op.drop_index(
        op.f("ix_service_endpoints_environment_id"), table_name="service_endpoints"
    )
    op.drop_index(
        op.f("ix_service_endpoints_project_id"), table_name="service_endpoints"
    )
    op.drop_table("service_endpoints")

    # ---- graph_data_quality_records ----
    op.drop_index(
        op.f("ix_graph_data_quality_records_severity"),
        table_name="graph_data_quality_records",
    )
    op.drop_index(
        op.f("ix_graph_data_quality_records_check_type"),
        table_name="graph_data_quality_records",
    )
    op.drop_index(
        op.f("ix_graph_data_quality_records_environment_id"),
        table_name="graph_data_quality_records",
    )
    op.drop_index(
        op.f("ix_graph_data_quality_records_project_id"),
        table_name="graph_data_quality_records",
    )
    op.drop_table("graph_data_quality_records")
    op.execute("DROP TYPE IF EXISTS dataqualityseverity")

    # ---- graph_reconciliation_runs ----
    op.drop_index(
        op.f("ix_graph_reconciliation_runs_environment_id"),
        table_name="graph_reconciliation_runs",
    )
    op.drop_index(
        op.f("ix_graph_reconciliation_runs_project_id"),
        table_name="graph_reconciliation_runs",
    )
    op.drop_table("graph_reconciliation_runs")
    op.execute("DROP TYPE IF EXISTS reconciliationstatus")

    # ---- graph_discovery_records ----
    op.drop_index(
        op.f("ix_graph_discovery_records_status"), table_name="graph_discovery_records"
    )
    op.drop_index(
        op.f("ix_graph_discovery_records_evidence_count"),
        table_name="graph_discovery_records",
    )
    op.drop_index(
        op.f("ix_graph_discovery_records_discovered_name"),
        table_name="graph_discovery_records",
    )
    op.drop_index(
        op.f("ix_graph_discovery_records_environment_id"),
        table_name="graph_discovery_records",
    )
    op.drop_index(
        op.f("ix_graph_discovery_records_project_id"),
        table_name="graph_discovery_records",
    )
    op.drop_table("graph_discovery_records")
    op.execute("DROP TYPE IF EXISTS discoveredcomponentstatus")

    # ---- graph_node_aliases ----
    op.drop_index(op.f("ix_graph_node_aliases_alias"), table_name="graph_node_aliases")
    op.drop_index(
        op.f("ix_graph_node_aliases_node_id"), table_name="graph_node_aliases"
    )
    op.drop_index(
        op.f("ix_graph_node_aliases_project_id"), table_name="graph_node_aliases"
    )
    op.drop_table("graph_node_aliases")

    # ---- graph_snapshots ----
    op.drop_index(
        op.f("ix_graph_snapshots_snapshot_version"), table_name="graph_snapshots"
    )
    op.drop_index(
        op.f("ix_graph_snapshots_environment_id"), table_name="graph_snapshots"
    )
    op.drop_index(op.f("ix_graph_snapshots_project_id"), table_name="graph_snapshots")
    op.drop_table("graph_snapshots")

    # ---- graph_edges ----
    op.drop_index(op.f("ix_graph_edges_status"), table_name="graph_edges")
    op.drop_index(op.f("ix_graph_edges_source"), table_name="graph_edges")
    op.drop_index(op.f("ix_graph_edges_edge_type"), table_name="graph_edges")
    op.drop_index(op.f("ix_graph_edges_target_node_id"), table_name="graph_edges")
    op.drop_index(op.f("ix_graph_edges_source_node_id"), table_name="graph_edges")
    op.drop_index(op.f("ix_graph_edges_environment_id"), table_name="graph_edges")
    op.drop_index(op.f("ix_graph_edges_project_id"), table_name="graph_edges")
    op.drop_table("graph_edges")
    op.execute("DROP TYPE IF EXISTS graphedgestatus")
    op.execute("DROP TYPE IF EXISTS graphedgesource")
    op.execute("DROP TYPE IF EXISTS graphedgetype")
    # dependencytype is created by the Phase 0 migration (component_dependencies.
    # dependency_type) — it is only reused here, never re-created, so it must
    # not be dropped on downgrade.

    # ---- graph_nodes ----
    op.drop_index(op.f("ix_graph_nodes_status"), table_name="graph_nodes")
    op.drop_index(op.f("ix_graph_nodes_external_identifier"), table_name="graph_nodes")
    op.drop_index(op.f("ix_graph_nodes_name"), table_name="graph_nodes")
    op.drop_index(op.f("ix_graph_nodes_entity_kind"), table_name="graph_nodes")
    op.drop_index(op.f("ix_graph_nodes_node_type"), table_name="graph_nodes")
    op.drop_index(op.f("ix_graph_nodes_environment_id"), table_name="graph_nodes")
    op.drop_index(op.f("ix_graph_nodes_project_id"), table_name="graph_nodes")
    op.drop_table("graph_nodes")
    op.execute("DROP TYPE IF EXISTS graphcriticality")
    op.execute("DROP TYPE IF EXISTS graphnodestatus")
    op.execute("DROP TYPE IF EXISTS graphnodetype")
