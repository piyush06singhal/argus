"""Phase 11: unified reliability platform & control plane

Revision ID: fa1b2c3d4e5
Revises: e4f5a6b7c8d9
Create Date: 2026-09-23 10:00:00.000000

Creates the control plane's own domain — the tables Phase 11 introduced to turn
eleven phases of capability into one operationally coherent platform:

* ``reliability_cases``               — the unified operational object (§14)
* ``reliability_case_timeline``       — the one timeline everything writes to (§15)
* ``component_state_transitions``     — historical component state (§3–§5)
* ``reliability_workflows``           — the workflow engine's runs (§11–§13)
* ``platform_events``                 — the unified event stream (§9, §10)
* ``reliability_context_snapshots``   — time-frozen context (§7, §8)
* ``service_level_objectives``        — objectives (§32, §33)
* ``error_budget_snapshots``          — computed budgets and burn (§34, §35)
* ``platform_notifications``          — deduplicated notifications (§54–§56)
* ``data_quality_issues``             — consistency findings (§87–§90)
* ``configuration_versions``          — append-only configuration history (§91–§94)

Three properties of this DDL are deliberate and worth stating, because they are
what keep Phase 11 from becoming a second source of truth:

1. **Nothing here is authoritative about another phase's facts.** The foreign
   keys into incidents, components, forecasts, patches and remediation rows are
   references for traceability, not ownership: they are ``ON DELETE SET NULL``
   where the platform must survive losing a row, and ``ON DELETE CASCADE`` only
   where the row genuinely belongs to the parent (a case timeline entry, a
   budget snapshot for an objective).
2. **Nothing here can execute anything.** There is no column holding a command,
   a URL to call, or an authorization token. Actions live in Phase 9's schema,
   behind Phase 9's gates, and a case or workflow row only ever *references* an
   action by id.
3. **Evidence is a column, not a comment.** ``evidence``/``snapshot``/``payload``
   JSON exists so every derived conclusion — a state change, a timeline entry, a
   budget reading, an issue, a notification — can name the rows it came from.

Enum type names are namespaced (``component_operational_state``, ``case_status``,
``workflow_stage``, ``slo_status``, ``data_quality_issue_kind``, …) because
PostgreSQL enum types are database-global and Phases 3/7/8/9 already own several
generic names (``incident_status``, ``risk_level``, ``severity``). Twenty-one
types are created here and dropped in ``downgrade``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "fa1b2c3d4e5"
down_revision: Union[str, None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every enum type this migration creates, with its members. Kept as data so
#: creation and teardown cannot drift apart.
_NEW_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "component_operational_state",
        ("INCIDENT", "RECOVERING", "DEGRADED", "AT_RISK", "HEALTHY", "UNKNOWN"),
    ),
    (
        "state_transition_trigger",
        (
            "EVIDENCE",
            "INCIDENT_OPENED",
            "INCIDENT_RESOLVED",
            "ANOMALY_DETECTED",
            "ANOMALY_RESOLVED",
            "FORECAST_RISK",
            "REMEDIATION_STARTED",
            "REMEDIATION_COMPLETED",
            "VERIFICATION",
            "DEPLOYMENT",
            "DATA_GAP",
            "RECOMPUTED",
        ),
    ),
    (
        "case_status",
        (
            "OPEN",
            "TRIAGED",
            "ANALYZING",
            "DIAGNOSED",
            "REMEDIATION_READY",
            "AUTHORIZED",
            "EXECUTING",
            "VERIFYING",
            "RESOLVED",
            "LEARNED",
            "CLOSED",
            "CANCELLED",
        ),
    ),
    (
        "case_trigger",
        ("INCIDENT", "ANOMALY", "FORECAST", "DEPLOYMENT", "SLO_BURN", "OPERATOR"),
    ),
    (
        "timeline_entry_kind",
        (
            "STATE_CHANGE",
            "EVIDENCE",
            "ANALYSIS",
            "PREDICTION",
            "RECOMMENDATION",
            "DECISION",
            "EXECUTION",
            "VERIFICATION",
            "RECOVERY",
            "LEARNING",
            "NOTE",
            "DATA_QUALITY",
        ),
    ),
    (
        "workflow_stage",
        (
            "DETECTED",
            "TRIAGED",
            "ANALYZING",
            "DIAGNOSED",
            "REMEDIATION_READY",
            "AUTHORIZED",
            "EXECUTING",
            "VERIFYING",
            "RESOLVED",
            "LEARNED",
        ),
    ),
    (
        "workflow_status",
        (
            "PENDING",
            "RUNNING",
            "WAITING_APPROVAL",
            "BLOCKED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
        ),
    ),
    (
        "workflow_stop_reason",
        (
            "AUTHORIZATION_EXPIRED",
            "EVIDENCE_STALE",
            "INCIDENT_GONE",
            "STATE_CHANGED",
            "POLICY_CHANGED",
            "KILL_SWITCH",
            "VERIFICATION_FAILED",
            "TIMED_OUT",
            "CANCELLED",
            "ERROR",
        ),
    ),
    (
        "platform_event_type",
        (
            "COMPONENT_STATE_CHANGED",
            "ANOMALY_DETECTED",
            "INCIDENT_CREATED",
            "INCIDENT_UPDATED",
            "RCA_COMPLETED",
            "REPRODUCTION_COMPLETED",
            "PATCH_VERIFIED",
            "FORECAST_GENERATED",
            "RISK_CHANGED",
            "REMEDIATION_PROPOSED",
            "REMEDIATION_STARTED",
            "REMEDIATION_COMPLETED",
            "REMEDIATION_ROLLED_BACK",
            "LEARNING_COMPLETED",
            "DEPLOYMENT_RECORDED",
            "SLO_STATUS_CHANGED",
            "ERROR_BUDGET_BURN",
            "DATA_QUALITY_ISSUE",
            "CASE_OPENED",
            "CASE_CLOSED",
            "CASE_STATUS_CHANGED",
            "CONFIGURATION_CHANGED",
            "NOTIFICATION_RAISED",
        ),
    ),
    (
        "slo_indicator",
        ("AVAILABILITY", "LATENCY", "ERROR_RATE", "SATURATION", "CUSTOM"),
    ),
    ("slo_comparison", ("AT_LEAST", "AT_MOST")),
    ("slo_status", ("MEETING", "AT_RISK", "BREACHED", "UNKNOWN")),
    (
        "burn_rate_state",
        ("NORMAL", "ELEVATED", "FAST_BURN", "CRITICAL_BURN", "UNKNOWN"),
    ),
    (
        "notification_kind",
        (
            "CRITICAL_INCIDENT",
            "HIGH_PREDICTED_RISK",
            "REMEDIATION_APPROVAL",
            "REMEDIATION_FAILURE",
            "ROLLBACK",
            "SLO_BURN",
            "LEARNING_INSIGHT",
            "SYSTEM_DEGRADATION",
            "DATA_QUALITY",
        ),
    ),
    ("notification_severity", ("INFO", "WARNING", "CRITICAL")),
    ("notification_status", ("UNREAD", "READ", "ACKNOWLEDGED", "SUPPRESSED")),
    (
        "data_quality_issue_kind",
        (
            "ORPHANED_RECORD",
            "INCIDENT_WITHOUT_COMPONENT",
            "PREDICTION_WITHOUT_SNAPSHOT",
            "REMEDIATION_WITHOUT_AUTHORIZATION",
            "KNOWLEDGE_WITHOUT_EVIDENCE",
            "STALE_COMPONENT",
            "MISSING_TELEMETRY",
            "BROKEN_RELATIONSHIP",
            "INCONSISTENT_STATE",
            "INVALID_EVIDENCE",
        ),
    ),
    ("data_quality_severity", ("INFO", "WARNING", "CRITICAL")),
    ("data_quality_status", ("OPEN", "ACKNOWLEDGED", "RESOLVED", "IGNORED")),
    (
        "configuration_scope",
        (
            "PROJECT",
            "ENVIRONMENT",
            "PROJECT_SETTINGS",
            "SLO",
            "REMEDIATION_POLICY",
            "LEARNING",
            "NOTIFICATIONS",
            "RETENTION",
            "INTEGRATIONS",
            "FEATURE_FLAGS",
        ),
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    """The column type for one of the enum types above.

    ``create_type=False`` because the types are created once up front; the same
    convention the Phase 6–10 migrations use.
    """
    return postgresql.ENUM(name=name, create_type=False)


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


def _timestamps() -> list[sa.schema.Column]:
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


def _id() -> sa.schema.Column:
    return sa.Column("id", postgresql.UUID(), primary_key=True)


def upgrade() -> None:
    _create_enum_types()

    # ------------------------------------------- §31 ownership: on-call + docs
    #: Phase 2 gave ``component_owners`` a team, a named owner, a contact and a
    #: repository owner. §31 also names the on-call group and the documentation
    #: link, and stores them as columns rather than keys in a JSON blob so the
    #: catalog can filter on them. NULL means "not recorded", which is what the
    #: catalog renders as UNKNOWN — never inferred from commit or repository data.
    op.add_column(
        "component_owners", sa.Column("on_call", sa.String(255), nullable=True)
    )
    op.add_column(
        "component_owners",
        sa.Column("documentation_url", sa.String(1024), nullable=True),
    )

    # ------------------------------------------------------- §14 reliability case
    op.create_table(
        "reliability_cases",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _enum("case_status"),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("trigger", _enum("case_trigger"), nullable=False),
        sa.Column("severity", sa.String(16), nullable=True),
        sa.Column(
            "primary_component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("component_ids", postgresql.JSONB(), nullable=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_type", sa.String(32), nullable=True),
        sa.Column("source_id", postgresql.UUID(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opening_snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("opened_by", sa.String(255), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_by", sa.String(255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id", "reference", name="uq_reliability_cases_reference"
        ),
    )
    op.create_index(
        "ix_reliability_cases_project_id", "reliability_cases", ["project_id"]
    )
    op.create_index(
        "ix_reliability_cases_environment_id", "reliability_cases", ["environment_id"]
    )
    op.create_index("ix_reliability_cases_status", "reliability_cases", ["status"])
    op.create_index(
        "ix_reliability_cases_primary_component_id",
        "reliability_cases",
        ["primary_component_id"],
    )
    op.create_index(
        "ix_reliability_cases_incident_id", "reliability_cases", ["incident_id"]
    )
    op.create_index("ix_reliability_cases_opened_at", "reliability_cases", ["opened_at"])
    op.create_index(
        "ix_reliability_cases_project_status",
        "reliability_cases",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_reliability_cases_project_opened",
        "reliability_cases",
        ["project_id", "opened_at"],
    )

    # ------------------------------------------------- §15 the unified timeline
    op.create_table(
        "reliability_case_timeline",
        _id(),
        *_timestamps(),
        sa.Column(
            "case_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", _enum("timeline_entry_kind"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column(
            "system_action", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("result", sa.String(64), nullable=True),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "case_id", "dedup_key", name="uq_reliability_case_timeline_dedup"
        ),
    )
    op.create_index(
        "ix_reliability_case_timeline_case_id", "reliability_case_timeline", ["case_id"]
    )
    op.create_index(
        "ix_reliability_case_timeline_project_id",
        "reliability_case_timeline",
        ["project_id"],
    )
    op.create_index(
        "ix_reliability_case_timeline_occurred_at",
        "reliability_case_timeline",
        ["occurred_at"],
    )
    op.create_index(
        "ix_reliability_case_timeline_kind", "reliability_case_timeline", ["kind"]
    )
    op.create_index(
        "ix_reliability_case_timeline_case_seq",
        "reliability_case_timeline",
        ["case_id", "sequence"],
    )

    # --------------------------------------------------- §5 state transitions
    op.create_table(
        "component_state_transitions",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "previous_state", _enum("component_operational_state"), nullable=True
        ),
        sa.Column("new_state", _enum("component_operational_state"), nullable=False),
        sa.Column("trigger", _enum("state_transition_trigger"), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(64),
            nullable=False,
            server_default="system_state",
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "case_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_component_state_transitions_project_id",
        "component_state_transitions",
        ["project_id"],
    )
    op.create_index(
        "ix_component_state_transitions_environment_id",
        "component_state_transitions",
        ["environment_id"],
    )
    op.create_index(
        "ix_component_state_transitions_component_id",
        "component_state_transitions",
        ["component_id"],
    )
    op.create_index(
        "ix_component_state_transitions_new_state",
        "component_state_transitions",
        ["new_state"],
    )
    op.create_index(
        "ix_component_state_transitions_occurred_at",
        "component_state_transitions",
        ["occurred_at"],
    )
    op.create_index(
        "ix_component_state_transitions_component_time",
        "component_state_transitions",
        ["component_id", "occurred_at"],
    )
    op.create_index(
        "ix_component_state_transitions_project_time",
        "component_state_transitions",
        ["project_id", "occurred_at"],
    )

    # ------------------------------------------------- §11–§13 workflow engine
    op.create_table(
        "reliability_workflows",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_cases.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "stage",
            _enum("workflow_stage"),
            nullable=False,
            server_default="DETECTED",
        ),
        sa.Column(
            "status",
            _enum("workflow_status"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("completed_stages", postgresql.JSONB(), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column("state", postgresql.JSONB(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", _enum("workflow_stop_reason"), nullable=True),
        sa.Column("stop_detail", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(255), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_reliability_workflows_project_id", "reliability_workflows", ["project_id"]
    )
    op.create_index("ix_reliability_workflows_case_id", "reliability_workflows", ["case_id"])
    op.create_index("ix_reliability_workflows_status", "reliability_workflows", ["status"])
    op.create_index(
        "ix_reliability_workflows_next_run_at", "reliability_workflows", ["next_run_at"]
    )
    op.create_index(
        "ix_reliability_workflows_project_status",
        "reliability_workflows",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_reliability_workflows_case", "reliability_workflows", ["case_id", "created_at"]
    )

    # ------------------------------------------------ §9, §10 event stream
    op.create_table(
        "platform_events",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("event_type", _enum("platform_event_type"), nullable=False),
        sa.Column(
            "case_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(48), nullable=True),
        sa.Column("subject_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.String(64), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_platform_events_dedup_key"),
    )
    op.create_index("ix_platform_events_project_id", "platform_events", ["project_id"])
    op.create_index(
        "ix_platform_events_environment_id", "platform_events", ["environment_id"]
    )
    op.create_index("ix_platform_events_event_type", "platform_events", ["event_type"])
    op.create_index(
        "ix_platform_events_correlation_id", "platform_events", ["correlation_id"]
    )
    op.create_index("ix_platform_events_occurred_at", "platform_events", ["occurred_at"])
    op.create_index(
        "ix_platform_events_project_occurred",
        "platform_events",
        ["project_id", "occurred_at"],
    )
    op.create_index(
        "ix_platform_events_correlation",
        "platform_events",
        ["correlation_id", "occurred_at"],
    )
    op.create_index(
        "ix_platform_events_type_time", "platform_events", ["event_type", "occurred_at"]
    )

    # ------------------------------------------------------ §8 context snapshots
    op.create_table(
        "reliability_context_snapshots",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "case_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_cases.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("incident_id", postgresql.UUID(), nullable=True),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_reliability_context_snapshots_project_id",
        "reliability_context_snapshots",
        ["project_id"],
    )
    op.create_index(
        "ix_reliability_context_snapshots_environment_id",
        "reliability_context_snapshots",
        ["environment_id"],
    )
    op.create_index(
        "ix_reliability_context_snapshots_component_id",
        "reliability_context_snapshots",
        ["component_id"],
    )
    op.create_index(
        "ix_reliability_context_snapshots_fingerprint",
        "reliability_context_snapshots",
        ["fingerprint"],
    )
    op.create_index(
        "ix_reliability_context_snapshots_project_created",
        "reliability_context_snapshots",
        ["project_id", "created_at"],
    )

    # ------------------------------------------------------------ §32, §33 SLOs
    op.create_table(
        "service_level_objectives",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("indicator", _enum("slo_indicator"), nullable=False),
        sa.Column(
            "comparison",
            _enum("slo_comparison"),
            nullable=False,
            server_default="AT_LEAST",
        ),
        sa.Column("metric_name", sa.String(255), nullable=True),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "alert_on_burn", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id", "name", "component_id", name="uq_slo_project_name_component"
        ),
    )
    op.create_index(
        "ix_service_level_objectives_project_id", "service_level_objectives", ["project_id"]
    )
    op.create_index(
        "ix_service_level_objectives_environment_id",
        "service_level_objectives",
        ["environment_id"],
    )
    op.create_index(
        "ix_service_level_objectives_component_id",
        "service_level_objectives",
        ["component_id"],
    )
    op.create_index(
        "ix_slo_project_enabled", "service_level_objectives", ["project_id", "enabled"]
    )

    # --------------------------------------------------------- §34, §35 budgets
    op.create_table(
        "error_budget_snapshots",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "slo_id",
            postgresql.UUID(),
            sa.ForeignKey("service_level_objectives.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", _enum("slo_status"), nullable=False),
        sa.Column("allowed_failure", sa.Float(), nullable=True),
        sa.Column("observed_failure", sa.Float(), nullable=True),
        sa.Column("remaining", sa.Float(), nullable=True),
        sa.Column("remaining_percent", sa.Float(), nullable=True),
        sa.Column("burn_rate", sa.Float(), nullable=True),
        sa.Column(
            "burn_state",
            _enum("burn_rate_state"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("compliance_percent", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_quality", sa.String(24), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_error_budget_snapshots_project_id", "error_budget_snapshots", ["project_id"]
    )
    op.create_index("ix_error_budget_snapshots_slo_id", "error_budget_snapshots", ["slo_id"])
    op.create_index(
        "ix_error_budget_snapshots_computed_at", "error_budget_snapshots", ["computed_at"]
    )
    op.create_index(
        "ix_error_budget_snapshots_slo_time",
        "error_budget_snapshots",
        ["slo_id", "computed_at"],
    )
    op.create_index(
        "ix_error_budget_snapshots_project_time",
        "error_budget_snapshots",
        ["project_id", "computed_at"],
    )

    # ------------------------------------------------------ §54–§56 notifications
    op.create_table(
        "platform_notifications",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", _enum("notification_kind"), nullable=False),
        sa.Column(
            "severity",
            _enum("notification_severity"),
            nullable=False,
            server_default="WARNING",
        ),
        sa.Column(
            "status",
            _enum("notification_status"),
            nullable=False,
            server_default="UNREAD",
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(48), nullable=True),
        sa.Column("subject_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "case_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("dedup_bucket", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("channels_attempted", postgresql.JSONB(), nullable=True),
        sa.Column("delivery", postgresql.JSONB(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.UniqueConstraint(
            "project_id", "fingerprint", "dedup_bucket", name="uq_notifications_dedup"
        ),
    )
    op.create_index(
        "ix_platform_notifications_project_id", "platform_notifications", ["project_id"]
    )
    op.create_index(
        "ix_platform_notifications_project_created",
        "platform_notifications",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_platform_notifications_status",
        "platform_notifications",
        ["status", "severity"],
    )

    # --------------------------------------------------------- §87–§90 quality
    op.create_table(
        "data_quality_issues",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", _enum("data_quality_issue_kind"), nullable=False),
        sa.Column(
            "severity",
            _enum("data_quality_severity"),
            nullable=False,
            server_default="WARNING",
        ),
        sa.Column(
            "status",
            _enum("data_quality_status"),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("subject_type", sa.String(48), nullable=False),
        sa.Column("subject_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "subject_type",
            "subject_id",
            name="uq_data_quality_subject",
        ),
    )
    op.create_index("ix_data_quality_issues_project_id", "data_quality_issues", ["project_id"])
    op.create_index("ix_data_quality_issues_kind", "data_quality_issues", ["kind"])
    op.create_index("ix_data_quality_issues_status", "data_quality_issues", ["status"])
    op.create_index(
        "ix_data_quality_issues_subject_id", "data_quality_issues", ["subject_id"]
    )
    op.create_index(
        "ix_data_quality_issues_detected_at", "data_quality_issues", ["detected_at"]
    )
    op.create_index(
        "ix_data_quality_project_status", "data_quality_issues", ["project_id", "status"]
    )

    # ------------------------------------------------- §91–§94 configuration
    op.create_table(
        "configuration_versions",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", _enum("configuration_scope"), nullable=False),
        sa.Column("scope_id", postgresql.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("redacted_fields", postgresql.JSONB(), nullable=True),
        sa.Column("previous_version", sa.Integer(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("rolled_back_from", sa.Integer(), nullable=True),
        sa.Column("authorizing_actor", sa.String(255), nullable=True),
        sa.UniqueConstraint(
            "project_id", "scope", "scope_id", "version", name="uq_config_version"
        ),
    )
    op.create_index(
        "ix_configuration_versions_project_id", "configuration_versions", ["project_id"]
    )
    op.create_index("ix_configuration_versions_scope", "configuration_versions", ["scope"])
    op.create_index(
        "ix_configuration_versions_scope_lookup",
        "configuration_versions",
        ["project_id", "scope", "scope_id"],
    )


def downgrade() -> None:
    op.drop_column("component_owners", "documentation_url")
    op.drop_column("component_owners", "on_call")
    op.drop_table("configuration_versions")
    op.drop_table("data_quality_issues")
    op.drop_table("platform_notifications")
    op.drop_table("error_budget_snapshots")
    op.drop_table("service_level_objectives")
    op.drop_table("reliability_context_snapshots")
    op.drop_table("platform_events")
    op.drop_table("reliability_workflows")
    op.drop_table("component_state_transitions")
    op.drop_table("reliability_case_timeline")
    op.drop_table("reliability_cases")
    _drop_enum_types()
