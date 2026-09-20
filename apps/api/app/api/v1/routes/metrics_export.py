"""ARGUS Prometheus-Compatible Metrics Export.

Phase 1 §18: Prometheus-compatible scrape endpoint.

Exposes internal observability metrics in the Prometheus text exposition
format (https://prometheus.io/docs/instrumenting/exposition_formats/).

The endpoint is intentionally read-only and unauthenticated — Prometheus
scrape is pull-based and runs inside the same network boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.ingestion import (
    ObservabilitySource,
    IngestionFailure,
)
from app.models.observability import (
    LogRecord,
    ObservabilityEvent,
    TraceRecord,
)

router = APIRouter(tags=["Prometheus"])


def _prom_line(
    name: str,
    value: float,
    labels: str = "",
    *,
    help_text: str = "",
    metric_type: str = "gauge",
) -> str:
    """Format a single Prometheus metric line."""
    lines: list[str] = []
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    label_str = f"{{{labels}}}" if labels else ""
    lines.append(f"{name}{label_str} {value}")
    return "\n".join(lines)


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Prometheus-compatible metrics endpoint.

    Returns internal observability metrics in the text exposition format.
    Optionally filter by project_id.
    """
    lines: list[str] = []
    now = datetime.now(tz=timezone.utc)

    # --- Source health counts ---
    source_q = select(
        ObservabilitySource.status,
        func.count(ObservabilitySource.id),
    )
    if project_id:
        source_q = source_q.where(ObservabilitySource.project_id == project_id)
    source_q = source_q.group_by(ObservabilitySource.status)
    source_rows = (await db.execute(source_q)).all()

    lines.append(
        _prom_line(
            "argus_sources_total",
            sum(count for _, count in source_rows),
            help_text="Total registered observability sources",
            metric_type="gauge",
        )
    )
    for status, count in source_rows:
        lines.append(
            _prom_line(
                "argus_sources_by_status",
                count,
                labels=f'status="{status.value}"',
                help_text="Sources by health status",
                metric_type="gauge",
            )
        )

    # --- Source event counts ---
    source_events_q = select(func.sum(ObservabilitySource.event_count))
    if project_id:
        source_events_q = source_events_q.where(
            ObservabilitySource.project_id == project_id
        )
    total_events = (await db.execute(source_events_q)).scalar() or 0
    lines.append(
        _prom_line(
            "argus_source_events_total",
            total_events,
            help_text="Total events received across all sources",
            metric_type="counter",
        )
    )

    # --- Dead-letter count ---
    dl_q = select(func.count(IngestionFailure.id))
    if project_id:
        dl_q = dl_q.where(IngestionFailure.project_id == project_id)
    dl_count = (await db.execute(dl_q)).scalar() or 0
    lines.append(
        _prom_line(
            "argus_dead_letter_total",
            dl_count,
            help_text="Events in the dead-letter store",
            metric_type="gauge",
        )
    )

    # --- Event counts by type (last 24h) ---
    cutoff_24h = now - timedelta(hours=24)
    event_type_q = select(
        ObservabilityEvent.event_type,
        func.count(ObservabilityEvent.id),
    ).where(ObservabilityEvent.timestamp >= cutoff_24h)
    if project_id:
        event_type_q = event_type_q.where(ObservabilityEvent.project_id == project_id)
    event_type_q = event_type_q.group_by(ObservabilityEvent.event_type)
    event_rows = (await db.execute(event_type_q)).all()

    lines.append(
        _prom_line(
            "argus_events_24h_total",
            sum(count for _, count in event_rows),
            help_text="Events ingested in the last 24 hours",
            metric_type="counter",
        )
    )
    for etype, count in event_rows:
        lines.append(
            _prom_line(
                "argus_events_24h_by_type",
                count,
                labels=f'event_type="{etype.value}"',
                help_text="Events by type (last 24h)",
                metric_type="counter",
            )
        )

    # --- Log counts by severity (last 24h) ---
    log_q = select(
        LogRecord.level,
        func.count(LogRecord.id),
    ).where(LogRecord.timestamp >= cutoff_24h)
    if project_id:
        log_q = log_q.where(LogRecord.project_id == project_id)
    log_q = log_q.group_by(LogRecord.level)
    log_rows = (await db.execute(log_q)).all()

    for severity, count in log_rows:
        lines.append(
            _prom_line(
                "argus_logs_24h_by_severity",
                count,
                labels=f'severity="{severity.value}"',
                help_text="Log records by severity (last 24h)",
                metric_type="counter",
            )
        )

    # --- Trace counts (last 24h) ---
    trace_q = select(func.count(TraceRecord.id)).where(
        TraceRecord.start_time >= cutoff_24h
    )
    if project_id:
        trace_q = trace_q.where(TraceRecord.project_id == project_id)
    trace_count = (await db.execute(trace_q)).scalar() or 0
    lines.append(
        _prom_line(
            "argus_traces_24h_total",
            trace_count,
            help_text="Traces created in the last 24 hours",
            metric_type="counter",
        )
    )

    # --- Phase 3: anomaly & incident intelligence (§44, §45) --------------
    from app.services.anomaly_metrics import reliability_metrics

    phase3 = await reliability_metrics(
        db,
        project_id=project_id,
        window_seconds=86_400,
        now=now,
    )
    lines.append(
        _prom_line(
            "argus_anomalies_detected_24h",
            phase3.anomalies_detected,
            help_text="Anomalies detected in the last 24 hours",
            metric_type="counter",
        )
    )
    lines.append(
        _prom_line(
            "argus_anomalies_open",
            phase3.anomalies_open,
            help_text="Anomalies not yet resolved or expired",
            metric_type="gauge",
        )
    )
    lines.append(
        _prom_line(
            "argus_anomalies_suppressed_24h",
            phase3.anomalies_suppressed,
            help_text="Anomalies flagged by an explicit suppression policy",
            metric_type="counter",
        )
    )
    lines.append(
        _prom_line(
            "argus_anomalies_deduplicated",
            phase3.anomalies_deduplicated,
            help_text="Repeat detections collapsed by fingerprint",
            metric_type="gauge",
        )
    )
    lines.append(
        _prom_line(
            "argus_incidents_created_24h",
            phase3.incidents_created,
            help_text="Incidents created in the last 24 hours",
            metric_type="counter",
        )
    )
    lines.append(
        _prom_line(
            "argus_incidents_open",
            phase3.incidents_open,
            help_text="Incidents still open/acknowledged/investigating",
            metric_type="gauge",
        )
    )
    lines.append(
        _prom_line(
            "argus_incidents_resolved_24h",
            phase3.incidents_resolved,
            help_text="Incidents resolved or closed in the last 24 hours",
            metric_type="counter",
        )
    )
    for severity, count in phase3.anomalies_by_severity.items():
        lines.append(
            _prom_line(
                "argus_anomalies_24h_by_severity",
                count,
                labels=f'severity="{severity}"',
                help_text="Anomalies by severity (last 24h)",
                metric_type="counter",
            )
        )
    for anomaly_type, count in phase3.anomalies_by_type.items():
        lines.append(
            _prom_line(
                "argus_anomalies_24h_by_type",
                count,
                labels=f'anomaly_type="{anomaly_type}"',
                help_text="Anomalies by category (last 24h)",
                metric_type="counter",
            )
        )
    if phase3.mtta_seconds is not None:
        lines.append(
            _prom_line(
                "argus_incident_mtta_seconds",
                phase3.mtta_seconds,
                help_text="Mean time to acknowledge incidents (last 24h)",
                metric_type="gauge",
            )
        )
    if phase3.mttr_seconds is not None:
        lines.append(
            _prom_line(
                "argus_incident_mttr_seconds",
                phase3.mttr_seconds,
                help_text=("Mean time from detection to resolution (last 24h)"),
                metric_type="gauge",
            )
        )

    # --- API uptime probe ---
    lines.append(
        _prom_line(
            "argus_api_up",
            1.0,
            help_text="API availability (always 1 when reachable)",
            metric_type="gauge",
        )
    )

    body = "\n".join(lines) + "\n"
    return PlainTextResponse(
        content=body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )
