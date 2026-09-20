"""Phase 5: failure reproduction tables

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-20 10:00:00.000000

Creates the reproduction domain:

* ``reproduction_experiments``  — one controlled experiment per hypothesis
* ``reproduction_plans``        — the explicit, inspectable plan
* ``reproduction_hypotheses``   — the Phase 4 claim restated as a test
* ``reproduction_sandboxes``    — disposable isolated environments
* ``reproduction_runs``         — one repetition (non-determinism lives here)
* ``reproduction_inputs``       — sanitized replay items and their outcomes
* ``reproduction_faults``       — injected faults + audit trail
* ``reproduction_observations`` — captured reproduction telemetry
* ``reproduction_comparisons``  — explainable original-vs-reproduced scores
* ``reproduction_validations``  — what the experiment says about the hypothesis
* ``reproduction_artifacts``    — content-addressed immutable artifacts
* ``environment_snapshots``     — sanitized environment captures for diffing

Table order matters: ``reproduction_runs`` and ``reproduction_sandboxes`` are
referenced by inputs/faults/observations/comparisons/artifacts (``SET NULL`` or
``CASCADE``), so both are created before those.

``confidencelevel`` is **not** created here — it is the Phase 4 enum
(f1a2b3c4d5e6) and is referenced with ``create_type=False`` so a reproduction
verdict and a causal confidence share one scale. All other enum types are new
and created once at the top of ``upgrade()``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EXPERIMENT_STATUSES = [
    "PLANNED",
    "VALIDATING",
    "PROVISIONING",
    "READY",
    "REPLAYING",
    "RUNNING",
    "COLLECTING",
    "COMPARING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
]
REPRODUCTION_RESULTS = [
    "SUCCESSFUL",
    "PARTIAL",
    "FAILED",
    "INCONCLUSIVE",
    "NOT_RUN",
]
REPRODUCTION_STRATEGIES = [
    "SYNTHETIC_INPUT_REPLAY",
    "EVENT_REPLAY",
    "DEPENDENCY_FAULT",
    "CONFIGURATION_REPLAY",
    "STATE_SNAPSHOT",
]
SANDBOX_BACKENDS = ["LOCAL_PROCESS", "DOCKER"]
SANDBOX_STATUSES = [
    "CREATING",
    "READY",
    "STOPPING",
    "STOPPED",
    "DESTROYED",
    "FAILED",
]
SANDBOX_NETWORK_POLICIES = ["ISOLATED", "MOCK_DEPENDENCIES", "CONTROLLED_EGRESS"]
RUN_STATUSES = [
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "TIMED_OUT",
    "CANCELLED",
]
FAILURE_CLASSES = [
    "ENVIRONMENT_ERROR",
    "INPUT_ERROR",
    "TIMEOUT",
    "RESOURCE_LIMIT",
    "DEPENDENCY_UNAVAILABLE",
    "SANDBOX_ERROR",
    "APPLICATION_FAILURE",
    "NO_FAILURE_OBSERVED",
    "INSUFFICIENT_TELEMETRY",
    "UNKNOWN",
]
REPLAY_INPUT_SOURCES = [
    "HTTP_REQUEST",
    "EVENT",
    "MESSAGE",
    "TRACE_INPUT",
    "SYNTHETIC",
]
REPLAY_STATUSES = [
    "PENDING",
    "SENT",
    "SUCCEEDED",
    "FAILED",
    "REJECTED",
    "SKIPPED",
]
FAULT_TYPES = [
    "LATENCY",
    "TIMEOUT",
    "HTTP_4XX",
    "HTTP_5XX",
    "CONNECTION_FAILURE",
    "RESPONSE_CORRUPTION",
    "RESOURCE_PRESSURE",
    "DEPENDENCY_UNAVAILABLE",
]
FAULT_TRIGGERS = [
    "IMMEDIATE",
    "AFTER_REPLAY_INDEX",
    "AT_OFFSET",
    "ON_REQUEST_COUNT",
    "MANUAL",
]
FAULT_STATUSES = ["PLANNED", "ACTIVE", "COMPLETED", "FAILED", "SKIPPED"]
OBSERVATION_SIGNALS = [
    "SPAN",
    "TRACE",
    "LOG",
    "METRIC",
    "HEALTH",
    "EVENT",
    "DEPLOYMENT",
    "CONFIGURATION",
]
OBSERVATION_STATUSES = ["EXPECTED", "UNEXPECTED", "NEUTRAL", "MISSING"]
VALIDATION_OUTCOMES = [
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "NOT_SUPPORTED",
    "INCONCLUSIVE",
]
ARTIFACT_TYPES = [
    "ENVIRONMENT_SNAPSHOT",
    "REPRODUCTION_PLAN",
    "REPRODUCTION_MANIFEST",
    "REPLAY_MANIFEST",
    "TELEMETRY_SNAPSHOT",
    "LOGS",
    "TRACE_SUMMARY",
    "COMPARISON_RESULT",
    "SANDBOX_METADATA",
    "FAULT_RECORD",
    "VALIDATION_REPORT",
    "PROCESS_OUTPUT",
]
SNAPSHOT_SOURCES = ["ORIGINAL", "SANDBOX"]
CONFIDENCE_LEVELS = ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"]

#: (name, values) for the enum types this migration creates.
_NEW_ENUMS = [
    ("experimentstatus", EXPERIMENT_STATUSES),
    ("reproductionresult", REPRODUCTION_RESULTS),
    ("reproductionstrategy", REPRODUCTION_STRATEGIES),
    ("sandboxbackendkind", SANDBOX_BACKENDS),
    ("sandboxstatus", SANDBOX_STATUSES),
    ("sandboxnetworkpolicy", SANDBOX_NETWORK_POLICIES),
    ("runstatus", RUN_STATUSES),
    ("failureclass", FAILURE_CLASSES),
    ("replayinputsource", REPLAY_INPUT_SOURCES),
    ("replaystatus", REPLAY_STATUSES),
    ("faulttype", FAULT_TYPES),
    ("faulttrigger", FAULT_TRIGGERS),
    ("faultstatus", FAULT_STATUSES),
    ("observationsignal", OBSERVATION_SIGNALS),
    ("observationstatus", OBSERVATION_STATUSES),
    ("validationoutcome", VALIDATION_OUTCOMES),
    ("artifacttype", ARTIFACT_TYPES),
    ("snapshotsource", SNAPSHOT_SOURCES),
]


def _enum(values: list[str], name: str) -> postgresql.ENUM:
    """Reference an enum type; ``confidencelevel`` already exists from Phase 4."""
    return postgresql.ENUM(*values, name=name, create_type=False)


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
    """The shared ``BaseModel`` columns every reproduction table carries."""
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

    # ---- 1. reproduction_experiments ---------------------------------------
    op.create_table(
        "reproduction_experiments",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("environment_id", postgresql.UUID(), nullable=True),
        sa.Column("incident_id", postgresql.UUID(), nullable=False),
        sa.Column("causal_analysis_id", postgresql.UUID(), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(), nullable=True),
        sa.Column("experiment_version", sa.Integer(), nullable=False),
        sa.Column(
            "status", _enum(EXPERIMENT_STATUSES, "experimentstatus"), nullable=False
        ),
        sa.Column(
            "result", _enum(REPRODUCTION_RESULTS, "reproductionresult"), nullable=False
        ),
        sa.Column(
            "confidence", _enum(CONFIDENCE_LEVELS, "confidencelevel"), nullable=False
        ),
        sa.Column("trigger", sa.String(length=255), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("engine_version", sa.String(length=64), nullable=True),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("completed_runs", sa.Integer(), nullable=False),
        sa.Column("telemetry_namespace", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "failure_classification",
            _enum(FAILURE_CLASSES, "failureclass"),
            nullable=True,
        ),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["causal_analysis_id"], ["causal_analyses.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["root_cause_candidates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_experiments_incident_version",
        "reproduction_experiments",
        ["incident_id", "experiment_version"],
    )
    op.create_index(
        "ix_repro_experiments_project_status",
        "reproduction_experiments",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_repro_experiments_candidate", "reproduction_experiments", ["candidate_id"]
    )

    # ---- 2. reproduction_plans ---------------------------------------------
    op.create_table(
        "reproduction_plans",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "strategy",
            _enum(REPRODUCTION_STRATEGIES, "reproductionstrategy"),
            nullable=False,
        ),
        sa.Column("target_component_id", postgresql.UUID(), nullable=True),
        sa.Column("target_component_name", sa.String(length=255), nullable=False),
        sa.Column("target_version", sa.String(length=64), nullable=True),
        sa.Column("objectives", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "required_services", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "required_dependencies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "input_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "expected_behavior", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "safety_constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "resource_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "network_policy",
            _enum(SANDBOX_NETWORK_POLICIES, "sandboxnetworkpolicy"),
            nullable=False,
        ),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column(
            "derived_from", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_plans_experiment", "reproduction_plans", ["experiment_id"], unique=True
    )

    # ---- 3. reproduction_hypotheses ----------------------------------------
    op.create_table(
        "reproduction_hypotheses",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("source_analysis_id", postgresql.UUID(), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(), nullable=True),
        sa.Column("candidate_type", sa.String(length=64), nullable=True),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("component_name", sa.String(length=255), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("expected_failure", sa.Text(), nullable=True),
        sa.Column(
            "expected_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "expected_sequence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "expected_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("expected_time_window_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "supporting_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "contradicted_by", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_analysis_id"], ["causal_analyses.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["root_cause_candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_hypotheses_experiment",
        "reproduction_hypotheses",
        ["experiment_id"],
        unique=True,
    )

    # ---- 4. reproduction_sandboxes -----------------------------------------
    op.create_table(
        "reproduction_sandboxes",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=True),
        sa.Column("project_id", postgresql.UUID(), nullable=True),
        sa.Column("sandbox_key", sa.String(length=128), nullable=False),
        sa.Column(
            "backend", _enum(SANDBOX_BACKENDS, "sandboxbackendkind"), nullable=False
        ),
        sa.Column("status", _enum(SANDBOX_STATUSES, "sandboxstatus"), nullable=False),
        sa.Column(
            "network_policy",
            _enum(SANDBOX_NETWORK_POLICIES, "sandboxnetworkpolicy"),
            nullable=False,
        ),
        sa.Column("root_path", sa.String(length=1024), nullable=True),
        sa.Column(
            "resource_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("services", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "process_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "container_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("created_at_sandbox", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False),
        sa.Column("cleanup_error", sa.Text(), nullable=True),
        sa.Column("orphaned", sa.Boolean(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_sandboxes_experiment", "reproduction_sandboxes", ["experiment_id"]
    )
    op.create_index("ix_repro_sandboxes_status", "reproduction_sandboxes", ["status"])
    op.create_index(
        "ix_repro_sandboxes_key", "reproduction_sandboxes", ["sandbox_key"], unique=True
    )

    # ---- 5. reproduction_runs ----------------------------------------------
    op.create_table(
        "reproduction_runs",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("sandbox_id", postgresql.UUID(), nullable=True),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column("status", _enum(RUN_STATUSES, "runstatus"), nullable=False),
        sa.Column(
            "result", _enum(REPRODUCTION_RESULTS, "reproductionresult"), nullable=False
        ),
        sa.Column(
            "failure_classification",
            _enum(FAILURE_CLASSES, "failureclass"),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("replay_request_count", sa.Integer(), nullable=False),
        sa.Column("replay_success_count", sa.Integer(), nullable=False),
        sa.Column("replay_failure_count", sa.Integer(), nullable=False),
        sa.Column("replay_rejected_count", sa.Integer(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("telemetry_bytes", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "faults_applied", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sandbox_id"], ["reproduction_sandboxes.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_runs_experiment_index",
        "reproduction_runs",
        ["experiment_id", "run_index"],
    )

    # ---- 6. reproduction_inputs --------------------------------------------
    op.create_table(
        "reproduction_inputs",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=True),
        sa.Column("input_index", sa.Integer(), nullable=False),
        sa.Column("plan_order", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            _enum(REPLAY_INPUT_SOURCES, "replayinputsource"),
            nullable=False,
        ),
        sa.Column("replay_id", sa.String(length=64), nullable=True),
        sa.Column("status", _enum(REPLAY_STATUSES, "replaystatus"), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=True),
        sa.Column("target_service", sa.String(length=128), nullable=True),
        sa.Column("target_path", sa.String(length=512), nullable=True),
        sa.Column("target_component_id", postgresql.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("redactions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("relative_offset_ms", sa.Integer(), nullable=False),
        sa.Column("original_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "response_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["reproduction_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_inputs_experiment_order",
        "reproduction_inputs",
        ["experiment_id", "plan_order"],
    )
    op.create_index("ix_repro_inputs_run", "reproduction_inputs", ["run_id"])

    # ---- 7. reproduction_faults --------------------------------------------
    op.create_table(
        "reproduction_faults",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=True),
        sa.Column("fault_type", _enum(FAULT_TYPES, "faulttype"), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("target_component_id", postgresql.UUID(), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("trigger", _enum(FAULT_TRIGGERS, "faulttrigger"), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("intensity", sa.Float(), nullable=True),
        sa.Column("status", _enum(FAULT_STATUSES, "faultstatus"), nullable=False),
        sa.Column("injected", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requests_affected", sa.Integer(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["reproduction_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["target_component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_faults_experiment", "reproduction_faults", ["experiment_id"]
    )
    op.create_index("ix_repro_faults_run", "reproduction_faults", ["run_id"])

    # ---- 8. reproduction_observations --------------------------------------
    op.create_table(
        "reproduction_observations",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=False),
        sa.Column("sandbox_id", postgresql.UUID(), nullable=True),
        sa.Column("namespace", sa.String(length=255), nullable=False),
        sa.Column(
            "signal_type",
            _enum(OBSERVATION_SIGNALS, "observationsignal"),
            nullable=False,
        ),
        sa.Column(
            "status", _enum(OBSERVATION_STATUSES, "observationstatus"), nullable=False
        ),
        sa.Column("matched_expected", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relative_offset_ms", sa.Integer(), nullable=False),
        sa.Column("component_name", sa.String(length=255), nullable=True),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("metric_name", sa.String(length=255), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("expected_value", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("operation", sa.String(length=255), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.Boolean(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("parent_span_id", sa.String(length=64), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["reproduction_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sandbox_id"], ["reproduction_sandboxes.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_observations_experiment", "reproduction_observations", ["experiment_id"]
    )
    op.create_index(
        "ix_repro_observations_run_signal",
        "reproduction_observations",
        ["run_id", "signal_type"],
    )
    op.create_index(
        "ix_repro_observations_component", "reproduction_observations", ["component_name"]
    )

    # ---- 9. reproduction_comparisons ---------------------------------------
    op.create_table(
        "reproduction_comparisons",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "overall_similarity",
            _enum(CONFIDENCE_LEVELS, "confidencelevel"),
            nullable=False,
        ),
        sa.Column("similarity_score", sa.Float(), nullable=True),
        sa.Column(
            "result", _enum(REPRODUCTION_RESULTS, "reproductionresult"), nullable=False
        ),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("formula_reference", sa.Text(), nullable=True),
        sa.Column(
            "component_overlap", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "matched_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "missing_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "extra_components", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "sequence_original", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "sequence_reproduced", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("sequence_match", sa.Boolean(), nullable=True),
        sa.Column(
            "metric_deltas", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "error_comparison", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "trace_topology", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("log_pattern", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recovery", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("temporal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "original_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "reproduced_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["reproduction_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_comparisons_experiment", "reproduction_comparisons", ["experiment_id"]
    )
    op.create_index("ix_repro_comparisons_run", "reproduction_comparisons", ["run_id"])

    # ---- 10. reproduction_validations --------------------------------------
    op.create_table(
        "reproduction_validations",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "outcome",
            _enum(VALIDATION_OUTCOMES, "validationoutcome"),
            nullable=False,
        ),
        sa.Column(
            "confidence", _enum(CONFIDENCE_LEVELS, "confidencelevel"), nullable=False
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "supporting_observations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "contradicting_observations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "environment_differences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "missing_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("determinism", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("artifact_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["root_cause_candidates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_validations_experiment",
        "reproduction_validations",
        ["experiment_id"],
        unique=True,
    )

    # ---- 11. reproduction_artifacts ----------------------------------------
    op.create_table(
        "reproduction_artifacts",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("run_id", postgresql.UUID(), nullable=True),
        sa.Column("artifact_type", _enum(ARTIFACT_TYPES, "artifacttype"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("storage_location", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("immutable", sa.Boolean(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["reproduction_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repro_artifacts_experiment", "reproduction_artifacts", ["experiment_id"]
    )
    op.create_index(
        "ix_repro_artifacts_type", "reproduction_artifacts", ["artifact_type"]
    )

    # ---- 12. environment_snapshots -----------------------------------------
    op.create_table(
        "environment_snapshots",
        sa.Column("id", postgresql.UUID(), nullable=False),
        *_timestamps(),
        sa.Column("experiment_id", postgresql.UUID(), nullable=True),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("source", _enum(SNAPSHOT_SOURCES, "snapshotsource"), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("application_version", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.String(length=64), nullable=True),
        sa.Column(
            "runtime_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "dependency_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "environment_variables",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "feature_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "service_topology", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "resource_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("sanitization", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "snapshot_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["reproduction_experiments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_env_snapshots_experiment_source",
        "environment_snapshots",
        ["experiment_id", "source"],
    )


def downgrade() -> None:
    op.drop_index("ix_env_snapshots_experiment_source", table_name="environment_snapshots")
    op.drop_table("environment_snapshots")

    op.drop_index("ix_repro_artifacts_type", table_name="reproduction_artifacts")
    op.drop_index("ix_repro_artifacts_experiment", table_name="reproduction_artifacts")
    op.drop_table("reproduction_artifacts")

    op.drop_index("ix_repro_validations_experiment", table_name="reproduction_validations")
    op.drop_table("reproduction_validations")

    op.drop_index("ix_repro_comparisons_run", table_name="reproduction_comparisons")
    op.drop_index("ix_repro_comparisons_experiment", table_name="reproduction_comparisons")
    op.drop_table("reproduction_comparisons")

    op.drop_index(
        "ix_repro_observations_component", table_name="reproduction_observations"
    )
    op.drop_index(
        "ix_repro_observations_run_signal", table_name="reproduction_observations"
    )
    op.drop_index(
        "ix_repro_observations_experiment", table_name="reproduction_observations"
    )
    op.drop_table("reproduction_observations")

    op.drop_index("ix_repro_faults_run", table_name="reproduction_faults")
    op.drop_index("ix_repro_faults_experiment", table_name="reproduction_faults")
    op.drop_table("reproduction_faults")

    op.drop_index("ix_repro_inputs_run", table_name="reproduction_inputs")
    op.drop_index("ix_repro_inputs_experiment_order", table_name="reproduction_inputs")
    op.drop_table("reproduction_inputs")

    op.drop_index("ix_repro_runs_experiment_index", table_name="reproduction_runs")
    op.drop_table("reproduction_runs")

    op.drop_index("ix_repro_sandboxes_key", table_name="reproduction_sandboxes")
    op.drop_index("ix_repro_sandboxes_status", table_name="reproduction_sandboxes")
    op.drop_index("ix_repro_sandboxes_experiment", table_name="reproduction_sandboxes")
    op.drop_table("reproduction_sandboxes")

    op.drop_index("ix_repro_hypotheses_experiment", table_name="reproduction_hypotheses")
    op.drop_table("reproduction_hypotheses")

    op.drop_index("ix_repro_plans_experiment", table_name="reproduction_plans")
    op.drop_table("reproduction_plans")

    op.drop_index("ix_repro_experiments_candidate", table_name="reproduction_experiments")
    op.drop_index(
        "ix_repro_experiments_project_status", table_name="reproduction_experiments"
    )
    op.drop_index(
        "ix_repro_experiments_incident_version", table_name="reproduction_experiments"
    )
    op.drop_table("reproduction_experiments")

    _drop_enum_types()
