"""Phase 3: anomaly detection & incident intelligence tables

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-09-19 12:00:00.000000

Phase 3 adds a deterministic detection layer over Phase 1 telemetry and a
correlation layer that groups anomalies into Phase 0 incidents.

Design notes:

* The existing ``incidents`` / ``incident_evidence`` models are *extended in
  place* — one incident concept, no parallel table. ``incidentstatus`` gains
  ``ACKNOWLEDGED`` and ``evidencetype`` gains ``SPAN``/``HEALTH_CHECK``/
  ``GRAPH``/``ANOMALY``.
* New tables are project/environment scoped with ``ON DELETE CASCADE`` so a raw
  project delete can never be blocked by, or orphan, Phase 3 data.
* Component references use ``ON DELETE SET NULL`` — deleting a component must
  not erase incident history.
* ``incident_evidence.anomaly_id`` is ``SET NULL`` (evidence outlives the
  anomaly it cites); ``anomalies.incident_id`` is ``SET NULL`` so deleting an
  incident never destroys the anomalies.
* Shared PostgreSQL enum types (``anomalytype`` etc.) are created **once**,
  explicitly, then referenced with ``create_type=False`` — otherwise each
  ``create_table`` that reuses a type would attempt to create it again.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ANOMALY_TYPES = [
    "METRIC_THRESHOLD",
    "METRIC_BASELINE_DEVIATION",
    "ERROR_RATE_SPIKE",
    "LATENCY_SPIKE",
    "THROUGHPUT_DROP",
    "LOG_PATTERN_SPIKE",
    "TRACE_FAILURE_SPIKE",
    "HEALTH_DEGRADATION",
    "REQUEST_RATE_CHANGE",
    "RESOURCE_USAGE_SPIKE",
    "DEPLOYMENT_RELATED_CHANGE",
    "CONFIGURATION_RELATED_CHANGE",
]
ANOMALY_SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
ANOMALY_STATUSES = ["DETECTED", "ACKNOWLEDGED", "INVESTIGATING", "RESOLVED", "EXPIRED"]
ANOMALY_SOURCES = [
    "METRIC",
    "LOG",
    "TRACE",
    "SPAN",
    "HEALTH_CHECK",
    "DEPLOYMENT",
    "CONFIGURATION",
    "GRAPH",
    "COMPOSITE",
    "UNKNOWN",
]
BASELINE_STRATEGIES = ["STATIC", "ROLLING"]
RULE_CONDITIONS = [
    "THRESHOLD",
    "BASELINE_DEVIATION",
    "Z_SCORE",
    "RATE_CHANGE",
    "ERROR_RATE",
    "LATENCY_RATIO",
    "PATTERN_SPIKE",
    "HEALTH_TRANSITION",
    "TRACE_FAILURE_RATE",
]
TIMELINE_EVENT_TYPES = [
    "INCIDENT_CREATED",
    "ANOMALY_DETECTED",
    "ANOMALY_UPDATED",
    "COMPONENT_AFFECTED",
    "DEPLOYMENT_OCCURRED",
    "CONFIGURATION_CHANGED",
    "HEALTH_CHANGED",
    "TRACE_FAILURE",
    "LOG_PATTERN_SPIKE",
    "EVIDENCE_ADDED",
    "NOTE",
    "INCIDENT_STATUS_CHANGED",
    "INCIDENT_ACKNOWLEDGED",
    "INCIDENT_MITIGATED",
    "INCIDENT_RESOLVED",
]

#: (name, values) for the new enum types, created once at the top of upgrade().
_NEW_ENUMS = [
    ("anomalytype", ANOMALY_TYPES),
    ("anomalyseverity", ANOMALY_SEVERITIES),
    ("anomalystatus", ANOMALY_STATUSES),
    ("anomalysource", ANOMALY_SOURCES),
    ("baselinestrategy", BASELINE_STRATEGIES),
    ("rulecondition", RULE_CONDITIONS),
    ("timelineeventtype", TIMELINE_EVENT_TYPES),
]

_TS = sa.text("now()")


def _enum(values: list[str], name: str) -> postgresql.ENUM:
    """Reference an already-created enum type (never re-create it)."""
    return postgresql.ENUM(*values, name=name, create_type=False)


def _create_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, values in _NEW_ENUMS:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def _add_enum_values(enum_name: str, values: list[str]) -> None:
    """Extend an existing PostgreSQL enum in place.

    ``ALTER TYPE ... ADD VALUE`` is idempotent via ``IF NOT EXISTS`` and, since
    PostgreSQL 12, legal inside a transaction as long as the new value is not
    *used* in that same transaction — which it is not here.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for value in values:
        op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")


