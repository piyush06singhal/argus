"""Phase 7: fix generation & verification tables

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-21 01:00:00.000000

Creates the Phase 7 domain:

* ``fix_hypotheses``          — evidence-bound fix ideas with an explicit scope allowlist
* ``patches``                 — validated unified diffs with generation provenance
* ``patch_workspaces``        — one isolated git worktree per candidate
* ``patch_verification_runs`` — one verification attempt, one stored verdict
* ``patch_test_runs``         — allowlisted commands executed inside a workspace
* ``patch_regression_tests``  — the two-sided proof (fails on base, passes patched)
* ``patch_comparisons``       — BASE vs PATCHED from real experiment telemetry
* ``patch_risk_assessments``  — deterministic risk with an explicit explanation
* ``patch_review_actions``    — human decisions (the only path to APPROVED)
* ``patch_artifacts``         — hashed, immutable-after-verification files

Every FK to a Phase 0–6 row is ON DELETE CASCADE or SET NULL: fixes are
derived evidence about a project's incident and must never outlive it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every enum type this migration creates, with its members. Kept as data so
#: creation and teardown can never drift apart — a downgrade that drops fewer
#: types than the upgrade created leaves the next upgrade unable to run.
_NEW_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "fixcategory",
        (
            "BUG_FIX",
            "ERROR_HANDLING",
            "TIMEOUT_FIX",
            "RETRY_FIX",
            "VALIDATION_FIX",
            "RESOURCE_HANDLING",
            "CONCURRENCY_FIX",
            "DATABASE_QUERY_FIX",
            "API_CONTRACT_FIX",
            "CONFIGURATION_FIX",
            "DEPENDENCY_HANDLING",
            "PERFORMANCE_FIX",
            "UNKNOWN",
        ),
    ),
    (
        "fixstatus",
        (
            "DRAFT",
            "HYPOTHESIZED",
            "PATCHING",
            "PATCH_GENERATED",
            "GENERATION_FAILED",
            "REJECTED",
            "SUPERSEDED",
        ),
    ),
    (
        "patchstatus",
        (
            "GENERATED",
            "PARSE_FAILED",
            "VALIDATION_FAILED",
            "APPLIED",
            "BUILD_FAILED",
            "TEST_FAILED",
            "REPRODUCTION_FAILED",
            "VERIFIED",
            "REJECTED",
            "SUPERSEDED",
            "GENERATION_FAILED",
        ),
    ),
    ("patchformat", ("UNIFIED_DIFF",)),
    ("risklevel", ("LOW", "MEDIUM", "HIGH", "CRITICAL")),
    (
        "verificationlevel",
        (
            "NONE",
            "STATIC_VALIDATED",
            "TEST_VALIDATED",
            "REPRODUCTION_VALIDATED",
            "REGRESSION_VALIDATED",
            "FULLY_VERIFIED",
        ),
    ),
    (
        "verificationstatus",
        ("PENDING", "RUNNING", "VERIFIED", "NOT_VERIFIED", "FAILED", "CANCELLED"),
    ),
    (
        "workspacestatus",
        ("CREATING", "READY", "PATCH_APPLIED", "BUSY", "DESTROYED", "FAILED"),
    ),
    (
        "reviewaction",
        ("APPROVE", "REJECT", "REQUEST_CHANGES", "REGENERATE", "EXPORT"),
    ),
    (
        "reviewstate",
        ("AWAITING_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED"),
    ),
    (
        "testrunkind",
        ("STATIC", "BUILD", "UNIT", "INTEGRATION", "REGRESSION", "FULL_SUITE"),
    ),
    (
        "tamperingflag",
        (
            "NONE",
            "TEST_DELETED",
            "ASSERTION_WEAKENED",
            "TEST_SKIPPED",
            "EXPECTATION_CHANGED",
            "LINT_DISABLED",
            "TYPECHECK_DISABLED",
            "CI_MODIFIED",
            "VERIFICATION_MODIFIED",
        ),
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    """The column type for one of the enum types above.

    ``checkfirst`` creation already happened, so the column references the
    existing type by name — the same convention the Phase 6 migration uses.
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


