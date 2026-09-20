"""Phase 6: code intelligence & AI debugger tables

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-20 14:00:00.000000

Creates the code-intelligence domain and extends ``code_repositories``:

* ``repository_snapshots``  — the pinned revision every code claim is made against
* ``code_files``            — one indexed file per snapshot (content-hashed)
* ``code_symbols``          — definitions (the validation anchor for line ranges)
* ``code_references``       — syntactic occurrences, resolved or not
* ``code_relationships``    — resolved, typed edges of the code graph
* ``trace_code_mappings``   — span → code connections with confidence
* ``code_risk_signals``     — deterministic investigation signals (§44)
* ``code_index_runs``       — indexing audit, including incremental passes
* ``debug_sessions``        — one investigation, pinned to one snapshot
* ``debug_analysis_runs``   — one AI run, fully audited (context, limits, errors)
* ``debug_messages``        — the grounded conversation
* ``debug_hypotheses``      — hypotheses whose status is decided by evidence
* ``debug_code_locations``  — suspected locations with their validation outcome
* ``debug_evidence``        — cited facts, each pointing at a stored row
* ``debug_tool_calls``      — bounded read-only tool invocations

Two pre-existing enum types are **not** created here and are referenced with
``create_type=False``:

* ``confidencelevel``  — Phase 4 (f1a2b3c4d5e6): a debugging confidence and a
  causal confidence must be one comparable scale.
* ``evidencepolarity`` — Phase 4: supporting/contradicting/neutral is the same
  distinction for causal and for debugging evidence.

``debuganalysisstatus`` is created once and shared by ``code_index_runs`` and
``debug_analysis_runs`` so an indexing pass and an analysis run report their
outcome on the same scale.

Table order matters: symbols reference files, references and relationships
reference symbols, and the debugger tables reference snapshots, so the code
tables are created before the debugger tables.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


REPOSITORY_INDEX_STATUSES = [
    "PENDING",
    "INDEXING",
    "INDEXED",
    "PARTIAL",
    "STALE",
    "FAILED",
]
SNAPSHOT_STATUSES = ["CREATED", "INDEXING", "READY", "PARTIAL", "FAILED"]
CODE_VERSION_STATUSES = ["RESOLVED", "INFERRED", "UNKNOWN"]
PARSE_STATUSES = ["PENDING", "PARSED", "PARTIAL", "UNSUPPORTED", "FAILED"]
CODE_SYMBOL_TYPES = [
    "MODULE",
    "CLASS",
    "FUNCTION",
    "METHOD",
    "INTERFACE",
    "VARIABLE",
    "CONSTANT",
    "ROUTE",
    "HANDLER",
    "MODEL",
    "QUERY",
    "SERVICE",
    "UNKNOWN",
]
REFERENCE_KINDS = [
    "CALL",
    "IMPORT",
    "ATTRIBUTE",
    "DEFINITION",
    "TYPE",
    "DECORATOR",
    "ROUTE",
    "QUERY",
    "UNKNOWN",
]
CODE_RELATIONSHIP_TYPES = [
    "IMPORTS",
    "CALLS",
    "DEFINES",
    "IMPLEMENTS",
    "INHERITS",
    "READS",
    "WRITES",
    "QUERIES",
    "CALLS_API",
    "HANDLES_ROUTE",
    "THROWS",
    "CATCHES",
]
TRACE_MAPPING_KINDS = [
    "ROUTE",
    "STACK_FRAME",
    "SERVICE",
    "SYMBOL",
    "OPERATION",
    "UNMAPPED",
]
LOCATION_VALIDATIONS = [
    "VALID",
    "UNVERIFIED",
    "INVALID_FILE",
    "INVALID_SYMBOL",
    "INVALID_LINE_RANGE",
    "WRONG_SNAPSHOT",
]
RISK_SIGNAL_TYPES = [
    "RECENTLY_MODIFIED",
    "HIGH_COMPLEXITY",
    "HIGH_FAN_IN",
    "HIGH_FAN_OUT",
    "FREQUENTLY_FAILING",
    "FREQUENTLY_CHANGED",
    "ERROR_PRONE_PATH",
    "DEPENDENCY_BOUNDARY",
    "DATABASE_OPERATION",
    "EXTERNAL_API_CALL",
]
DEBUG_SESSION_STATUSES = [
    "CREATED",
    "CONTEXT_BUILDING",
    "ANALYZING",
    "WAITING_FOR_VALIDATION",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
DEBUG_ANALYSIS_STATUSES = [
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "DEGRADED",
    "FAILED",
    "CANCELLED",
    "LIMIT_REACHED",
]
DEBUG_MESSAGE_ROLES = ["ENGINEER", "ARGUS", "SYSTEM"]
HYPOTHESIS_CATEGORIES = [
    "INCORRECT_ERROR_HANDLING",
    "TIMEOUT_CONFIGURATION",
    "RETRY_LOGIC",
    "RESOURCE_EXHAUSTION",
    "DATABASE_QUERY",
    "CONCURRENCY",
    "RACE_CONDITION",
    "INVALID_STATE",
    "INPUT_HANDLING",
    "DEPENDENCY_FAILURE",
    "CONFIGURATION",
    "API_CONTRACT",
    "DATA_CONSISTENCY",
    "UNKNOWN",
]
HYPOTHESIS_VALIDATION_STATUSES = [
    "UNVERIFIED",
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "WEAKENED",
    "REFUTED",
    "INVALID_REFERENCE",
]
EVIDENCE_KINDS = [
    "INCIDENT_EVIDENCE",
    "ANOMALY",
    "TRACE",
    "SPAN",
    "LOG",
    "METRIC",
    "DEPLOYMENT",
    "CONFIGURATION_CHANGE",
    "COMMIT",
    "FILE",
    "SYMBOL",
    "REPRODUCTION",
    "CAUSAL_ANALYSIS",
    "CAUSAL_CANDIDATE",
    "GRAPH_EDGE",
    "RISK_SIGNAL",
    "RECURRENCE",
    "MISSING",
]
TOOL_CALL_STATUSES = [
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "REJECTED",
    "TRUNCATED",
]

#: (name, values) for the enum types this migration creates.
_NEW_ENUMS = [
    ("repositoryindexstatus", REPOSITORY_INDEX_STATUSES),
    ("snapshotstatus", SNAPSHOT_STATUSES),
    ("codeversionstatus", CODE_VERSION_STATUSES),
    ("parsestatus", PARSE_STATUSES),
    ("codesymboltype", CODE_SYMBOL_TYPES),
    ("referencekind", REFERENCE_KINDS),
    ("coderelationshiptype", CODE_RELATIONSHIP_TYPES),
    ("tracemappingkind", TRACE_MAPPING_KINDS),
    ("locationvalidation", LOCATION_VALIDATIONS),
    ("risksignaltype", RISK_SIGNAL_TYPES),
    ("debugsessionstatus", DEBUG_SESSION_STATUSES),
    ("debuganalysisstatus", DEBUG_ANALYSIS_STATUSES),
    ("debugmessagerole", DEBUG_MESSAGE_ROLES),
    ("hypothesiscategory", HYPOTHESIS_CATEGORIES),
    ("hypothesisvalidationstatus", HYPOTHESIS_VALIDATION_STATUSES),
    ("evidencekind", EVIDENCE_KINDS),
    ("toolcallstatus", TOOL_CALL_STATUSES),
]


def _enum(values: list[str], name: str, create_type: bool = False) -> postgresql.ENUM:
    """Reference an enum type without creating it.

    ``confidencelevel`` (Phase 4), ``evidencepolarity`` (Phase 4) and the two
    statuses this migration creates for reuse across tables are all referenced
    with ``create_type=False`` so they are never re-created per column.
    """
    return postgresql.ENUM(*values, name=name, create_type=create_type)


def _create_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, values in _NEW_ENUMS:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def _drop_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, _values in reversed(_NEW_ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def _timestamps() -> list[sa.Column]:
    """The shared ``BaseModel`` columns every Phase 6 table carries."""
    return [
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
    ]


def upgrade() -> None:
    _create_enum_types()

    # ---- 0. code_repositories: Phase 6 index metadata ----------------------
    op.add_column(
        "code_repositories", sa.Column("credentials_ref", sa.String(255), nullable=True)
    )
    op.add_column(
        "code_repositories", sa.Column("local_path", sa.String(1024), nullable=True)
    )
    op.add_column(
        "code_repositories", sa.Column("language", sa.String(32), nullable=True)
    )
    op.add_column(
        "code_repositories", sa.Column("framework", sa.String(64), nullable=True)
    )
    op.add_column(
        "code_repositories",
        sa.Column(
            "index_status",
            _enum(REPOSITORY_INDEX_STATUSES, "repositoryindexstatus"),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column(
        "code_repositories",
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "code_repositories", sa.Column("last_indexed_commit", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_code_repositories_index_status", "code_repositories", ["index_status"]
    )

    # ---- 1. repository_snapshots ------------------------------------------
    op.create_table(
        "repository_snapshots",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("repository_id", postgresql.UUID(), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("commit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commit_message", sa.Text(), nullable=True),
        sa.Column("commit_author", sa.String(255), nullable=True),
        sa.Column("reference", sa.String(512), nullable=True),
        sa.Column("root_path", sa.String(1024), nullable=True),
        sa.Column(
            "provider_name", sa.String(50), nullable=False, server_default="local"
        ),
        sa.Column(
            "version_status",
            _enum(CODE_VERSION_STATUSES, "codeversionstatus"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("version_evidence", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _enum(SNAPSHOT_STATUSES, "snapshotstatus"),
            nullable=False,
            server_default="CREATED",
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbol_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("languages", postgresql.JSONB(), nullable=True),
        sa.Column("index_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_repositories.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repository_snapshots_project_id", "repository_snapshots", ["project_id"]
    )
    op.create_index(
        "ix_repository_snapshots_repository_id", "repository_snapshots", ["repository_id"]
    )
    op.create_index(
        "ix_repository_snapshots_commit_sha", "repository_snapshots", ["commit_sha"]
    )
    op.create_index("ix_repository_snapshots_status", "repository_snapshots", ["status"])
    op.create_index(
        "ix_repo_snapshots_repo_commit",
        "repository_snapshots",
        ["repository_id", "commit_sha"],
    )
    op.create_index(
        "ix_repo_snapshots_project_status",
        "repository_snapshots",
        ["project_id", "status"],
    )

    # ---- 2. code_files -----------------------------------------------------
    op.create_table(
        "code_files",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("repository_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("module_name", sa.String(512), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "parse_status",
            _enum(PARSE_STATUSES, "parsestatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("last_commit_sha", sa.String(64), nullable=True),
        sa.Column("last_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_author", sa.String(255), nullable=True),
        sa.Column("file_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_repositories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "path", name="uq_code_files_snapshot_path"),
    )
    op.create_index("ix_code_files_project_id", "code_files", ["project_id"])
    op.create_index("ix_code_files_repository_id", "code_files", ["repository_id"])
    op.create_index("ix_code_files_snapshot_id", "code_files", ["snapshot_id"])
    op.create_index("ix_code_files_path", "code_files", ["path"])
    op.create_index("ix_code_files_language", "code_files", ["language"])
    op.create_index("ix_code_files_content_hash", "code_files", ["content_hash"])
    op.create_index("ix_code_files_is_test", "code_files", ["is_test"])
    op.create_index("ix_code_files_parse_status", "code_files", ["parse_status"])
    op.create_index("ix_code_files_repo_path", "code_files", ["repository_id", "path"])

    # ---- 3. code_symbols ---------------------------------------------------
    op.create_table(
        "code_symbols",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("repository_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=False),
        sa.Column("file_id", postgresql.UUID(), nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("symbol_name", sa.String(512), nullable=False),
        sa.Column("qualified_name", sa.String(1024), nullable=False),
        sa.Column(
            "symbol_type",
            _enum(CODE_SYMBOL_TYPES, "codesymboltype"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("documentation", sa.Text(), nullable=True),
        sa.Column("symbol_hash", sa.String(64), nullable=True),
        sa.Column("parent_symbol_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "is_async", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("complexity", sa.Integer(), nullable=True),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("route", sa.String(512), nullable=True),
        sa.Column("http_method", sa.String(16), nullable=True),
        sa.Column("symbol_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_repositories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["file_id"], ["code_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_symbol_id"], ["code_symbols.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "project_id",
        "repository_id",
        "snapshot_id",
        "file_id",
        "file_path",
        "symbol_name",
        "qualified_name",
        "symbol_type",
        "symbol_hash",
        "parent_symbol_id",
        "complexity",
        "component_id",
        "route",
    ):
        op.create_index(f"ix_code_symbols_{column}", "code_symbols", [column])
    op.create_index(
        "ix_code_symbols_snapshot_name", "code_symbols", ["snapshot_id", "symbol_name"]
    )
    op.create_index(
        "ix_code_symbols_snapshot_path", "code_symbols", ["snapshot_id", "file_path"]
    )
    op.create_index(
        "ix_code_symbols_qualified", "code_symbols", ["snapshot_id", "qualified_name"]
    )

    # ---- 4. code_references ------------------------------------------------
    op.create_table(
        "code_references",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=False),
        sa.Column("file_id", postgresql.UUID(), nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column(
            "reference_kind",
            _enum(REFERENCE_KINDS, "referencekind"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("column", sa.Integer(), nullable=True),
        sa.Column("enclosing_symbol_id", postgresql.UUID(), nullable=True),
        sa.Column("symbol_id", postgresql.UUID(), nullable=True),
        sa.Column("resolution", sa.String(64), nullable=True),
        sa.Column("reference_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["file_id"], ["code_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enclosing_symbol_id"], ["code_symbols.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["symbol_id"], ["code_symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "project_id",
        "snapshot_id",
        "file_id",
        "file_path",
        "name",
        "reference_kind",
        "line",
        "enclosing_symbol_id",
        "symbol_id",
    ):
        op.create_index(f"ix_code_references_{column}", "code_references", [column])
    op.create_index(
        "ix_code_refs_snapshot_name", "code_references", ["snapshot_id", "name"]
    )
    op.create_index(
        "ix_code_refs_enclosing", "code_references", ["enclosing_symbol_id"]
    )

    # ---- 5. code_relationships --------------------------------------------
    op.create_table(
        "code_relationships",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=False),
        sa.Column("source_symbol_id", postgresql.UUID(), nullable=False),
        sa.Column("target_symbol_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "relationship_type",
            _enum(CODE_RELATIONSHIP_TYPES, "coderelationshiptype"),
            nullable=False,
        ),
        sa.Column("line", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("relationship_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_symbol_id"], ["code_symbols.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_symbol_id"], ["code_symbols.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "source_symbol_id",
            "target_symbol_id",
            "relationship_type",
            "line",
            name="uq_code_relationship_edge",
        ),
    )
    op.create_index("ix_code_relationships_project_id", "code_relationships", ["project_id"])
    op.create_index(
        "ix_code_relationships_snapshot_id", "code_relationships", ["snapshot_id"]
    )
    op.create_index(
        "ix_code_relationships_relationship_type",
        "code_relationships",
        ["relationship_type"],
    )
    op.create_index(
        "ix_code_relationships_component_id", "code_relationships", ["component_id"]
    )
    op.create_index(
        "ix_code_rel_source_type",
        "code_relationships",
        ["source_symbol_id", "relationship_type"],
    )
    op.create_index(
        "ix_code_rel_target_type",
        "code_relationships",
        ["target_symbol_id", "relationship_type"],
    )

    # ---- 6. trace_code_mappings -------------------------------------------
    op.create_table(
        "trace_code_mappings",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("span_id", sa.String(64), nullable=True),
        sa.Column("operation", sa.String(512), nullable=True),
        sa.Column("service_name", sa.String(255), nullable=True),
        sa.Column("endpoint", sa.String(512), nullable=True),
        sa.Column("http_method", sa.String(16), nullable=True),
        sa.Column(
            "mapping_kind",
            _enum(TRACE_MAPPING_KINDS, "tracemappingkind"),
            nullable=False,
            server_default="UNMAPPED",
        ),
        sa.Column("symbol_id", postgresql.UUID(), nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("unmapped_reason", sa.String(255), nullable=True),
        sa.Column("mapping_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["symbol_id"], ["code_symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trace_code_mappings_project_id", "trace_code_mappings", ["project_id"])
    op.create_index(
        "ix_trace_code_mappings_snapshot_id", "trace_code_mappings", ["snapshot_id"]
    )
    op.create_index(
        "ix_trace_code_mappings_component_id", "trace_code_mappings", ["component_id"]
    )
    op.create_index("ix_trace_code_mappings_trace_id", "trace_code_mappings", ["trace_id"])
    op.create_index("ix_trace_code_mappings_span_id", "trace_code_mappings", ["span_id"])
    op.create_index(
        "ix_trace_code_mappings_service_name", "trace_code_mappings", ["service_name"]
    )
    op.create_index("ix_trace_code_mappings_endpoint", "trace_code_mappings", ["endpoint"])
    op.create_index(
        "ix_trace_code_mappings_mapping_kind", "trace_code_mappings", ["mapping_kind"]
    )
    op.create_index(
        "ix_trace_code_mappings_symbol_id", "trace_code_mappings", ["symbol_id"]
    )
    op.create_index("ix_trace_code_mappings_trace", "trace_code_mappings", ["trace_id"])
    op.create_index("ix_trace_code_mappings_span", "trace_code_mappings", ["span_id"])
    op.create_index(
        "ix_trace_code_mappings_project",
        "trace_code_mappings",
        ["project_id", "created_at"],
    )

    # ---- 7. code_risk_signals ---------------------------------------------
    op.create_table(
        "code_risk_signals",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=False),
        sa.Column("symbol_id", postgresql.UUID(), nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column(
            "signal_type",
            _enum(RISK_SIGNAL_TYPES, "risksignaltype"),
            nullable=False,
        ),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["symbol_id"], ["code_symbols.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_code_risk_signals_project_id", "code_risk_signals", ["project_id"])
    op.create_index(
        "ix_code_risk_signals_snapshot_id", "code_risk_signals", ["snapshot_id"]
    )
    op.create_index("ix_code_risk_signals_symbol_id", "code_risk_signals", ["symbol_id"])
    op.create_index("ix_code_risk_signals_file_path", "code_risk_signals", ["file_path"])
    op.create_index(
        "ix_code_risk_signals_signal_type", "code_risk_signals", ["signal_type"]
    )
    op.create_index(
        "ix_code_risk_signals_snapshot_type",
        "code_risk_signals",
        ["snapshot_id", "signal_type"],
    )
    op.create_index(
        "ix_code_risk_signals_symbol", "code_risk_signals", ["symbol_id", "signal_type"]
    )

    # ---- 8. code_index_runs ------------------------------------------------
    op.create_table(
        "code_index_runs",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("repository_id", postgresql.UUID(), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "status",
            _enum(DEBUG_ANALYSIS_STATUSES, "debuganalysisstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("trigger", sa.String(64), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=True),
        sa.Column(
            "incremental", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("base_commit_sha", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("files_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_modified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbols_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("references_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "relationships_indexed", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("errors", postgresql.JSONB(), nullable=True),
        sa.Column("run_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_repositories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_code_index_runs_project_id", "code_index_runs", ["project_id"])
    op.create_index("ix_code_index_runs_repository_id", "code_index_runs", ["repository_id"])
    op.create_index("ix_code_index_runs_snapshot_id", "code_index_runs", ["snapshot_id"])
    op.create_index("ix_code_index_runs_status", "code_index_runs", ["status"])
    op.create_index(
        "ix_code_index_runs_snapshot_started",
        "code_index_runs",
        ["snapshot_id", "started_at"],
    )

    # ---- 9. debug_sessions -------------------------------------------------
    op.create_table(
        "debug_sessions",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("incident_id", postgresql.UUID(), nullable=False),
        sa.Column("repository_id", postgresql.UUID(), nullable=True),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("causal_analysis_id", postgresql.UUID(), nullable=True),
        sa.Column("experiment_id", postgresql.UUID(), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column(
            "status",
            _enum(DEBUG_SESSION_STATUSES, "debugsessionstatus"),
            nullable=False,
            server_default="CREATED",
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "version_status",
            _enum(CODE_VERSION_STATUSES, "codeversionstatus", create_type=False),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("version_note", sa.Text(), nullable=True),
        sa.Column("context_version", sa.String(32), nullable=False, server_default="1"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("session_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_repositories.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["causal_analysis_id"], ["causal_analyses.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_sessions_project_id", "debug_sessions", ["project_id"])
    op.create_index("ix_debug_sessions_repository_id", "debug_sessions", ["repository_id"])
    op.create_index("ix_debug_sessions_snapshot_id", "debug_sessions", ["snapshot_id"])
    op.create_index(
        "ix_debug_sessions_causal_analysis_id", "debug_sessions", ["causal_analysis_id"]
    )
    op.create_index("ix_debug_sessions_experiment_id", "debug_sessions", ["experiment_id"])
    op.create_index("ix_debug_sessions_status", "debug_sessions", ["status"])
    op.create_index(
        "ix_debug_sessions_incident", "debug_sessions", ["incident_id", "created_at"]
    )
    op.create_index(
        "ix_debug_sessions_project_status", "debug_sessions", ["project_id", "status"]
    )

    # ---- 10. debug_analysis_runs ------------------------------------------
    op.create_table(
        "debug_analysis_runs",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("repository_id", postgresql.UUID(), nullable=True),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "status",
            _enum(DEBUG_ANALYSIS_STATUSES, "debuganalysisstatus", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "kind", sa.String(32), nullable=False, server_default="incident_analysis"
        ),
        sa.Column("provider_name", sa.String(64), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=False, server_default="1"),
        sa.Column("context_version", sa.String(32), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_accessed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_bytes", sa.Integer(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "confidence",
            _enum(
                ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"],
                "confidencelevel",
                create_type=False,
            ),
            nullable=False,
            server_default="INSUFFICIENT",
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("invalid_references", postgresql.JSONB(), nullable=True),
        sa.Column("missing_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("recommended_inspections", postgresql.JSONB(), nullable=True),
        sa.Column("context_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("redaction_report", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("run_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["debug_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_repositories.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_analysis_runs_project_id", "debug_analysis_runs", ["project_id"])
    op.create_index("ix_debug_analysis_runs_status", "debug_analysis_runs", ["status"])
    op.create_index(
        "ix_debug_analysis_runs_session", "debug_analysis_runs", ["session_id", "started_at"]
    )

    # ---- 11. debug_messages -----------------------------------------------
    op.create_table(
        "debug_messages",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(), nullable=True),
        sa.Column("role", _enum(DEBUG_MESSAGE_ROLES, "debugmessagerole"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=True),
        sa.Column("message_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["debug_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["debug_analysis_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_messages_project_id", "debug_messages", ["project_id"])
    op.create_index(
        "ix_debug_messages_session_created", "debug_messages", ["session_id", "created_at"]
    )

    # ---- 12. debug_hypotheses ---------------------------------------------
    op.create_table(
        "debug_hypotheses",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "category",
            _enum(HYPOTHESIS_CATEGORIES, "hypothesiscategory"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column(
            "confidence",
            _enum(
                ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"],
                "confidencelevel",
                create_type=False,
            ),
            nullable=False,
            server_default="INSUFFICIENT",
        ),
        sa.Column(
            "validation_status",
            _enum(HYPOTHESIS_VALIDATION_STATUSES, "hypothesisvalidationstatus"),
            nullable=False,
            server_default="UNVERIFIED",
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "testable", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("test_approach", sa.Text(), nullable=True),
        sa.Column(
            "recurrence_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("hypothesis_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["debug_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["debug_analysis_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_hypotheses_project_id", "debug_hypotheses", ["project_id"])
    op.create_index("ix_debug_hypotheses_category", "debug_hypotheses", ["category"])
    op.create_index(
        "ix_debug_hypotheses_validation_status", "debug_hypotheses", ["validation_status"]
    )
    op.create_index(
        "ix_debug_hypotheses_session", "debug_hypotheses", ["session_id", "created_at"]
    )

    # ---- 13. debug_code_locations -----------------------------------------
    op.create_table(
        "debug_code_locations",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(), nullable=True),
        sa.Column("hypothesis_id", postgresql.UUID(), nullable=True),
        sa.Column("snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("symbol_id", postgresql.UUID(), nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("symbol_name", sa.String(512), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column(
            "label",
            sa.String(64),
            nullable=False,
            server_default="SUSPICIOUS_CODE_PATH",
        ),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "confidence",
            _enum(
                ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"],
                "confidencelevel",
                create_type=False,
            ),
            nullable=False,
            server_default="INSUFFICIENT",
        ),
        sa.Column(
            "validation",
            _enum(LOCATION_VALIDATIONS, "locationvalidation"),
            nullable=False,
            server_default="UNVERIFIED",
        ),
        sa.Column("validation_detail", sa.Text(), nullable=True),
        sa.Column("location_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["debug_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["debug_analysis_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["debug_hypotheses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["repository_snapshots.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["symbol_id"], ["code_symbols.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_code_locations_project_id", "debug_code_locations", ["project_id"])
    op.create_index("ix_debug_code_locations_session_id", "debug_code_locations", ["session_id"])
    op.create_index(
        "ix_debug_code_locations_validation", "debug_code_locations", ["validation"]
    )
    op.create_index(
        "ix_debug_code_locations_analysis", "debug_code_locations", ["analysis_run_id"]
    )
    op.create_index(
        "ix_debug_code_locations_symbol", "debug_code_locations", ["symbol_id"]
    )
    op.create_index(
        "ix_debug_code_locations_file", "debug_code_locations", ["snapshot_id", "file_path"]
    )

    # ---- 14. debug_evidence -----------------------------------------------
    op.create_table(
        "debug_evidence",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(), nullable=True),
        sa.Column("hypothesis_id", postgresql.UUID(), nullable=True),
        sa.Column("kind", _enum(EVIDENCE_KINDS, "evidencekind"), nullable=False),
        sa.Column(
            "polarity",
            _enum(
                ["SUPPORTING", "CONTRADICTING", "NEUTRAL"],
                "evidencepolarity",
                create_type=False,
            ),
            nullable=False,
            server_default="NEUTRAL",
        ),
        sa.Column("reference", sa.String(1024), nullable=False),
        sa.Column("label", sa.String(512), nullable=True),
        sa.Column("source_table", sa.String(64), nullable=True),
        sa.Column("source_id", postgresql.UUID(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "valid", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("strength", sa.Float(), nullable=False, server_default="0"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["debug_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["debug_analysis_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["debug_hypotheses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_evidence_project_id", "debug_evidence", ["project_id"])
    op.create_index("ix_debug_evidence_kind", "debug_evidence", ["kind"])
    op.create_index("ix_debug_evidence_polarity", "debug_evidence", ["polarity"])
    op.create_index("ix_debug_evidence_valid", "debug_evidence", ["valid"])
    op.create_index("ix_debug_evidence_source_id", "debug_evidence", ["source_id"])
    op.create_index(
        "ix_debug_evidence_session_kind", "debug_evidence", ["session_id", "kind"]
    )
    op.create_index(
        "ix_debug_evidence_hypothesis", "debug_evidence", ["hypothesis_id"]
    )
    op.create_index("ix_debug_evidence_ref", "debug_evidence", ["reference"])

    # ---- 15. debug_tool_calls ---------------------------------------------
    op.create_table(
        "debug_tool_calls",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            _enum(TOOL_CALL_STATUSES, "toolcallstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tool_metadata", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["debug_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["debug_analysis_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debug_tool_calls_project_id", "debug_tool_calls", ["project_id"])
    op.create_index("ix_debug_tool_calls_tool_name", "debug_tool_calls", ["tool_name"])
    op.create_index("ix_debug_tool_calls_status", "debug_tool_calls", ["status"])
    op.create_index("ix_debug_tool_calls_run", "debug_tool_calls", ["analysis_run_id"])


def downgrade() -> None:
    op.drop_table("debug_tool_calls")
    op.drop_table("debug_evidence")
    op.drop_table("debug_code_locations")
    op.drop_table("debug_hypotheses")
    op.drop_table("debug_messages")
    op.drop_table("debug_analysis_runs")
    op.drop_table("debug_sessions")
    op.drop_table("code_index_runs")
    op.drop_table("code_risk_signals")
    op.drop_table("trace_code_mappings")
    op.drop_table("code_relationships")
    op.drop_table("code_references")
    op.drop_table("code_symbols")
    op.drop_table("code_files")
    op.drop_table("repository_snapshots")

    op.drop_index("ix_code_repositories_index_status", table_name="code_repositories")
    op.drop_column("code_repositories", "last_indexed_commit")
    op.drop_column("code_repositories", "last_indexed_at")
    op.drop_column("code_repositories", "index_status")
    op.drop_column("code_repositories", "framework")
    op.drop_column("code_repositories", "language")
    op.drop_column("code_repositories", "local_path")
    op.drop_column("code_repositories", "credentials_ref")

    _drop_enum_types()