def upgrade() -> None:
    # ---- 1. Enums ----------------------------------------------------------
    _create_enum_types()
    _add_enum_values("incidentstatus", ["ACKNOWLEDGED"])
    _add_enum_values("evidencetype", ["SPAN", "HEALTH_CHECK", "GRAPH", "ANOMALY"])

    # ---- 2. Extend incidents ----------------------------------------------
    op.add_column(
        "incidents", sa.Column("fingerprint", sa.String(length=64), nullable=True)
    )
    op.add_column("incidents", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "incidents", sa.Column("primary_component_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "incidents",
        sa.Column(
            "correlation_rationale",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "incidents",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("status_changed_by", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_incidents_fingerprint", "incidents", ["fingerprint"])
    op.create_index(
        "ix_incidents_primary_component_id", "incidents", ["primary_component_id"]
    )
    op.create_foreign_key(
        None,
        "incidents",
        "system_components",
        ["primary_component_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- 3. Detection tables (rules + suppressions before anomalies) ------
    op.create_table(
        "anomaly_rules",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("anomaly_type", _enum(ANOMALY_TYPES, "anomalytype"), nullable=False),
        sa.Column("condition", _enum(RULE_CONDITIONS, "rulecondition"), nullable=False),
        sa.Column("metric_name", sa.String(length=255), nullable=True),
        sa.Column(
            "baseline_strategy",
            _enum(BASELINE_STRATEGIES, "baselinestrategy"),
            nullable=False,
        ),
        sa.Column("expected_value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("multiplier", sa.Float(), nullable=True),
        sa.Column("z_threshold", sa.Float(), nullable=True),
        sa.Column("min_samples", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("persistence_cycles", sa.Integer(), nullable=False),
        sa.Column(
            "severity", _enum(ANOMALY_SEVERITIES, "anomalyseverity"), nullable=False
        ),
        sa.Column(
            "severity_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_anomaly_rules_project_name"),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "project_id",
        "environment_id",
        "component_id",
        "anomaly_type",
        "metric_name",
        "enabled",
    ):
        op.create_index(f"ix_anomaly_rules_{col}", "anomaly_rules", [col])

    op.create_table(
        "anomaly_suppressions",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("anomaly_type", _enum(ANOMALY_TYPES, "anomalytype"), nullable=True),
        sa.Column("metric_name", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "project_id",
        "environment_id",
        "component_id",
        "anomaly_type",
        "starts_at",
        "ends_at",
        "enabled",
    ):
        op.create_index(f"ix_anomaly_suppressions_{col}", "anomaly_suppressions", [col])

    op.create_table(
        "anomalies",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("rule_id", sa.UUID(), nullable=True),
        sa.Column("incident_id", sa.UUID(), nullable=True),
        sa.Column("anomaly_type", _enum(ANOMALY_TYPES, "anomalytype"), nullable=False),
        sa.Column(
            "severity", _enum(ANOMALY_SEVERITIES, "anomalyseverity"), nullable=False
        ),
        sa.Column("status", _enum(ANOMALY_STATUSES, "anomalystatus"), nullable=False),
        sa.Column("source", _enum(ANOMALY_SOURCES, "anomalysource"), nullable=False),
        sa.Column("metric_name", sa.String(length=255), nullable=True),
        sa.Column("pattern_template", sa.String(length=512), nullable=True),
        sa.Column("observed_value", sa.Float(), nullable=True),
        sa.Column("expected_value", sa.Float(), nullable=True),
        sa.Column("deviation", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("z_score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_event_id", sa.String(length=255), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_by", sa.String(length=255), nullable=True),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("suppression_rule_id", sa.UUID(), nullable=True),
        sa.Column("suppression_reason", sa.String(length=255), nullable=True),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["anomaly_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["suppression_rule_id"], ["anomaly_suppressions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "project_id",
        "environment_id",
        "component_id",
        "rule_id",
        "incident_id",
        "anomaly_type",
        "severity",
        "status",
        "source",
        "metric_name",
        "fingerprint",
        "source_event_id",
        "detected_at",
        "started_at",
        "last_seen_at",
        "suppressed",
    ):
        op.create_index(f"ix_anomalies_{col}", "anomalies", [col])
    op.create_index(
        "ix_anomalies_project_status_severity",
        "anomalies",
        ["project_id", "status", "severity"],
    )
    op.create_index(
        "ix_anomalies_project_fingerprint", "anomalies", ["project_id", "fingerprint"]
    )

    # ---- 4. Extend incident_evidence (anomalies now exist) ----------------
    op.add_column(
        "incident_evidence", sa.Column("component_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "incident_evidence",
        sa.Column("observed_value", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "incident_evidence",
        sa.Column("expected_value", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "incident_evidence",
        sa.Column(
            "severity",
            _enum(["LOW", "MEDIUM", "HIGH", "CRITICAL"], "incidentseverity"),
            nullable=True,
        ),
    )
    op.add_column(
        "incident_evidence", sa.Column("confidence", sa.Float(), nullable=True)
    )
    op.add_column(
        "incident_evidence",
        sa.Column("provenance", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "incident_evidence", sa.Column("relevance_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "incident_evidence", sa.Column("anomaly_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        "ix_incident_evidence_component_id", "incident_evidence", ["component_id"]
    )
    op.create_index("ix_incident_evidence_severity", "incident_evidence", ["severity"])
    op.create_index(
        "ix_incident_evidence_anomaly_id", "incident_evidence", ["anomaly_id"]
    )
    op.create_foreign_key(
        None,
        "incident_evidence",
        "system_components",
        ["component_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        None,
        "incident_evidence",
        "anomalies",
        ["anomaly_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- 5. Incident timeline ---------------------------------------------
    op.create_table(
        "incident_timeline_events",
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column(
            "event_type",
            _enum(TIMELINE_EVENT_TYPES, "timelineeventtype"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("anomaly_id", sa.UUID(), nullable=True),
        sa.Column("evidence_id", sa.UUID(), nullable=True),
        sa.Column(
            "is_context_only",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("provenance", sa.String(length=64), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["anomaly_id"], ["anomalies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["incident_evidence.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "incident_id",
        "project_id",
        "environment_id",
        "event_type",
        "occurred_at",
        "component_id",
        "anomaly_id",
    ):
        op.create_index(
            f"ix_incident_timeline_events_{col}", "incident_timeline_events", [col]
        )
    op.create_index(
        "ix_incident_timeline_incident_occurred",
        "incident_timeline_events",
        ["incident_id", "occurred_at"],
    )

    # ---- 6. Fingerprint registry & observations ---------------------------
    op.create_table(
        "anomaly_fingerprints",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("anomaly_type", _enum(ANOMALY_TYPES, "anomalytype"), nullable=False),
        sa.Column("anomaly_id", sa.UUID(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_suppressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["anomaly_id"], ["anomalies.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "project_id",
            "fingerprint",
            name="uq_anomaly_fingerprints_project_fingerprint",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "project_id",
        "environment_id",
        "component_id",
        "fingerprint",
        "anomaly_type",
        "anomaly_id",
        "last_seen_at",
    ):
        op.create_index(f"ix_anomaly_fingerprints_{col}", "anomaly_fingerprints", [col])

    op.create_table(
        "anomaly_observations",
        sa.Column("anomaly_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_value", sa.Float(), nullable=True),
        sa.Column("expected_value", sa.Float(), nullable=True),
        sa.Column("deviation", sa.Float(), nullable=True),
        sa.Column("z_score", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=True),
        sa.Column("source_event_id", sa.String(length=255), nullable=True),
        sa.Column(
            "payload_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["anomaly_id"], ["anomalies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "anomaly_id",
        "project_id",
        "component_id",
        "observed_at",
        "source_event_id",
    ):
        op.create_index(f"ix_anomaly_observations_{col}", "anomaly_observations", [col])
    op.create_index(
        "ix_anomaly_observations_anomaly_time",
        "anomaly_observations",
        ["anomaly_id", "observed_at"],
    )

    # ---- 7. Baselines ------------------------------------------------------
    op.create_table(
        "anomaly_baselines",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("component_id", sa.UUID(), nullable=True),
        sa.Column("metric_name", sa.String(length=255), nullable=False),
        sa.Column(
            "strategy", _enum(BASELINE_STRATEGIES, "baselinestrategy"), nullable=False
        ),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=True),
        sa.Column("median", sa.Float(), nullable=True),
        sa.Column("stddev", sa.Float(), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("p50", sa.Float(), nullable=True),
        sa.Column("p95", sa.Float(), nullable=True),
        sa.Column("p99", sa.Float(), nullable=True),
        sa.Column("expected_value", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "project_id",
        "environment_id",
        "component_id",
        "metric_name",
        "computed_at",
    ):
        op.create_index(f"ix_anomaly_baselines_{col}", "anomaly_baselines", [col])
    op.create_index(
        "ix_anomaly_baselines_scope_metric_time",
        "anomaly_baselines",
        ["project_id", "metric_name", "computed_at"],
    )

    # ---- 8. Maintenance windows -------------------------------------------
    op.create_table(
        "maintenance_windows",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("suppress_anomalies", sa.Boolean(), nullable=False),
        sa.Column("downgrade_severity", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_TS, nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("project_id", "environment_id", "starts_at", "ends_at", "enabled"):
        op.create_index(f"ix_maintenance_windows_{col}", "maintenance_windows", [col])


def downgrade() -> None:
    op.drop_table("maintenance_windows")
    op.drop_table("anomaly_baselines")
    op.drop_table("anomaly_observations")
    op.drop_table("anomaly_fingerprints")
    op.drop_table("incident_timeline_events")

    op.drop_constraint(
        "incident_evidence_anomaly_id_fkey", "incident_evidence", type_="foreignkey"
    )
    op.drop_constraint(
        "incident_evidence_component_id_fkey", "incident_evidence", type_="foreignkey"
    )
    op.drop_index("ix_incident_evidence_anomaly_id", table_name="incident_evidence")
    op.drop_index("ix_incident_evidence_severity", table_name="incident_evidence")
    op.drop_index("ix_incident_evidence_component_id", table_name="incident_evidence")
    op.drop_column("incident_evidence", "anomaly_id")
    op.drop_column("incident_evidence", "relevance_reason")
    op.drop_column("incident_evidence", "provenance")
    op.drop_column("incident_evidence", "confidence")
    op.drop_column("incident_evidence", "severity")
    op.drop_column("incident_evidence", "expected_value")
    op.drop_column("incident_evidence", "observed_value")
    op.drop_column("incident_evidence", "component_id")

    op.drop_table("anomalies")
    op.drop_table("anomaly_suppressions")
    op.drop_table("anomaly_rules")

    op.drop_constraint(
        "incidents_primary_component_id_fkey", "incidents", type_="foreignkey"
    )
    op.drop_index("ix_incidents_primary_component_id", table_name="incidents")
    op.drop_index("ix_incidents_fingerprint", table_name="incidents")
    op.drop_column("incidents", "status_changed_by")
    op.drop_column("incidents", "acknowledged_at")
    op.drop_column("incidents", "correlation_rationale")
    op.drop_column("incidents", "primary_component_id")
    op.drop_column("incidents", "summary")
    op.drop_column("incidents", "fingerprint")

    # PostgreSQL cannot remove a value from an enum type, so the added
    # incidentstatus/evidencetype values stay (harmless, forward-compatible).
    # The Phase 3 enum *types* are dropped explicitly — dropping their tables
    # does not remove the types themselves.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name, _ in _NEW_ENUMS:
            op.execute(f"DROP TYPE IF EXISTS {name}")