def upgrade() -> None:
    _create_enum_types()

    op.create_table(
        "fix_hypotheses",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "debug_session_id",
            postgresql.UUID(),
            sa.ForeignKey("debug_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "analysis_run_id",
            postgresql.UUID(),
            sa.ForeignKey("debug_analysis_runs.id", ondelete="SET NULL"),
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
            "repository_id",
            postgresql.UUID(),
            sa.ForeignKey("code_repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(),
            sa.ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("proposed_change", sa.Text(), nullable=False),
        sa.Column("expected_behavior", sa.Text(), nullable=True),
        sa.Column("category", _enum("fixcategory"), nullable=False),
        sa.Column("scope_files", sa.JSON(), nullable=False),
        sa.Column("excluded_paths", sa.JSON(), nullable=False),
        sa.Column("supporting_evidence", sa.JSON(), nullable=False),
        sa.Column("target_symbols", sa.JSON(), nullable=False),
        sa.Column("risk_level", _enum("risklevel"), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("status", _enum("fixstatus"), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_fix_hypotheses_incident", "fix_hypotheses", ["incident_id", "status"]
    )
    op.create_index(
        "ix_fix_hypotheses_project", "fix_hypotheses", ["project_id", "status"]
    )
    op.create_index("ix_fix_hypotheses_session", "fix_hypotheses", ["debug_session_id"])

    op.create_table(
        "patches",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "fix_hypothesis_id",
            postgresql.UUID(),
            sa.ForeignKey("fix_hypotheses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("patch_experiment_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "repository_id",
            postgresql.UUID(),
            sa.ForeignKey("code_repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(),
            sa.ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("base_commit_sha", sa.String(64), nullable=True),
        sa.Column("patch_format", _enum("patchformat"), nullable=False),
        sa.Column("patch_content", sa.Text(), nullable=False),
        sa.Column("changed_files", sa.Integer(), nullable=False),
        sa.Column("lines_added", sa.Integer(), nullable=False),
        sa.Column("lines_removed", sa.Integer(), nullable=False),
        sa.Column("symbols_modified", sa.JSON(), nullable=False),
        sa.Column("affected_paths", sa.JSON(), nullable=False),
        sa.Column("generated_by", sa.String(32), nullable=False),
        sa.Column("generation_model", sa.String(128), nullable=True),
        sa.Column("generation_version", sa.String(32), nullable=False),
        sa.Column("status", _enum("patchstatus"), nullable=False),
        sa.Column("explanation", sa.JSON(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_patches_hypothesis", "patches", ["fix_hypothesis_id", "status"])
    op.create_index("ix_patches_project", "patches", ["project_id", "status"])
    op.create_index("ix_patches_experiment", "patches", ["patch_experiment_id"])

    op.create_table(
        "patch_workspaces",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("patch_experiment_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "repository_id",
            postgresql.UUID(),
            sa.ForeignKey("code_repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("branch_name", sa.String(128), nullable=False),
        sa.Column("base_commit_sha", sa.String(64), nullable=True),
        sa.Column("patched_commit_sha", sa.String(64), nullable=True),
        sa.Column("root_path", sa.String(512), nullable=True),
        sa.Column("status", _enum("workspacestatus"), nullable=False),
        sa.Column("created_at_workspace", sa.DateTime(timezone=True), nullable=True),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_metadata", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_patch_workspaces_patch", "patch_workspaces", ["patch_id", "status"]
    )
    op.create_index(
        "ix_patch_workspaces_experiment", "patch_workspaces", ["patch_experiment_id"]
    )

    op.create_table(
        "patch_verification_runs",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(),
            sa.ForeignKey("patch_workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reproduction_experiment_id",
            postgresql.UUID(),
            sa.ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", _enum("verificationstatus"), nullable=False),
        sa.Column("level", _enum("verificationlevel"), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("confidence_reason", sa.Text(), nullable=True),
        sa.Column("tampering_flag", _enum("tamperingflag"), nullable=False),
        sa.Column(
            "verification_env_intact",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "baseline_failure_reproduced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("patched_failure_reproduced", sa.Boolean(), nullable=True),
        sa.Column(
            "regression_detected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_patch_verification_patch", "patch_verification_runs", ["patch_id", "status"]
    )
    op.create_index(
        "ix_patch_verification_project",
        "patch_verification_runs",
        ["project_id", "created_at"],
    )

    op.create_table(
        "patch_test_runs",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "verification_run_id",
            postgresql.UUID(),
            sa.ForeignKey("patch_verification_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(),
            sa.ForeignKey("patch_workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", _enum("testrunkind"), nullable=False),
        sa.Column("command_key", sa.String(64), nullable=False),
        sa.Column("command_resolved", sa.Text(), nullable=True),
        sa.Column(
            "unknown_configuration",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tests_total", sa.Integer(), nullable=True),
        sa.Column("tests_passed", sa.Integer(), nullable=True),
        sa.Column("tests_failed", sa.Integer(), nullable=True),
        sa.Column("tests_skipped", sa.Integer(), nullable=True),
        sa.Column("output_tail", sa.Text(), nullable=True),
        sa.Column("selected_tests", sa.JSON(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_patch_test_runs_verification",
        "patch_test_runs",
        ["verification_run_id", "kind"],
    )

    op.create_table(
        "patch_regression_tests",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "verification_run_id",
            postgresql.UUID(),
            sa.ForeignKey("patch_verification_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("test_content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column(
            "ran_on_base", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "failed_on_base", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "ran_on_patched", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "passed_on_patched", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("base_output_tail", sa.Text(), nullable=True),
        sa.Column("patched_output_tail", sa.Text(), nullable=True),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_patch_regression_verification",
        "patch_regression_tests",
        ["verification_run_id"],
    )

    op.create_table(
        "patch_comparisons",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "verification_run_id",
            postgresql.UUID(),
            sa.ForeignKey("patch_verification_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "baseline_experiment_id",
            postgresql.UUID(),
            sa.ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "patched_experiment_id",
            postgresql.UUID(),
            sa.ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("causal_chain_resolved", sa.Boolean(), nullable=True),
        sa.Column("causal_chain_note", sa.Text(), nullable=True),
        sa.Column("regressions", sa.JSON(), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_patch_comparisons_verification",
        "patch_comparisons",
        ["verification_run_id"],
    )

    op.create_table(
        "patch_risk_assessments",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("risk_level", _enum("risklevel"), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("quality_findings", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
    )
    op.create_index("ix_patch_risk_patch", "patch_risk_assessments", ["patch_id"])

    op.create_table(
        "patch_review_actions",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", _enum("reviewaction"), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("new_patch_id", postgresql.UUID(), nullable=True),
        sa.Column("audit_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_patch_review_patch", "patch_review_actions", ["patch_id", "created_at"]
    )
    op.create_index("ix_patch_review_project", "patch_review_actions", ["project_id"])

    op.create_table(
        "patch_artifacts",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patch_id",
            postgresql.UUID(),
            sa.ForeignKey("patches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "verification_run_id",
            postgresql.UUID(),
            sa.ForeignKey("patch_verification_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("artifact_type", sa.String(48), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("immutable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("artifact_metadata", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_patch_artifacts_patch", "patch_artifacts", ["patch_id", "artifact_type"]
    )


def downgrade() -> None:
    op.drop_table("patch_artifacts")
    op.drop_table("patch_review_actions")
    op.drop_table("patch_risk_assessments")
    op.drop_table("patch_comparisons")
    op.drop_table("patch_regression_tests")
    op.drop_table("patch_test_runs")
    op.drop_table("patch_verification_runs")
    op.drop_table("patch_workspaces")
    op.drop_table("patches")
    op.drop_table("fix_hypotheses")
    _drop_enum_types()
