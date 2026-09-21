"""Phase 8: predictive reliability tables

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-21 04:00:00.000000

Creates the Phase 8 predictive-reliability domain:

* ``reliability_model_versions``   — registered predictors with their parameters
* ``forecast_feature_snapshots``   — the exact features a forecast used (audit + backtest)
* ``reliability_forecasts``        — one prediction per component/type/horizon
* ``predictive_signals``           — ranked contributing signals (predictive, never causal)
* ``forecast_outcomes``            — append-only scored outcomes
* ``reliability_evaluation_runs``  — immutable scoring passes with sample sizes
* ``reliability_backtests``        — repeatable time-based historical evaluations
* ``reliability_drift_records``    — feature/data/prediction drift, review-flagging only
* ``reliability_early_warnings``   — deduplicated, cooled-down human alerts
* ``forecast_fingerprints``        — dedup/revision registry for one logical scope

Every FK to a Phase 0–7 row is ON DELETE CASCADE or SET NULL: forecasts are
derived evidence and must never outlive, or block the deletion of, the project
they describe.

**Enum names are namespaced on purpose.** PostgreSQL enum types are database
global, and earlier phases already created ``risklevel`` and ``risksignaltype``
with *different* members. Reusing those names would make every Phase 8 insert
coerce into the wrong value set, so all seventeen types below are new names.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every enum type this migration creates, with its members. Kept as data so
#: creation and teardown can never drift apart.
_NEW_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "forecast_horizon",
        ("ONE_HOUR", "SIX_HOURS", "TWENTY_FOUR_HOURS", "SEVEN_DAYS"),
    ),
    (
        "prediction_type",
        (
            "FAILURE_RISK",
            "ERROR_RATE_RISK",
            "LATENCY_RISK",
            "AVAILABILITY_RISK",
            "RESOURCE_EXHAUSTION_RISK",
            "DEPENDENCY_FAILURE_RISK",
            "REGRESSION_RISK",
            "INCIDENT_RISK",
            "RELIABILITY_DEGRADATION",
        ),
    ),
    ("forecast_risk_level", ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")),
    (
        "forecast_status",
        (
            "GENERATED",
            "ACTIVE",
            "EXPIRED",
            "CONFIRMED",
            "FALSE_POSITIVE",
            "INCONCLUSIVE",
        ),
    ),
    ("forecast_data_quality", ("GOOD", "PARTIAL", "POOR", "INSUFFICIENT")),
    ("calibration_status", ("GOOD", "ACCEPTABLE", "POOR", "UNKNOWN")),
    (
        "prediction_outcome",
        (
            "TRUE_POSITIVE",
            "FALSE_POSITIVE",
            "TRUE_NEGATIVE",
            "FALSE_NEGATIVE",
            "INCONCLUSIVE",
        ),
    ),
    (
        "reliability_model_type",
        (
            "ROLLING_TREND",
            "EWMA",
            "THRESHOLD_TRAJECTORY",
            "HISTORICAL_FREQUENCY",
            "LOGISTIC_REGRESSION",
            "GRADIENT_BOOSTED_TREES",
            "TIME_SERIES",
            "SURVIVAL",
        ),
    ),
    (
        "reliability_model_status",
        ("DEVELOPMENT", "VALIDATED", "ACTIVE", "RETIRED"),
    ),
    (
        "predictive_signal_type",
        (
            "ERROR_RATE_INCREASING",
            "LATENCY_INCREASING",
            "RESOURCE_SATURATION",
            "DEPENDENCY_DEGRADATION",
            "FAILURE_FREQUENCY_INCREASING",
            "DEPLOYMENT_INSTABILITY",
            "RECURRENT_INCIDENT_PATTERN",
            "CODE_CHURN_RISK",
            "RECENT_REGRESSION_SIGNAL",
            "ANOMALY_CLUSTER",
        ),
    ),
    ("signal_severity", ("LOW", "MEDIUM", "HIGH")),
    ("feature_trend", ("RISING", "FALLING", "FLAT", "VOLATILE", "UNKNOWN")),
    (
        "forecast_failure_reason",
        (
            "INSUFFICIENT_DATA",
            "DATA_QUALITY_FAILURE",
            "MODEL_UNAVAILABLE",
            "FEATURE_GENERATION_FAILED",
            "PREDICTION_FAILED",
            "EVALUATION_FAILED",
        ),
    ),
    (
        "reliability_drift_kind",
        (
            "FEATURE_DRIFT",
            "PREDICTION_DRIFT",
            "OUTCOME_DRIFT",
            "CALIBRATION_DRIFT",
            "DATA_DRIFT",
        ),
    ),
    ("reliability_drift_status", ("STABLE", "WATCH", "FLAGGED")),
    ("early_warning_status", ("OPEN", "ACKNOWLEDGED", "DISMISSED", "EXPIRED")),
    ("backtest_status", ("QUEUED", "RUNNING", "COMPLETED", "FAILED")),
    (
        "evaluation_status",
        ("COMPLETED", "INSUFFICIENT_SAMPLE", "FAILED"),
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    """The column type for one of the enum types above.

    Column references use ``create_type=False`` because the types are created
    once up front; the same convention the Phase 6/7 migrations use.
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


