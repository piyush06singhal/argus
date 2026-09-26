"""ARGUS Data Retention Policy Service.

Enforces configurable retention policies on timestamped/lifecycle metadata.
Each table (events, logs, metrics, traces) has a configurable number of days;
records older than the threshold are hard-deleted.

Phase 1 §22: data retention policies on existing timestamp/lifecycle metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.config import get_settings
from app.models.anomaly import Anomaly, AnomalyObservation
from app.models.incident import Incident, IncidentTimelineEvent
from app.models.intelligence import LearningEvent
from app.models.ingestion import (
    ConfigurationChangeEvent,
    HealthCheckEvent,
    IngestionFailure,
)
from app.models.observability import (
    LogRecord,
    MetricRecord,
    ObservabilityEvent,
    SpanRecord,
    TraceRecord,
)
from app.models.reliability import (
    ForecastFeatureSnapshot,
    ForecastOutcome,
    PredictiveSignal,
    ReliabilityBacktest,
    ReliabilityDriftRecord,
    ReliabilityEarlyWarning,
    ReliabilityEvaluationRun,
    ReliabilityForecast,
)
from app.services.oidc import expire_and_prune_sessions

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class RetentionResult:
    """Outcome of a single retention sweep."""

    table: str
    deleted: int = 0
    threshold_days: int = 0
    cutoff: datetime | None = None


@dataclass
class RetentionSummary:
    """Aggregated outcome across all tables."""

    results: list[RetentionResult] = None  # type: ignore[assignment]
    total_deleted: int = 0

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []

    def add(self, r: RetentionResult) -> None:
        self.results.append(r)
        self.total_deleted += r.deleted


# Mapping of table → (model, timestamp_column, retention_days)
_RETENTION_TABLES: dict[str, tuple[Any, InstrumentedAttribute[Any], int]] = {
    "observability_events": (
        ObservabilityEvent,
        ObservabilityEvent.timestamp,
        settings.RETENTION_EVENTS,
    ),
    "log_records": (LogRecord, LogRecord.timestamp, settings.RETENTION_LOGS),
    "metric_records": (
        MetricRecord,
        MetricRecord.timestamp,
        settings.RETENTION_METRICS,
    ),
    "traces": (TraceRecord, TraceRecord.start_time, settings.RETENTION_TRACES),
    # Spans follow the same retention as their parent traces.
    "spans": (SpanRecord, SpanRecord.start_time, settings.RETENTION_TRACES),
    # Configuration and health events share the log retention window.
    "configuration_change_events": (
        ConfigurationChangeEvent,
        ConfigurationChangeEvent.timestamp,
        settings.RETENTION_LOGS,
    ),
    "health_check_events": (
        HealthCheckEvent,
        HealthCheckEvent.timestamp,
        settings.RETENTION_LOGS,
    ),
    # Dead-letter records: shorter window — operational, not archival.
    "ingestion_failures": (IngestionFailure, IngestionFailure.failed_at, 30),
    # Phase 3: anomalies are swept on their retention window; incidents are
    # kept far longer because they are the historical record humans rely on.
    "anomalies": (Anomaly, Anomaly.detected_at, settings.RETENTION_ANOMALIES),
    "anomaly_observations": (
        AnomalyObservation,
        AnomalyObservation.observed_at,
        settings.RETENTION_ANOMALIES,
    ),
    "incidents": (Incident, Incident.detected_at, settings.RETENTION_INCIDENTS),
    "incident_timeline_events": (
        IncidentTimelineEvent,
        IncidentTimelineEvent.occurred_at,
        settings.RETENTION_INCIDENTS,
    ),
    # Phase 8: forecasts are the operative record and are swept on their own
    # window; the derived rows (signals, outcomes, snapshots) share it, because
    # a signal without its forecast — or a forecast without its snapshot — is
    # not auditable. Evaluation runs and drift records are kept far longer than
    # the forecasts they describe, so precision history and drift trends survive
    # the forecasts being pruned.
    "reliability_forecasts": (
        ReliabilityForecast,
        ReliabilityForecast.generated_at,
        settings.RETENTION_RELIABILITY_FORECASTS,
    ),
    "predictive_signals": (
        PredictiveSignal,
        PredictiveSignal.created_at,
        settings.RETENTION_RELIABILITY_FORECASTS,
    ),
    "forecast_feature_snapshots": (
        ForecastFeatureSnapshot,
        ForecastFeatureSnapshot.forecast_time,
        settings.RETENTION_RELIABILITY_FORECASTS,
    ),
    "forecast_outcomes": (
        ForecastOutcome,
        ForecastOutcome.evaluated_at,
        settings.RETENTION_RELIABILITY_FORECASTS,
    ),
    "reliability_early_warnings": (
        ReliabilityEarlyWarning,
        ReliabilityEarlyWarning.first_raised_at,
        settings.RETENTION_RELIABILITY_FORECASTS,
    ),
    "reliability_evaluation_runs": (
        ReliabilityEvaluationRun,
        ReliabilityEvaluationRun.created_at,
        settings.RETENTION_RELIABILITY_EVALUATIONS,
    ),
    "reliability_backtests": (
        ReliabilityBacktest,
        ReliabilityBacktest.created_at,
        settings.RETENTION_RELIABILITY_EVALUATIONS,
    ),
    "reliability_drift_records": (
        ReliabilityDriftRecord,
        ReliabilityDriftRecord.created_at,
        settings.RETENTION_RELIABILITY_EVALUATIONS,
    ),
    #: Phase 10. The learning event log is evidence with a consumer, not belief:
    #: an outcome that has been consumed is history, and history is bounded.
    #: Experiences and knowledge are deliberately absent — they are retired by
    #: the knowledge lifecycle (DEPRECATED / SUPERSEDED) and stay readable, and
    #: they are bounded anyway by the incident and project cascades.
    "learning_events": (
        LearningEvent,
        LearningEvent.created_at,
        settings.RETENTION_LEARNING_EVENTS,
    ),
}


class RetentionService:
    """Enforce data retention policies.

    Usage::

        service = RetentionService(db)
        summary = await service.run_sweep()
    """

    def __init__(self, db: AsyncSession):
        self._db = db

    async def run_sweep(
        self, *, overrides: Dict[str, int] | None = None
    ) -> RetentionSummary:
        """Delete records older than their retention threshold.

        Args:
            overrides: Optional mapping of table_name → retention_days to
                       temporarily override the config defaults (useful for
                       admin endpoints and tests).
        """
        summary = RetentionSummary()
        overrides = overrides or {}

        for table_name, (model, ts_col, default_days) in _RETENTION_TABLES.items():
            days = overrides.get(table_name, default_days)
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            # Count first so we can report what we're about to delete.
            count_q = select(func.count()).select_from(model).where(ts_col < cutoff)  # type: ignore[attr-defined]
            count_result = await self._db.execute(count_q)
            count = count_result.scalar() or 0

            if count > 0:
                delete_q = delete(model).where(ts_col < cutoff)  # type: ignore[attr-defined]
                await self._db.execute(delete_q)
                await self._db.flush()
                logger.info(
                    f"Retention: deleted {count} rows from {table_name} (older than {days}d)"
                )

            summary.add(
                RetentionResult(
                    table=table_name,
                    deleted=count,
                    threshold_days=days,
                    cutoff=cutoff,
                )
            )

        #: SSO sessions are swept by their own rule (hardening W2) rather than
        #: through ``_RETENTION_TABLES``, because that map deletes purely by
        #: age — and an ``ACTIVE`` credential must never be deleted by a
        #: retention sweep however old it is. The dedicated rule retires what
        #: has already stopped working and only then removes the dead rows.
        sso_expired, sso_deleted = await expire_and_prune_sessions(
            self._db, retention_days=settings.RETENTION_OIDC_SESSIONS_DAYS
        )
        if sso_expired or sso_deleted:
            logger.info(
                "Retention: retired %d expired SSO session(s), removed %d dead "
                "one(s) older than %dd",
                sso_expired,
                sso_deleted,
                settings.RETENTION_OIDC_SESSIONS_DAYS,
            )
        summary.add(
            RetentionResult(
                table="oidc_sessions",
                deleted=sso_deleted,
                threshold_days=settings.RETENTION_OIDC_SESSIONS_DAYS,
                cutoff=datetime.now(timezone.utc)
                - timedelta(days=settings.RETENTION_OIDC_SESSIONS_DAYS),
            )
        )

        return summary

    async def preview(
        self, *, overrides: Dict[str, int] | None = None
    ) -> RetentionSummary:
        """Preview what *would* be deleted without modifying data.

        Same as ``run_sweep`` but never executes DELETE — dry run only.
        """
        summary = RetentionSummary()
        overrides = overrides or {}

        for table_name, (model, ts_col, default_days) in _RETENTION_TABLES.items():
            days = overrides.get(table_name, default_days)
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            count_q = select(func.count()).select_from(model).where(ts_col < cutoff)  # type: ignore[attr-defined]
            count_result = await self._db.execute(count_q)
            count = count_result.scalar() or 0

            summary.add(
                RetentionResult(
                    table=table_name,
                    deleted=count,
                    threshold_days=days,
                    cutoff=cutoff,
                )
            )

        return summary

    async def get_policy(self) -> Dict[str, int]:
        """Return the current retention policy (table → days)."""
        return {
            table_name: default_days
            for table_name, (_, _, default_days) in _RETENTION_TABLES.items()
        }
