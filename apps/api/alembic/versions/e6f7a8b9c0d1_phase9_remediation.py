"""Phase 9: safe autonomous remediation tables

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-22 06:00:00.000000

Creates the Phase 9 remediation domain:

* ``remediation_policies``          — the per-scope contract: regime, ceilings, guard rails
* ``remediation_proposals``         — candidate remediations with their evidence provenance
* ``remediation_actions``           — the core gated action (status + three gate verdicts)
* ``remediation_assessments``       — safety assessments, append-only
* ``remediation_policy_decisions``  — policy evaluations, with the rules that fired
* ``remediation_approvals``         — human and policy authorizations
* ``remediation_executions``        — one row per attempt, with ``effect_applied``
* ``remediation_verifications``     — did the system actually behave as expected?
* ``remediation_rollbacks``         — reversal attempts, themselves verified
* ``remediation_audit_events``      — hash-chained audit trail
* ``remediation_circuit_breakers``  — per-scope failure breakers
* ``remediation_controls``          — the ARGUS-native control plane that actions change

Two deliberate properties of this DDL:

1. **Nothing here holds a command.** There is no column that a shell string could
   be written to. Action parameters live in JSON and are validated against the
   code-defined registry before any handler sees them, so the schema cannot be
   used to smuggle execution parameters past validation.
2. **Irreversibility is a column, not a convention.** ``rollback_strategy`` is
   ``NOT NULL`` on both proposals and actions, so an action whose reversal was
   never considered cannot be inserted at all.

Enum type names are namespaced ``remediation_*``: PostgreSQL enum types are
database-global and earlier phases already own ``risklevel``, ``risksignaltype``
and ``remediation``-free names. Twenty-five types are created here and dropped in
``downgrade``.

Every foreign key to a Phase 0–8 row is ``ON DELETE CASCADE`` (project/environment
scope) or ``ON DELETE SET NULL`` (component, incident, patch and other referenced
evidence), so removing a component never erases the record of what ARGUS did to
it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every enum type this migration creates, with its members. Kept as data so
#: creation and teardown can never drift apart.
_NEW_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "remediation_action_type",
        (
            "RESTART_SERVICE",
            "RESTART_INSTANCE",
            "DISABLE_FEATURE_FLAG",
            "ENABLE_FEATURE_FLAG",
            "ROLLBACK_DEPLOYMENT",
            "ROLLBACK_CONFIGURATION",
            "SCALE_SERVICE_WITHIN_LIMIT",
            "DISABLE_DEGRADED_DEPENDENCY",
            "ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE",
            "APPLY_VERIFIED_PATCH",
            "PAUSE_BACKGROUND_JOB",
            "RESUME_BACKGROUND_JOB",
        ),
    ),
    (
        "remediation_source_type",
        (
            "INCIDENT",
            "ANOMALY",
            "ROOT_CAUSE_ANALYSIS",
            "FAILURE_REPRODUCTION",
            "DEBUG_ANALYSIS",
            "VERIFIED_PATCH",
            "RELIABILITY_FORECAST",
            "KNOWN_RECOVERY_PATTERN",
            "HUMAN_OPERATOR",
        ),
    ),
    (
        "remediation_status",
        (
            "PROPOSED",
            "VALIDATING",
            "POLICY_REVIEW",
            "AWAITING_APPROVAL",
            "AUTHORIZED",
            "SCHEDULED",
            "EXECUTING",
            "VERIFYING",
            "VERIFIED",
            "FAILED",
            "ROLLING_BACK",
            "ROLLED_BACK",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
            "BLOCKED",
        ),
    ),
    ("remediation_risk_level", ("LOW", "MEDIUM", "HIGH", "CRITICAL")),
    (
        "remediation_execution_mode",
        (
            "OBSERVE_ONLY",
            "DRY_RUN",
            "SHADOW",
            "HUMAN_APPROVAL",
            "AUTONOMOUS",
            "EMERGENCY_STOP",
        ),
    ),
    (
        "remediation_policy_decision",
        ("ALLOW", "ALLOW_WITH_CANARY", "REQUIRE_APPROVAL", "DENY"),
    ),
    (
        "remediation_approval_status",
        ("PENDING", "APPROVED", "REJECTED", "EXPIRED", "WITHDRAWN"),
    ),
    (
        "remediation_actor_type",
        ("HUMAN", "AUTONOMOUS_POLICY", "AI_PROPOSAL", "SYSTEM"),
    ),
    ("remediation_safety_status", ("PASSED", "PASSED_WITH_WARNINGS", "FAILED")),
    (
        "remediation_execution_status",
        ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "REFUSED", "NOT_PERFORMED"),
    ),
    ("remediation_adapter_kind", ("CONTROL_PLANE", "WORKSPACE", "EXTERNAL")),
    (
        "remediation_verification_verdict",
        ("VERIFIED", "PARTIALLY_VERIFIED", "FAILED", "INCONCLUSIVE", "NOT_EXECUTED"),
    ),
    (
        "remediation_rollback_strategy",
        (
            "NONE",
            "INVERSE_ACTION",
            "RESTORE_PREVIOUS_STATE",
            "REVERT_WORKSPACE",
            "MANUAL",
        ),
    ),
    (
        "remediation_rollback_trigger",
        (
            "VERIFICATION_FAILED",
            "VERIFICATION_INCONCLUSIVE",
            "EXECUTION_FAILED",
            "HUMAN_REQUEST",
            "POLICY_REQUIRED",
            "EMERGENCY_STOP",
        ),
    ),
    (
        "remediation_rollback_status",
        ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "NOT_AVAILABLE"),
    ),
    (
        "remediation_blast_radius",
        (
            "SINGLE_INSTANCE",
            "SINGLE_COMPONENT",
            "LIMITED_PERCENTAGE",
            "ENVIRONMENT",
        ),
    ),
    (
        "remediation_outcome",
        (
            "EFFECTIVE",
            "PARTIALLY_EFFECTIVE",
            "INEFFECTIVE",
            "HARMFUL",
            "INCONCLUSIVE",
        ),
    ),
    (
        "remediation_failure_reason",
        (
            "EXECUTION_DISABLED",
            "ADAPTER_UNAVAILABLE",
            "ENVIRONMENT_NOT_ALLOWED",
            "PRECONDITION_FAILED",
            "IRREVERSIBLE_RESTRICTED",
            "POLICY_DENIED",
            "EMERGENCY_STOP",
            "APPROVAL_REQUIRED",
            "APPROVAL_EXPIRED",
            "CIRCUIT_OPEN",
            "BUDGET_EXHAUSTED",
            "CONCURRENCY_LIMIT",
            "STALE_ACTION",
            "NOT_ACTIONABLE",
            "HANDLER_ERROR",
            "TIMEOUT",
            "PARAMETER_INVALID",
        ),
    ),
    ("remediation_circuit_state", ("CLOSED", "OPEN", "HALF_OPEN")),
    ("remediation_canary_stage", ("NONE", "CANARY", "EXPANDING", "COMPLETE")),
    (
        "remediation_control_kind",
        ("FEATURE_FLAG", "BACKGROUND_JOB", "DEPENDENCY_SUPPRESSION"),
    ),
    (
        "remediation_control_state",
        ("ENABLED", "DISABLED", "PAUSED", "RESUMED", "SUPPRESSED"),
    ),
    (
        "remediation_audit_event_type",
        (
            "ACTION_PROPOSED",
            "ACTION_VALIDATED",
            "VALIDATION_FAILED",
            "SAFETY_ASSESSED",
            "POLICY_EVALUATED",
            "POLICY_DENIED",
            "APPROVAL_REQUESTED",
            "APPROVED",
            "REJECTED",
            "AUTHORIZED",
            "EXECUTION_STARTED",
            "EXECUTION_REFUSED",
            "EXECUTION_SUCCEEDED",
            "EXECUTION_FAILED",
            "EXECUTION_RECORDED_MANUALLY",
            "VERIFICATION_STARTED",
            "VERIFICATION_COMPLETED",
            "ROLLBACK_STARTED",
            "ROLLBACK_COMPLETED",
            "ROLLBACK_FAILED",
            "POST_ANALYSIS_COMPLETED",
            "ACTION_CANCELLED",
            "ACTION_EXPIRED",
            "ACTION_BLOCKED",
            "CIRCUIT_OPENED",
            "CIRCUIT_CLOSED",
            "EMERGENCY_STOP_ENGAGED",
            "EMERGENCY_STOP_RELEASED",
            "POLICY_UPDATED",
            "CONTROL_APPLIED",
            "CONTROL_REVERTED",
        ),
    ),
    (
        "remediation_post_analysis_status",
        ("PENDING", "COMPLETED", "INSUFFICIENT_EVIDENCE", "FAILED"),
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    """The column type for one of the enum types above.

    ``create_type=False`` because the types are created once up front; the same
    convention the Phase 6/7/8 migrations use.
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

    # ---------------------------------------------------------------- policies
    op.create_table(
        "remediation_policies",
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
        sa.Column("name", sa.String(120), nullable=False, server_default="default"),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "execution_mode",
            _enum("remediation_execution_mode"),
            nullable=False,
            server_default="HUMAN_APPROVAL",
        ),
        sa.Column(
            "autonomous_max_risk",
            _enum("remediation_risk_level"),
            nullable=False,
            server_default="LOW",
        ),
        sa.Column("allowed_action_types", postgresql.JSONB(), nullable=True),
        sa.Column("allowed_environment_names", postgresql.JSONB(), nullable=True),
        sa.Column(
            "max_actions_per_window",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "action_window_seconds",
            sa.Integer(),
            nullable=False,
            server_default="3600",
        ),
        sa.Column(
            "cooldown_seconds", sa.Integer(), nullable=False, server_default="60"
        ),
        sa.Column(
            "max_concurrent_actions",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "max_blast_radius_percent",
            sa.Float(),
            nullable=False,
            server_default="10",
        ),
        sa.Column(
            "circuit_failure_threshold",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
        sa.Column(
            "circuit_reset_seconds", sa.Integer(), nullable=False, server_default="900"
        ),
        sa.Column(
            "canary_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("canary_percent", sa.Float(), nullable=False, server_default="10"),
        sa.Column(
            "approval_ttl_seconds",
            sa.Integer(),
            nullable=False,
            server_default="1800",
        ),
        sa.Column(
            "verification_window_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column(
            "verification_grace_seconds",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "max_verification_attempts",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column(
            "execution_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
        sa.Column(
            "max_execution_attempts",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column(
            "action_expiry_seconds",
            sa.Integer(),
            nullable=False,
            server_default="86400",
        ),
        sa.Column(
            "emergency_stop_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("emergency_stop_reason", sa.Text(), nullable=True),
        sa.Column("emergency_stop_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emergency_stop_by", sa.String(255), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("project_id", "ix_remediation_policies_project_id"),
        ("environment_id", "ix_remediation_policies_environment_id"),
        ("execution_mode", "ix_remediation_policies_execution_mode"),
        ("emergency_stop_active", "ix_remediation_policies_emergency_stop_active"),
    ):
        op.create_index(name, "remediation_policies", [column])
    op.create_index(
        "ix_remediation_policies_scope",
        "remediation_policies",
        ["project_id", "environment_id"],
    )

    # --------------------------------------------------------------- proposals
    op.create_table(
        "remediation_proposals",
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
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "action_type", _enum("remediation_action_type"), nullable=False
        ),
        sa.Column("source_type", _enum("remediation_source_type"), nullable=False),
        sa.Column("source_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "causal_analysis_id",
            postgresql.UUID(),
            sa.ForeignKey("causal_analyses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "root_cause_candidate_id",
            postgresql.UUID(),
            sa.ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reproduction_experiment_id",
            postgresql.UUID(),
            sa.ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "fix_hypothesis_id",
            postgresql.UUID(),
            sa.ForeignKey("fix_hypotheses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("expected_effect", sa.Text(), nullable=False),
        sa.Column("supporting_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("risk_level", _enum("remediation_risk_level"), nullable=False),
        sa.Column("blast_radius", _enum("remediation_blast_radius"), nullable=False),
        sa.Column("blast_radius_percent", sa.Float(), nullable=True),
        sa.Column("preconditions", postgresql.JSONB(), nullable=True),
        sa.Column("verification_plan", postgresql.JSONB(), nullable=True),
        sa.Column("rollback_plan", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confidence_reason", sa.Text(), nullable=True),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("strategy", sa.String(80), nullable=True),
        sa.Column(
            "generated_by",
            _enum("remediation_actor_type"),
            nullable=False,
            server_default="SYSTEM",
        ),
        sa.Column("model_version", sa.String(120), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("project_id", "ix_remediation_proposals_project_id"),
        ("environment_id", "ix_remediation_proposals_environment_id"),
        ("component_id", "ix_remediation_proposals_component_id"),
        ("action_type", "ix_remediation_proposals_action_type"),
        ("source_type", "ix_remediation_proposals_source_type"),
        ("source_id", "ix_remediation_proposals_source_id"),
        ("incident_id", "ix_remediation_proposals_incident_id"),
        ("forecast_id", "ix_remediation_proposals_forecast_id"),
        ("patch_id", "ix_remediation_proposals_patch_id"),
        ("risk_level", "ix_remediation_proposals_risk_level"),
        ("fingerprint", "ix_remediation_proposals_fingerprint"),
    ):
        op.create_index(name, "remediation_proposals", [column])
    op.create_index(
        "ix_remediation_proposals_scope_created",
        "remediation_proposals",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_remediation_proposals_source",
        "remediation_proposals",
        ["source_type", "source_id"],
    )

    # ----------------------------------------------------------------- actions
    op.create_table(
        "remediation_actions",
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
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "proposal_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "action_type", _enum("remediation_action_type"), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("risk_level", _enum("remediation_risk_level"), nullable=False),
        sa.Column("blast_radius", _enum("remediation_blast_radius"), nullable=False),
        sa.Column("blast_radius_percent", sa.Float(), nullable=True),
        sa.Column(
            "affected_resource_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("source_type", _enum("remediation_source_type"), nullable=False),
        sa.Column("source_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "causal_analysis_id",
            postgresql.UUID(),
            sa.ForeignKey("causal_analyses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "root_cause_analysis_id",
            postgresql.UUID(),
            sa.ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "fix_id",
            postgresql.UUID(),
            sa.ForeignKey("fix_hypotheses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("safety_status", _enum("remediation_safety_status"), nullable=True),
        sa.Column(
            "policy_status", _enum("remediation_policy_decision"), nullable=True
        ),
        sa.Column(
            "authorization_status",
            _enum("remediation_approval_status"),
            nullable=True,
        ),
        sa.Column(
            "execution_status", _enum("remediation_execution_status"), nullable=True
        ),
        sa.Column(
            "status",
            _enum("remediation_status"),
            nullable=False,
            server_default="PROPOSED",
        ),
        sa.Column(
            "execution_mode",
            _enum("remediation_execution_mode"),
            nullable=False,
            server_default="HUMAN_APPROVAL",
        ),
        sa.Column("adapter_kind", _enum("remediation_adapter_kind"), nullable=False),
        sa.Column(
            "rollback_strategy",
            _enum("remediation_rollback_strategy"),
            nullable=False,
        ),
        sa.Column(
            "rollback_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "rollback_action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rollback_plan", postgresql.JSONB(), nullable=True),
        sa.Column("verification_plan", postgresql.JSONB(), nullable=True),
        sa.Column("preconditions", postgresql.JSONB(), nullable=True),
        sa.Column(
            "canary_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "canary_stage",
            _enum("remediation_canary_stage"),
            nullable=False,
            server_default="NONE",
        ),
        sa.Column("canary_percent", sa.Float(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "failure_reason", _enum("remediation_failure_reason"), nullable=True
        ),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("outcome", _enum("remediation_outcome"), nullable=True),
        sa.Column(
            "post_analysis_status",
            _enum("remediation_post_analysis_status"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("post_analysis", postgresql.JSONB(), nullable=True),
        sa.Column("headline", sa.String(400), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_performed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.String(255),
            nullable=False,
            server_default="system",
        ),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("authorized_by", sa.String(255), nullable=True),
        sa.Column("executed_by", sa.String(255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "fingerprint",
            "attempt",
            name="uq_remediation_actions_project_fingerprint_attempt",
        ),
    )
    for column, name in (
        ("project_id", "ix_remediation_actions_project_id"),
        ("environment_id", "ix_remediation_actions_environment_id"),
        ("component_id", "ix_remediation_actions_component_id"),
        ("proposal_id", "ix_remediation_actions_proposal_id"),
        ("action_type", "ix_remediation_actions_action_type"),
        ("risk_level", "ix_remediation_actions_risk_level"),
        ("source_id", "ix_remediation_actions_source_id"),
        ("incident_id", "ix_remediation_actions_incident_id"),
        ("forecast_id", "ix_remediation_actions_forecast_id"),
        ("patch_id", "ix_remediation_actions_patch_id"),
        ("status", "ix_remediation_actions_status"),
        ("rollback_available", "ix_remediation_actions_rollback_available"),
        ("fingerprint", "ix_remediation_actions_fingerprint"),
        ("failure_reason", "ix_remediation_actions_failure_reason"),
        ("expires_at", "ix_remediation_actions_expires_at"),
    ):
        op.create_index(name, "remediation_actions", [column])
    op.create_index(
        "ix_remediation_actions_scope_status",
        "remediation_actions",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_remediation_actions_scope_type",
        "remediation_actions",
        ["project_id", "action_type", "created_at"],
    )
    op.create_index(
        "ix_remediation_actions_active",
        "remediation_actions",
        ["project_id", "environment_id", "status"],
    )

    # ------------------------------------------------------------- assessments
    op.create_table(
        "remediation_assessments",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", _enum("remediation_safety_status"), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column("blocking", postgresql.JSONB(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(), nullable=True),
        sa.Column(
            "reversible", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("rollback_plan", postgresql.JSONB(), nullable=True),
        sa.Column("blast_radius", _enum("remediation_blast_radius"), nullable=False),
        sa.Column("blast_radius_percent", sa.Float(), nullable=True),
        sa.Column(
            "affected_resource_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "requires_human_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "assessed_by",
            sa.String(255),
            nullable=False,
            server_default="engine",
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("action_id", "ix_remediation_assessments_action_id"),
        ("project_id", "ix_remediation_assessments_project_id"),
        ("status", "ix_remediation_assessments_status"),
        (
            "requires_human_approval",
            "ix_remediation_assessments_requires_human_approval",
        ),
    ):
        op.create_index(name, "remediation_assessments", [column])
    op.create_index(
        "ix_remediation_assessments_action_created",
        "remediation_assessments",
        ["action_id", "created_at"],
    )

    # ------------------------------------------------------- policy decisions
    op.create_table(
        "remediation_policy_decisions",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.Column("decision", _enum("remediation_policy_decision"), nullable=False),
        sa.Column(
            "execution_mode", _enum("remediation_execution_mode"), nullable=False
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("matched_rules", postgresql.JSONB(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), nullable=True),
        sa.Column(
            "failure_reason", _enum("remediation_failure_reason"), nullable=True
        ),
        sa.Column(
            "requires_canary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("budget_state", postgresql.JSONB(), nullable=True),
        sa.Column("circuit_state", postgresql.JSONB(), nullable=True),
        sa.Column(
            "evaluated_by",
            sa.String(255),
            nullable=False,
            server_default="policy-engine",
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("action_id", "ix_remediation_policy_decisions_action_id"),
        ("policy_id", "ix_remediation_policy_decisions_policy_id"),
        ("project_id", "ix_remediation_policy_decisions_project_id"),
        ("decision", "ix_remediation_policy_decisions_decision"),
    ):
        op.create_index(name, "remediation_policy_decisions", [column])
    op.create_index(
        "ix_remediation_policy_decisions_action_created",
        "remediation_policy_decisions",
        ["action_id", "created_at"],
    )
    op.create_index(
        "ix_remediation_policy_decisions_project_decision",
        "remediation_policy_decisions",
        ["project_id", "decision"],
    )

    # -------------------------------------------------------------- approvals
    op.create_table(
        "remediation_approvals",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("remediation_approval_status"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("actor_type", _enum("remediation_actor_type"), nullable=False),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("scope_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("action_id", "ix_remediation_approvals_action_id"),
        ("project_id", "ix_remediation_approvals_project_id"),
        ("status", "ix_remediation_approvals_status"),
        ("expires_at", "ix_remediation_approvals_expires_at"),
    ):
        op.create_index(name, "remediation_approvals", [column])
    op.create_index(
        "ix_remediation_approvals_action_status",
        "remediation_approvals",
        ["action_id", "status"],
    )
    op.create_index(
        "ix_remediation_approvals_project_created",
        "remediation_approvals",
        ["project_id", "created_at"],
    )

    # ------------------------------------------------------------- executions
    op.create_table(
        "remediation_executions",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "action_type", _enum("remediation_action_type"), nullable=False
        ),
        sa.Column("mode", _enum("remediation_execution_mode"), nullable=False),
        sa.Column("adapter_kind", _enum("remediation_adapter_kind"), nullable=False),
        sa.Column("adapter_name", sa.String(120), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status",
            _enum("remediation_execution_status"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "effect_applied",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("steps", postgresql.JSONB(), nullable=True),
        sa.Column("control_ids", postgresql.JSONB(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column(
            "failure_reason", _enum("remediation_failure_reason"), nullable=True
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "executed_by",
            sa.String(255),
            nullable=False,
            server_default="executor",
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "action_id", "attempt", name="uq_remediation_executions_action_attempt"
        ),
    )
    for column, name in (
        ("action_id", "ix_remediation_executions_action_id"),
        ("project_id", "ix_remediation_executions_project_id"),
        ("component_id", "ix_remediation_executions_component_id"),
        ("action_type", "ix_remediation_executions_action_type"),
        ("mode", "ix_remediation_executions_mode"),
        ("status", "ix_remediation_executions_status"),
        ("effect_applied", "ix_remediation_executions_effect_applied"),
        ("failure_reason", "ix_remediation_executions_failure_reason"),
        ("idempotency_key", "ix_remediation_executions_idempotency_key"),
    ):
        op.create_index(name, "remediation_executions", [column])
    op.create_index(
        "ix_remediation_executions_project_status",
        "remediation_executions",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_remediation_executions_scope_type",
        "remediation_executions",
        ["project_id", "action_type", "created_at"],
    )

    # ----------------------------------------------------------- verifications
    op.create_table(
        "remediation_verifications",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("verdict", _enum("remediation_verification_verdict"), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column("passed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "not_observable_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "observation_seconds", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column(
            "verified_by",
            sa.String(255),
            nullable=False,
            server_default="verifier",
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("action_id", "ix_remediation_verifications_action_id"),
        ("execution_id", "ix_remediation_verifications_execution_id"),
        ("project_id", "ix_remediation_verifications_project_id"),
        ("component_id", "ix_remediation_verifications_component_id"),
        ("verdict", "ix_remediation_verifications_verdict"),
    ):
        op.create_index(name, "remediation_verifications", [column])
    op.create_index(
        "ix_remediation_verifications_action_created",
        "remediation_verifications",
        ["action_id", "created_at"],
    )
    op.create_index(
        "ix_remediation_verifications_project_verdict",
        "remediation_verifications",
        ["project_id", "verdict"],
    )

    # -------------------------------------------------------------- rollbacks
    op.create_table(
        "remediation_rollbacks",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", _enum("remediation_rollback_trigger"), nullable=False),
        sa.Column(
            "strategy", _enum("remediation_rollback_strategy"), nullable=False
        ),
        sa.Column(
            "status",
            _enum("remediation_rollback_status"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("plan", postgresql.JSONB(), nullable=True),
        sa.Column("steps", postgresql.JSONB(), nullable=True),
        sa.Column(
            "inverse_action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("controls_reverted", postgresql.JSONB(), nullable=True),
        sa.Column(
            "verification_verdict",
            _enum("remediation_verification_verdict"),
            nullable=True,
        ),
        sa.Column(
            "failure_reason", _enum("remediation_failure_reason"), nullable=True
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("action_id", "ix_remediation_rollbacks_action_id"),
        ("project_id", "ix_remediation_rollbacks_project_id"),
        ("trigger", "ix_remediation_rollbacks_trigger"),
        ("status", "ix_remediation_rollbacks_status"),
    ):
        op.create_index(name, "remediation_rollbacks", [column])
    op.create_index(
        "ix_remediation_rollbacks_action_created",
        "remediation_rollbacks",
        ["action_id", "created_at"],
    )

    # ------------------------------------------------------------- audit trail
    op.create_table(
        "remediation_audit_events",
        _id(),
        *_timestamps(),
        sa.Column(
            "action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=True,
        ),
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
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "event_type", _enum("remediation_audit_event_type"), nullable=False
        ),
        sa.Column("actor_type", _enum("remediation_actor_type"), nullable=False),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("from_status", _enum("remediation_status"), nullable=True),
        sa.Column("to_status", _enum("remediation_status"), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "action_id",
            "sequence",
            name="uq_remediation_audit_events_action_sequence",
        ),
    )
    for column, name in (
        ("action_id", "ix_remediation_audit_events_action_id"),
        ("project_id", "ix_remediation_audit_events_project_id"),
        ("event_type", "ix_remediation_audit_events_event_type"),
        ("occurred_at", "ix_remediation_audit_events_occurred_at"),
        ("entry_hash", "ix_remediation_audit_events_entry_hash"),
    ):
        op.create_index(name, "remediation_audit_events", [column])
    op.create_index(
        "ix_remediation_audit_events_action_created",
        "remediation_audit_events",
        ["action_id", "created_at"],
    )
    op.create_index(
        "ix_remediation_audit_events_project_type",
        "remediation_audit_events",
        ["project_id", "event_type", "created_at"],
    )

    # ------------------------------------------------------- circuit breakers
    op.create_table(
        "remediation_circuit_breakers",
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
            "action_type", _enum("remediation_action_type"), nullable=False
        ),
        sa.Column(
            "state",
            _enum("remediation_circuit_state"),
            nullable=False,
            server_default="CLOSED",
        ),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("total_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_trip_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("project_id", "ix_remediation_circuit_breakers_project_id"),
        ("environment_id", "ix_remediation_circuit_breakers_environment_id"),
        ("action_type", "ix_remediation_circuit_breakers_action_type"),
        ("state", "ix_remediation_circuit_breakers_state"),
        ("opened_until", "ix_remediation_circuit_breakers_opened_until"),
    ):
        op.create_index(name, "remediation_circuit_breakers", [column])
    op.create_index(
        "ix_remediation_circuit_breakers_scope_type",
        "remediation_circuit_breakers",
        ["project_id", "environment_id", "action_type"],
    )

    # ---------------------------------------------------------- control plane
    op.create_table(
        "remediation_controls",
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
        sa.Column(
            "component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", _enum("remediation_control_kind"), nullable=False),
        sa.Column("scope_key", sa.String(120), nullable=False),
        sa.Column("state", _enum("remediation_control_state"), nullable=False),
        sa.Column(
            "previous_state", _enum("remediation_control_state"), nullable=True
        ),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "applied_by_action_id",
            postgresql.UUID(),
            sa.ForeignKey("remediation_actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "applied_by",
            sa.String(255),
            nullable=False,
            server_default="system",
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column, name in (
        ("project_id", "ix_remediation_controls_project_id"),
        ("environment_id", "ix_remediation_controls_environment_id"),
        ("component_id", "ix_remediation_controls_component_id"),
        ("kind", "ix_remediation_controls_kind"),
        ("scope_key", "ix_remediation_controls_scope_key"),
        ("state", "ix_remediation_controls_state"),
        ("is_current", "ix_remediation_controls_is_current"),
        ("applied_by_action_id", "ix_remediation_controls_applied_by_action_id"),
        ("expires_at", "ix_remediation_controls_expires_at"),
    ):
        op.create_index(name, "remediation_controls", [column])
    op.create_index(
        "ix_remediation_controls_scope_key_kind",
        "remediation_controls",
        ["project_id", "kind", "scope_key"],
    )
    op.create_index(
        "ix_remediation_controls_lookup",
        "remediation_controls",
        ["kind", "scope_key", "is_current"],
    )


def downgrade() -> None:
    op.drop_table("remediation_controls")
    op.drop_table("remediation_circuit_breakers")
    op.drop_table("remediation_audit_events")
    op.drop_table("remediation_rollbacks")
    op.drop_table("remediation_verifications")
    op.drop_table("remediation_executions")
    op.drop_table("remediation_approvals")
    op.drop_table("remediation_policy_decisions")
    op.drop_table("remediation_assessments")
    op.drop_table("remediation_actions")
    op.drop_table("remediation_proposals")
    op.drop_table("remediation_policies")
    _drop_enum_types()