def upgrade() -> None:
    _create_enum_types()

    op.create_table(
        "reliability_model_versions",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        *_timestamps(),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column(
            "model_type",
            _enum("reliability_model_type"),
            nullable=False,
        ),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("algorithm", sa.String(200), nullable=True),
        sa.Column("training_window_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "feature_schema_version",
            sa.String(40),
            nullable=False,
            server_default="v1",
        ),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("calibration_metrics", postgresql.JSONB(), nullable=True),
        sa.Column(
            "calibration_status",
            _enum("calibration_status"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column(
            "sample_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "status",
            _enum("reliability_model_status"),
            nullable=False,
            server_default="DEVELOPMENT",
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "model_name", "version", name="uq_reliability_model_versions_name_version"
        ),
    )
    op.create_index(
        "ix_reliability_model_versions_model_name",
        "reliability_model_versions",
        ["model_name"],
    )
    op.create_index(
        "ix_reliability_model_versions_model_type",
        "reliability_model_versions",
        ["model_type"],
    )
    op.create_index(
        "ix_reliability_model_versions_status",
        "reliability_model_versions",
        ["status"],
    )

    op.create_table(
        "forecast_feature_snapshots",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column("forecast_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "feature_window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("feature_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "feature_schema_version",
            sa.String(40),
            nullable=False,
            server_default="v1",
        ),
        sa.Column("feature_values", postgresql.JSONB(), nullable=False),
        sa.Column("data_sources", postgresql.JSONB(), nullable=True),
        sa.Column("data_quality", _enum("forecast_data_quality"), nullable=False),
        sa.Column("data_quality_notes", postgresql.JSONB(), nullable=True),
        sa.Column("data_coverage", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_forecast_feature_snapshots_project_id",
        "forecast_feature_snapshots",
        ["project_id"],
    )
    op.create_index(
        "ix_forecast_feature_snapshots_environment_id",
        "forecast_feature_snapshots",
        ["environment_id"],
    )
    op.create_index(
        "ix_forecast_feature_snapshots_component_id",
        "forecast_feature_snapshots",
        ["component_id"],
    )
    op.create_index(
        "ix_forecast_feature_snapshots_forecast_time",
        "forecast_feature_snapshots",
        ["forecast_time"],
    )
    op.create_index(
        "ix_forecast_feature_snapshots_data_quality",
        "forecast_feature_snapshots",
        ["data_quality"],
    )
    op.create_index(
        "ix_forecast_feature_snapshots_scope_time",
        "forecast_feature_snapshots",
        ["project_id", "component_id", "forecast_time"],
    )
    op.create_index(
        "ix_forecast_feature_snapshots_window",
        "forecast_feature_snapshots",
        ["feature_window_start", "feature_window_end"],
    )

    op.create_table(
        "reliability_forecasts",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column("prediction_type", _enum("prediction_type"), nullable=False),
        sa.Column("forecast_horizon", _enum("forecast_horizon"), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("risk_level", _enum("forecast_risk_level"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confidence_reason", sa.Text(), nullable=True),
        sa.Column(
            "calibration_status",
            _enum("calibration_status"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("data_quality", _enum("forecast_data_quality"), nullable=False),
        sa.Column("data_coverage", sa.Float(), nullable=True),
        sa.Column(
            "model_version_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_model_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_version_label", sa.String(120), nullable=False),
        sa.Column(
            "feature_snapshot_id",
            postgresql.UUID(),
            sa.ForeignKey("forecast_feature_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            _enum("forecast_status"),
            nullable=False,
            server_default="GENERATED",
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("dominant_signal", sa.String(80), nullable=True),
        sa.Column("headline", sa.String(400), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column("supporting_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("failure_reason", _enum("forecast_failure_reason"), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "previous_forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "environment_id",
            "component_id",
            "prediction_type",
            "forecast_horizon",
            "revision",
            name="uq_reliability_forecasts_scope_revision",
        ),
    )
    for column in (
        "project_id",
        "environment_id",
        "component_id",
        "prediction_type",
        "forecast_horizon",
        "generated_at",
        "valid_until",
        "risk_level",
        "data_quality",
        "model_version_id",
        "feature_snapshot_id",
        "status",
        "fingerprint",
        "dominant_signal",
    ):
        op.create_index(
            f"ix_reliability_forecasts_{column}", "reliability_forecasts", [column]
        )
    op.create_index(
        "ix_reliability_forecasts_scope_generated",
        "reliability_forecasts",
        ["project_id", "generated_at"],
    )
    op.create_index(
        "ix_reliability_forecasts_scope_status",
        "reliability_forecasts",
        ["project_id", "status", "risk_level"],
    )
    op.create_index(
        "ix_reliability_forecasts_expiry",
        "reliability_forecasts",
        ["valid_until", "status"],
    )
    op.create_index(
        "ix_reliability_forecasts_type_level",
        "reliability_forecasts",
        ["prediction_type", "risk_level"],
    )

    op.create_table(
        "predictive_signals",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        *_timestamps(),
        sa.Column(
            "forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="CASCADE"),
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
        sa.Column("signal_type", _enum("predictive_signal_type"), nullable=False),
        sa.Column("severity", _enum("signal_severity"), nullable=False),
        sa.Column("contribution", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("metric_name", sa.String(255), nullable=True),
        sa.Column("observed_value", sa.Float(), nullable=True),
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("change_rate", sa.Float(), nullable=True),
        sa.Column(
            "trend",
            _enum("feature_trend"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("evidence_ids", postgresql.JSONB(), nullable=True),
        sa.Column(
            "similar_incident_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column in (
        "forecast_id",
        "project_id",
        "component_id",
        "signal_type",
        "metric_name",
    ):
        op.create_index(
            f"ix_predictive_signals_{column}", "predictive_signals", [column]
        )
    op.create_index(
        "ix_predictive_signals_forecast_rank",
        "predictive_signals",
        ["forecast_id", "rank"],
    )
    op.create_index(
        "ix_predictive_signals_scope_type",
        "predictive_signals",
        ["project_id", "signal_type", "created_at"],
    )

    op.create_table(
        "forecast_outcomes",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        *_timestamps(),
        sa.Column(
            "forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="CASCADE"),
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
            "evaluation_window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "evaluation_window_end", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("outcome", _enum("prediction_outcome"), nullable=False),
        sa.Column("actual_event", sa.String(80), nullable=True),
        sa.Column("actual_severity", sa.String(40), nullable=True),
        sa.Column("time_to_event_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "matched_incident_id",
            postgresql.UUID(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "matched_anomaly_id",
            postgresql.UUID(),
            sa.ForeignKey("anomalies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "predicted_risk_level", _enum("forecast_risk_level"), nullable=False
        ),
        sa.Column("predicted_risk_score", sa.Float(), nullable=True),
        sa.Column("evaluation_reason", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column in (
        "forecast_id",
        "project_id",
        "component_id",
        "outcome",
        "matched_incident_id",
        "matched_anomaly_id",
        "evaluated_at",
    ):
        op.create_index(
            f"ix_forecast_outcomes_{column}", "forecast_outcomes", [column]
        )
    op.create_index(
        "ix_forecast_outcomes_forecast_evaluated",
        "forecast_outcomes",
        ["forecast_id", "evaluated_at"],
    )
    op.create_index(
        "ix_forecast_outcomes_project_outcome",
        "forecast_outcomes",
        ["project_id", "outcome"],
    )

    op.create_table(
        "reliability_evaluation_runs",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "model_version_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_model_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_version_label", sa.String(120), nullable=True),
        sa.Column("prediction_type", _enum("prediction_type"), nullable=True),
        sa.Column("forecast_horizon", _enum("forecast_horizon"), nullable=True),
        sa.Column(
            "status",
            _enum("evaluation_status"),
            nullable=False,
            server_default="COMPLETED",
        ),
        sa.Column(
            "dataset_window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("dataset_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "feature_schema_version",
            sa.String(40),
            nullable=False,
            server_default="v1",
        ),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "inconclusive_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("calibration", postgresql.JSONB(), nullable=True),
        sa.Column(
            "calibration_status",
            _enum("calibration_status"),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("reliability_bands", postgresql.JSONB(), nullable=True),
        sa.Column("notes", postgresql.JSONB(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_reliability_evaluation_runs_project_id",
        "reliability_evaluation_runs",
        ["project_id"],
    )
    op.create_index(
        "ix_reliability_evaluation_runs_model_version_id",
        "reliability_evaluation_runs",
        ["model_version_id"],
    )
    op.create_index(
        "ix_reliability_evaluation_runs_project_created",
        "reliability_evaluation_runs",
        ["project_id", "created_at"],
    )

    op.create_table(
        "reliability_backtests",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        *_timestamps(),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("backtest_status"),
            nullable=False,
            server_default="QUEUED",
        ),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_window_seconds", sa.Integer(), nullable=False),
        sa.Column("forecast_horizon", _enum("forecast_horizon"), nullable=False),
        sa.Column("prediction_type", _enum("prediction_type"), nullable=False),
        sa.Column(
            "evaluation_run_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_evaluation_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("steps", postgresql.JSONB(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_reliability_backtests_project_id", "reliability_backtests", ["project_id"]
    )
    op.create_index(
        "ix_reliability_backtests_status", "reliability_backtests", ["status"]
    )
    op.create_index(
        "ix_reliability_backtests_evaluation_run_id",
        "reliability_backtests",
        ["evaluation_run_id"],
    )
    op.create_index(
        "ix_reliability_backtests_project_created",
        "reliability_backtests",
        ["project_id", "created_at"],
    )

    op.create_table(
        "reliability_drift_records",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
            "model_version_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_model_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", _enum("reliability_drift_kind"), nullable=False),
        sa.Column(
            "status",
            _enum("reliability_drift_status"),
            nullable=False,
            server_default="STABLE",
        ),
        sa.Column("feature_name", sa.String(120), nullable=True),
        sa.Column("drift_score", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column(
            "reference_window_start", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("reference_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "requires_review", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    for column in (
        "project_id",
        "component_id",
        "kind",
        "status",
        "feature_name",
        "requires_review",
    ):
        op.create_index(
            f"ix_reliability_drift_records_{column}",
            "reliability_drift_records",
            [column],
        )
    op.create_index(
        "ix_reliability_drift_records_scope_kind",
        "reliability_drift_records",
        ["project_id", "kind", "created_at"],
    )

    op.create_table(
        "reliability_early_warnings",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
            "forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(400), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", _enum("forecast_risk_level"), nullable=False),
        sa.Column(
            "status",
            _enum("early_warning_status"),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column(
            "occurrence_count", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("first_raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_suppressed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "fingerprint",
            name="uq_early_warnings_project_fingerprint",
        ),
    )
    for column in (
        "project_id",
        "environment_id",
        "component_id",
        "forecast_id",
        "fingerprint",
        "severity",
        "status",
        "last_raised_at",
    ):
        op.create_index(
            f"ix_reliability_early_warnings_{column}",
            "reliability_early_warnings",
            [column],
        )
    op.create_index(
        "ix_reliability_early_warnings_project_status",
        "reliability_early_warnings",
        ["project_id", "status", "severity"],
    )

    op.create_table(
        "forecast_fingerprints",
        sa.Column("id", postgresql.UUID(), primary_key=True),
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
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("prediction_type", _enum("prediction_type"), nullable=False),
        sa.Column("forecast_horizon", _enum("forecast_horizon"), nullable=False),
        sa.Column(
            "current_forecast_id",
            postgresql.UUID(),
            sa.ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "revision_count", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "previous_risk_level", _enum("forecast_risk_level"), nullable=True
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "project_id",
            "fingerprint",
            name="uq_forecast_fingerprints_project_fingerprint",
        ),
    )
    for column in (
        "project_id",
        "component_id",
        "fingerprint",
        "current_forecast_id",
        "last_seen_at",
    ):
        op.create_index(
            f"ix_forecast_fingerprints_{column}",
            "forecast_fingerprints",
            [column],
        )


def downgrade() -> None:
    op.drop_table("forecast_fingerprints")
    op.drop_table("reliability_early_warnings")
    op.drop_table("reliability_drift_records")
    op.drop_table("reliability_backtests")
    op.drop_table("reliability_evaluation_runs")
    op.drop_table("forecast_outcomes")
    op.drop_table("predictive_signals")
    op.drop_table("reliability_forecasts")
    op.drop_table("forecast_feature_snapshots")
    op.drop_table("reliability_model_versions")
    _drop_enum_types()
