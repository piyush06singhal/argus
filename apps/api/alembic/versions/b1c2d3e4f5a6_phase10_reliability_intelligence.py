"""Phase 10: reliability intelligence & autonomous learning tables

Revision ID: b1c2d3e4f5a6
Revises: f7a8b9c0d1e2
Create Date: 2026-09-23 08:00:00.000000

Creates the Phase 10 learning domain:

* ``reliability_experiences``              — one normalised reliability episode
* ``learning_events``                      — append-only outcome inbox (dedup-keyed)
* ``reliability_knowledge``                — learned, scoped, versioned knowledge
* ``intelligence_knowledge_versions``      — immutable per-revision ledger
* ``intelligence_learning_runs``           — auditable pipeline executions
* ``intelligence_learning_experiments``    — inert algorithm evaluations
* ``intelligence_component_profiles``      — historical reliability facts
* ``intelligence_recommendations``         — evidence-backed advisories
* ``intelligence_recommendation_outcomes`` — what actually happened after a decision
* ``intelligence_knowledge_reviews``       — human review decisions
* ``intelligence_event_hooks``             — which events/provenance are learned from

Three properties of this DDL are deliberate:

1. **Every table is derived.** The columns reference Phase 0–9 rows and cascade or
   null with them, so the learning layer can never outlive the history it
   describes. There is no "shadow incident" table here.
2. **A pattern without evidence cannot be inserted.** ``sources`` and
   ``experience_ids`` are ``NOT NULL`` on ``reliability_knowledge``: the empty
   claim is unrepresentable, not merely discouraged.
3. **Sample size is a column, not a comment.** ``sample_count``, ``coverage_start``
   and ``coverage_end`` are non-null/defaulted, so "it worked once" and "34 of 42
   comparable cases" cannot be stored in the same shape (§7, §33).

Enum type names are namespaced ``intelligence_*``. PostgreSQL enum types are
database-global and earlier phases already own generic names (``confidence``,
``provenance``, ``status``), so reuse would silently coerce Phase 10 values. Ten
types are created here and dropped in ``downgrade``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every enum type this migration creates, with its members. Kept as data so
#: creation and teardown can never drift apart.
_NEW_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "intelligence_knowledge_type",
        (
            "INCIDENT_PATTERN",
            "FAILURE_PATTERN",
            "ANOMALY_PATTERN",
            "REMEDIATION_PATTERN",
            "REGRESSION_PATTERN",
            "DEPENDENCY_PATTERN",
            "DEPLOYMENT_PATTERN",
            "RESOURCE_PATTERN",
            "PREDICTIVE_PATTERN",
            "RECOVERY_PATTERN",
            "COMPONENT_RELIABILITY_PATTERN",
        ),
    ),
    (
        "intelligence_knowledge_status",
        (
            "CANDIDATE",
            "VALIDATING",
            "VALIDATED",
            "ACTIVE",
            "DEPRECATED",
            "REJECTED",
            "SUPERSEDED",
        ),
    ),
    (
        "intelligence_knowledge_scope",
        (
            "COMPONENT_SPECIFIC",
            "SERVICE_CLASS",
            "PROJECT_LEVEL",
            "CROSS_PROJECT",
        ),
    ),
    (
        "intelligence_confidence",
        ("UNKNOWN", "LOW", "MEDIUM", "HIGH"),
    ),
    (
        "intelligence_provenance",
        (
            "OBSERVABILITY",
            "SYSTEM_GENERATED",
            "HUMAN_ENTERED",
            "AI_GENERATED",
            "IMPORTED",
            "MOCK",
        ),
    ),
    (
        "intelligence_learning_event_type",
        (
            "INCIDENT_RESOLVED",
            "REMEDIATION_COMPLETED",
            "PATCH_VERIFIED",
            "PATCH_REGRESSION_DETECTED",
            "FORECAST_CONFIRMED",
            "FORECAST_FALSE_POSITIVE",
            "FORECAST_MISSED",
            "ROOT_CAUSE_CONFIRMED",
            "ROOT_CAUSE_REJECTED",
            "REPRODUCTION_CONFIRMED",
            "REPRODUCTION_FAILED",
            "ROLLBACK_COMPLETED",
        ),
    ),
    (
        "intelligence_recommendation_type",
        (
            "INVESTIGATE_COMPONENT",
            "INVESTIGATE_DEPENDENCY",
            "REVIEW_RECENT_CHANGE",
            "REVIEW_REMEDIATION",
            "RUN_REPRODUCTION",
            "CONSIDER_ROLLBACK",
            "CONSIDER_RESTART",
            "CONSIDER_TRAFFIC_SHIFT",
            "REVIEW_CAPACITY",
            "REVIEW_CONFIGURATION",
        ),
    ),
    (
        "intelligence_recommendation_status",
        (
            "OPEN",
            "ACCEPTED",
            "DISMISSED",
            "EXPIRED",
            "EFFECTIVE",
            "INEFFECTIVE",
            "REGRESSION_CAUSING",
        ),
    ),
    (
        "intelligence_run_status",
        ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"),
    ),
    (
        "intelligence_experiment_status",
        ("RUNNING", "PASSED", "FAILED", "REJECTED"),
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    """The column type for one of the enum types above.

    ``create_type=False`` because the types are created once up front; the same
    convention the Phase 6–9 migrations use.
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

    # ------------------------------------------------------------- experiences
    op.create_table(
        "reliability_experiences",
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
            sa.ForeignKey("environments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "primary_component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "remediation_action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Phase 4/5/7 references are plain UUIDs, not FKs: an experience records
        # what happened even if the analysis row itself is later removed, and a
        # circular dependency between learning and analysis is not wanted.
        sa.Column("causal_analysis_id", postgresql.UUID(), nullable=True),
        sa.Column("root_cause_candidate_id", postgresql.UUID(), nullable=True),
        sa.Column("reproduction_id", postgresql.UUID(), nullable=True),
        sa.Column("patch_id", postgresql.UUID(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recovery_seconds", sa.Integer(), nullable=True),
        sa.Column("failure_signature", postgresql.JSONB(), nullable=False),
        sa.Column("failure_fingerprint", sa.String(64), nullable=False),
        sa.Column("resolution_signature", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column(
            "provenance",
            _enum("intelligence_provenance"),
            nullable=False,
            server_default="OBSERVABILITY",
        ),
        sa.Column("data_quality", sa.String(20), nullable=False, server_default="OK"),
        sa.Column("component_ids", postgresql.JSONB(), nullable=False),
        sa.Column("learning_run_id", postgresql.UUID(), nullable=True),
        sa.Column("supersedes_experience_id", postgresql.UUID(), nullable=True),
    )
    for column in (
        "project_id",
        "environment_id",
        "incident_id",
        "primary_component_id",
        "remediation_action_id",
        "causal_analysis_id",
        "root_cause_candidate_id",
        "reproduction_id",
        "patch_id",
        "start_time",
        "provenance",
        "learning_run_id",
    ):
        op.create_index(
            f"ix_reliability_experiences_{column}", "reliability_experiences", [column]
        )
    op.create_index(
        "ix_reliability_experiences_project_time",
        "reliability_experiences",
        ["project_id", "start_time"],
    )
    op.create_index(
        "ix_reliability_experiences_signature",
        "reliability_experiences",
        ["project_id", "failure_fingerprint"],
    )

    # ---------------------------------------------------------- learning events
    op.create_table(
        "learning_events",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            _enum("intelligence_learning_event_type"),
            nullable=False,
        ),
        sa.Column("subject_id", postgresql.UUID(), nullable=False),
        sa.Column("dedup_key", sa.String(120), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "provenance",
            _enum("intelligence_provenance"),
            nullable=False,
            server_default="OBSERVABILITY",
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_by_run_id", postgresql.UUID(), nullable=True),
        sa.Column("unprocessable_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_learning_events_dedup_key"),
    )
    for column in (
        "project_id",
        "event_type",
        "subject_id",
        "occurred_at",
        "processed_at",
    ):
        op.create_index(f"ix_learning_events_{column}", "learning_events", [column])
    op.create_index(
        "ix_learning_events_project_created",
        "learning_events",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_learning_events_unprocessed",
        "learning_events",
        ["processed_at", "project_id"],
    )

    # --------------------------------------------------------------- knowledge
    op.create_table(
        "reliability_knowledge",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_type", _enum("intelligence_knowledge_type"), nullable=False
        ),
        sa.Column(
            "status",
            _enum("intelligence_knowledge_status"),
            nullable=False,
            server_default="CANDIDATE",
        ),
        sa.Column(
            "scope",
            _enum("intelligence_knowledge_scope"),
            nullable=False,
            server_default="COMPONENT_SPECIFIC",
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("feature_signature", sa.String(255), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("experience_ids", postgresql.JSONB(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=True),
        sa.Column("coverage_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "confidence",
            _enum("intelligence_confidence"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("support_strength", sa.Float(), nullable=True),
        sa.Column("algorithm", sa.String(80), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("feature_schema_version", sa.String(40), nullable=False),
        sa.Column("validation", postgresql.JSONB(), nullable=True),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supersedes_knowledge_id", postgresql.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in (
        "project_id",
        "knowledge_type",
        "status",
        "component_id",
        "fingerprint",
        "last_confirmed_at",
    ):
        op.create_index(
            f"ix_reliability_knowledge_{column}", "reliability_knowledge", [column]
        )
    op.create_index(
        "ix_reliability_knowledge_project_status",
        "reliability_knowledge",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_reliability_knowledge_project_type",
        "reliability_knowledge",
        ["project_id", "knowledge_type"],
    )

    # -------------------------------------------------------- knowledge ledger
    op.create_table(
        "intelligence_knowledge_versions",
        _id(),
        *_timestamps(),
        sa.Column(
            "knowledge_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_knowledge.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _enum("intelligence_knowledge_status"), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("confidence", _enum("intelligence_confidence"), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("learning_run_id", postgresql.UUID(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_intelligence_knowledge_versions_knowledge_id",
        "intelligence_knowledge_versions",
        ["knowledge_id"],
    )
    op.create_index(
        "ix_knowledge_versions_knowledge",
        "intelligence_knowledge_versions",
        ["knowledge_id", "version"],
    )

    # ------------------------------------------------------------ learning runs
    op.create_table(
        "intelligence_learning_runs",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "status",
            _enum("intelligence_run_status"),
            nullable=False,
            server_default="QUEUED",
        ),
        sa.Column("trigger", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "experiences_created", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "experiences_updated", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "patterns_discovered", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "patterns_validated", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "patterns_rejected", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "knowledge_activated", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("records_flagged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("algorithm_versions", postgresql.JSONB(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )
    for column in ("project_id", "status"):
        op.create_index(
            f"ix_intelligence_learning_runs_{column}",
            "intelligence_learning_runs",
            [column],
        )
    op.create_index(
        "ix_learning_runs_project_started",
        "intelligence_learning_runs",
        ["project_id", "started_at"],
    )

    # ------------------------------------------------------------- experiments
    op.create_table(
        "intelligence_learning_experiments",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("algorithm", sa.String(80), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column(
            "status",
            _enum("intelligence_experiment_status"),
            nullable=False,
            server_default="RUNNING",
        ),
        sa.Column("dataset_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_schema", postgresql.JSONB(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=True),
    )
    for column in ("project_id", "status"):
        op.create_index(
            f"ix_intelligence_learning_experiments_{column}",
            "intelligence_learning_experiments",
            [column],
        )

    # ------------------------------------------------------ component profiles
    op.create_table(
        "intelligence_component_profiles",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("incident_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("anomaly_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "remediation_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("rollback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("regression_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mean_recovery_seconds", sa.Float(), nullable=True),
        sa.Column(
            "forecast_outcome_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "forecast_true_positive_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "chronic_signal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("chronic_reasons", postgresql.JSONB(), nullable=True),
        sa.Column("breakdown", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "component_id",
            "window_days",
            name="uq_component_profiles_scope",
        ),
    )
    op.create_index(
        "ix_intelligence_component_profiles_component_id",
        "intelligence_component_profiles",
        ["component_id"],
    )
    op.create_index(
        "ix_component_profiles_project",
        "intelligence_component_profiles",
        ["project_id", "window_days"],
    )

    # --------------------------------------------------------- recommendations
    op.create_table(
        "intelligence_recommendations",
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
            sa.ForeignKey("environments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("forecast_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "recommendation_type",
            _enum("intelligence_recommendation_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("intelligence_recommendation_status"),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("knowledge_ids", postgresql.JSONB(), nullable=False),
        sa.Column("experience_ids", postgresql.JSONB(), nullable=False),
        sa.Column("current_evidence", postgresql.JSONB(), nullable=False),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column("ranking", postgresql.JSONB(), nullable=True),
        sa.Column("policy_note", sa.Text(), nullable=True),
        sa.Column("historical", postgresql.JSONB(), nullable=True),
        sa.Column(
            "confidence",
            _enum("intelligence_confidence"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("decision", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=True),
    )
    for column in ("project_id", "status", "expires_at", "fingerprint"):
        op.create_index(
            f"ix_intelligence_recommendations_{column}",
            "intelligence_recommendations",
            [column],
        )
    op.create_index(
        "ix_recommendations_project_status",
        "intelligence_recommendations",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_recommendations_project_component",
        "intelligence_recommendations",
        ["project_id", "component_id"],
    )

    # ------------------------------------------------- recommendation outcomes
    op.create_table(
        "intelligence_recommendation_outcomes",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recommendation_id",
            postgresql.UUID(),
            sa.ForeignKey("intelligence_recommendations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(30), nullable=False),
        sa.Column("remediation_action_id", postgresql.UUID(), nullable=True),
        sa.Column("incident_id", postgresql.UUID(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_intelligence_recommendation_outcomes_project_id",
        "intelligence_recommendation_outcomes",
        ["project_id"],
    )
    op.create_index(
        "ix_intelligence_recommendation_outcomes_recommendation_id",
        "intelligence_recommendation_outcomes",
        ["recommendation_id"],
    )
    op.create_index(
        "ix_recommendation_outcomes_project",
        "intelligence_recommendation_outcomes",
        ["project_id", "recorded_at"],
    )

    # -------------------------------------------------------- knowledge review
    op.create_table(
        "intelligence_knowledge_reviews",
        _id(),
        *_timestamps(),
        sa.Column(
            "knowledge_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_knowledge.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("reviewer", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("knowledge_version", sa.Integer(), nullable=False),
    )
    for column in ("knowledge_id", "project_id"):
        op.create_index(
            f"ix_intelligence_knowledge_reviews_{column}",
            "intelligence_knowledge_reviews",
            [column],
        )
    op.create_index(
        "ix_knowledge_reviews_knowledge",
        "intelligence_knowledge_reviews",
        ["knowledge_id", "created_at"],
    )

    # ------------------------------------------------------------ event hooks
    op.create_table(
        "intelligence_event_hooks",
        _id(),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column("enabled_event_types", postgresql.JSONB(), nullable=False),
        sa.Column("trusted_provenance", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("intelligence_event_hooks")
    op.drop_table("intelligence_knowledge_reviews")
    op.drop_table("intelligence_recommendation_outcomes")
    op.drop_table("intelligence_recommendations")
    op.drop_table("intelligence_component_profiles")
    op.drop_table("intelligence_learning_experiments")
    op.drop_table("intelligence_learning_runs")
    op.drop_table("intelligence_knowledge_versions")
    op.drop_table("reliability_knowledge")
    op.drop_table("learning_events")
    op.drop_table("reliability_experiences")
    _drop_enum_types()
