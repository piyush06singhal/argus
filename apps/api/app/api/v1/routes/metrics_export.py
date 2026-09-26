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
from typing import Any, Optional

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
    Severity,
    TraceRecord,
)

router = APIRouter(tags=["Prometheus"])


async def _pipeline_outcome_lines(
    db: AsyncSession,
    *,
    now: datetime,
    project_id: Optional[uuid.UUID],
) -> list[str]:
    """Per-subsystem outcomes from Phases 5, 7, 9 and 10.

    Each metric answers "is this pipeline working?" without anyone opening the
    UI: reproduction results, patch-verification verdicts, remediation statuses
    and learning-run statuses. Failures are labelled, not aggregated away — a
    verification that could not be completed is not a verification that passed.
    """
    from app.models.fix import PatchVerificationRun, VerificationStatus
    from app.models.intelligence import LearningRun, LearningRunStatus
    from app.models.remediation import RemediationAction, RemediationStatus
    from app.models.reproduction import (
        ExperimentStatus,
        ReproductionExperiment,
        ReproductionResult,
    )

    since = now - timedelta(hours=24)
    out: list[str] = []

    async def _group(
        model,
        column: Any,
        metric: str,
        help_text: str,
        *,
        time_column: Any,
        #: The dimension's own name. It used to be hardcoded to ``status``, so
        #: ``..._by_result`` was labelled ``status`` — a series whose label
        #: disagreed with its name, which is exactly the kind of thing an alert
        #: rule written against the docs would silently never match.
        label_name: str = "status",
        #: The column's enum. When given, every member is emitted even with no
        #: rows behind it. This matters more than it looks: a labelled counter
        #: that is simply absent from a fresh deployment is a series the alert
        #: rules and dashboards reference but Prometheus has never seen, so the
        #: alert cannot fire and the panel reads as a broken query. ``{status=
        #: "FAILED"} 0`` means "no failures", which is true and useful; absence
        #: means nothing at all. Series that summarise a *population* stay
        #: genuinely absent when there is no population — see ``mtta_seconds``
        #: below, where zero would be a lie ("acknowledged in 0 seconds").
        enum_type: Any = None,
    ) -> None:
        query = (
            select(column, func.count()).where(time_column >= since).group_by(column)
        )
        if project_id is not None:
            query = query.where(model.project_id == project_id)

        counts: dict[str, float] = {
            member.value: 0.0
            for member in (enum_type or [])
            if hasattr(member, "value")
        }
        for value, count in (await db.execute(query)).all():
            label = getattr(value, "value", value)
            counts[str(label)] = float(count)

        for label in sorted(counts):
            out.append(
                _prom_line(
                    metric,
                    counts[label],
                    labels=f'{label_name}="{label}"',
                    help_text=help_text,
                    metric_type="counter",
                )
            )

    #: ``getattr`` keeps the endpoint honest on a schema where a phase's table
    #: was created by an older migration: the series is simply absent rather
    #: than the whole scrape failing.
    if hasattr(ReproductionExperiment, "created_at"):
        await _group(
            ReproductionExperiment,
            ReproductionExperiment.result,
            "argus_reproduction_runs_24h_by_result",
            "Reproduction experiments by observed result (last 24h)",
            time_column=ReproductionExperiment.created_at,
            label_name="result",
            enum_type=ReproductionResult,
        )
        await _group(
            ReproductionExperiment,
            ReproductionExperiment.status,
            "argus_reproduction_runs_24h_by_status",
            "Reproduction experiments by lifecycle status (last 24h)",
            time_column=ReproductionExperiment.created_at,
            enum_type=ExperimentStatus,
        )
    await _group(
        PatchVerificationRun,
        PatchVerificationRun.status,
        "argus_patch_verifications_24h_by_status",
        "Patch verification outcomes (last 24h) — NOT_VERIFIED is not a pass",
        time_column=PatchVerificationRun.created_at,
        enum_type=VerificationStatus,
    )
    await _group(
        RemediationAction,
        RemediationAction.status,
        "argus_remediation_actions_24h_by_status",
        "Remediation actions by status (last 24h)",
        time_column=RemediationAction.created_at,
        enum_type=RemediationStatus,
    )
    await _group(
        LearningRun,
        LearningRun.status,
        "argus_learning_runs_24h_by_status",
        "Learning pipeline runs by status (last 24h)",
        time_column=LearningRun.created_at,
        enum_type=LearningRunStatus,
    )
    return out


async def _queue_depth_lines(*, project_id: Optional[uuid.UUID]) -> list[str]:
    """Pending jobs per queue, read directly from the broker.

    Emitted only when the broker answers. A scrape that reported ``0`` for an
    unreachable Redis would be worse than one that reported nothing: the alert
    that matters most ("Redis is gone") would read as "the queue is empty".
    """
    from app.services.queue import ALL_QUEUES, IngestionQueue
    from app.services.queue import QueueUnavailable

    out: list[str] = []
    for queue_name in ALL_QUEUES:
        depth: Optional[int] = None
        try:
            client = IngestionQueue(queue_name)._client()
            depth = int(await client.llen(queue_name))
        except QueueUnavailable:
            depth = None
        except Exception:  # noqa: BLE001 - a scrape must never fail on a metric
            depth = None

        #: Written as an explicit guard rather than a `continue` in the handler,
        #: because this is the load-bearing decision of the whole function: a
        #: depth that could not be read is not published at all. Stating it this
        #: way also lets the invariant test *derive* which series are allowed to
        #: be absent by reading the AST (see `test_hardening_metrics.py`).
        if depth is not None:
            out.append(
                _prom_line(
                    "argus_ingestion_queue_depth",
                    float(depth),
                    labels=f'queue="{queue_name}"',
                    help_text="Jobs waiting in the named queue",
                    metric_type="gauge",
                )
            )
    return out


def _prom_line(
    name: str,
    value: float,
    labels: str = "",
    *,
    help_text: str = "",
    metric_type: str = "gauge",
) -> str:
    """Format a single Prometheus metric line.

    HELP and TYPE are emitted with every sample, which is correct for a single
    sample and redundant for a family — ``_exposition`` folds the duplicates out
    of the assembled body, so a caller never has to know whether it is the first
    to report a family.
    """
    lines: list[str] = []
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    label_str = f"{{{labels}}}" if labels else ""
    lines.append(f"{name}{label_str} {value}")
    return "\n".join(lines)


def _exposition(body: str) -> str:
    """Rewrite the body with one HELP/TYPE pair per family, before its samples.

    The text exposition format wants a family's metadata stated once, ahead of
    any sample carrying that name. Emitting it per sample produced 87 metadata
    lines for 36 families on `_by_status` counters, which is not merely noisy:
    any consumer that counts families by counting TYPE lines over-reports the
    metric surface, and that is precisely a number this project publishes.

    Metadata is collected in first-appearance order and the samples follow — so
    metadata for family *N* still precedes its samples, which is the only
    ordering rule the format imposes.
    """
    metadata: list[str] = []
    seen: set[tuple[str, str]] = set()
    samples: list[str] = []

    for line in body.splitlines():
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            kind, _, rest = line[2:].partition(" ")
            name = rest.split(" ", 1)[0]
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            metadata.append(line)
        else:
            samples.append(line)

    return "\n".join([*metadata, "", *samples])


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

    #: Emitted for every severity, zero included, for the reason given in
    #: ``_group``: an alert on ``severity="ERROR"`` must be able to match a
    #: series that exists and reads 0, not one that was never scraped.
    log_counts: dict[str, float] = {level.value: 0.0 for level in Severity}
    for severity, count in log_rows:
        log_counts[severity.value] = float(count)
    for severity in sorted(log_counts):
        lines.append(
            _prom_line(
                "argus_logs_24h_by_severity",
                log_counts[severity],
                labels=f'severity="{severity}"',
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

    # --- Data quality (§87–§90) ---
    from app.models.platform import DataQualityIssue, DataQualityStatus

    open_issues = await db.scalar(
        select(func.count(DataQualityIssue.id)).where(
            DataQualityIssue.status.in_(
                [DataQualityStatus.OPEN, DataQualityStatus.ACKNOWLEDGED]
            )
        )
    )
    lines.append(
        _prom_line(
            "argus_data_quality_issues_open",
            float(open_issues or 0),
            help_text="Cross-phase consistency issues awaiting an operator",
            metric_type="gauge",
        )
    )

    # --- Hardening W5: the pipeline's own outcomes ---------------------------
    #
    # The §11 list asks for coverage of every subsystem ARGUS runs, not just the
    # ingestion half. These series are counted from the same rows the services
    # write, so a metric can never disagree with the history it summarises.
    lines.extend(await _pipeline_outcome_lines(db, now=now, project_id=project_id))

    # --- Queue depth ---------------------------------------------------------
    #
    # The one load signal the database cannot answer: how much work is waiting.
    # Reported per queue rather than summed, because "ingestion is backed up"
    # and "remediation is backed up" need different responses. Absent (not zero)
    # when the broker is unreachable — an unreachable queue is not an empty one.
    lines.extend(await _queue_depth_lines(project_id=None))

    # --- API uptime probe ---
    lines.append(
        _prom_line(
            "argus_api_up",
            1.0,
            help_text="API availability (always 1 when reachable)",
            metric_type="gauge",
        )
    )

    # --- Backups (hardening W10) ---------------------------------------------
    #
    # "Is the platform backed up?" cannot be answered from telemetry, and a
    # schedule nobody measures is a schedule nobody can trust. The freshness
    # series is **absent** when no verified success exists — deliberately
    # different from reporting zero, because zero means "a backup finished in
    # 1970" and absent means "there has never been one". The alert rules key on
    # that difference.
    from app.services.backup_state import backup_state

    backups = await backup_state(db, now=now)
    if backups.last_success_at is not None:
        lines.append(
            _prom_line(
                "argus_backup_last_success_timestamp_seconds",
                backups.last_success_at.timestamp(),
                help_text=(
                    "Start of the newest verified, successful database dump "
                    "(the recovery point)"
                ),
                metric_type="gauge",
            )
        )
    if backups.last_drill_at is not None:
        lines.append(
            _prom_line(
                "argus_backup_last_drill_timestamp_seconds",
                backups.last_drill_at.timestamp(),
                help_text="Start of the newest successful restore rehearsal",
                metric_type="gauge",
            )
        )
    if backups.last_size_bytes is not None:
        lines.append(
            _prom_line(
                "argus_backup_last_size_bytes",
                float(backups.last_size_bytes),
                help_text="Size of the newest verified dump",
                metric_type="gauge",
            )
        )
    if backups.last_duration_seconds is not None:
        lines.append(
            _prom_line(
                "argus_backup_last_duration_seconds",
                float(backups.last_duration_seconds),
                help_text="Wall-clock duration of the newest verified dump",
                metric_type="gauge",
            )
        )
    for status, count in backups.runs_by_status.items():
        lines.append(
            _prom_line(
                "argus_backup_runs_24h_by_status",
                count,
                labels=f'status="{status}"',
                help_text="Backup and drill runs by outcome (last 24h)",
                metric_type="counter",
            )
        )

    # --- Process-local facts (hardening W10: self-observability) -------------
    #
    # Everything above is derived from Postgres. These few series exist only
    # inside a running process — which rate-limit backend this replica is really
    # using, how often it had to fall back, and which instance/region produced
    # this scrape. They are the difference between a dashboard that can see a
    # silent downgrade and one that cannot. Values come from Settings so
    # configuration keeps a single source.
    from app.core.config import get_settings
    from app.core.runtime_metrics import render_prometheus

    settings = get_settings()
    runtime = render_prometheus(
        version=settings.APP_VERSION,
        instance=settings.INSTANCE_ID or None,
        region=settings.INSTANCE_REGION,
    )
    lines.append("")
    lines.extend(runtime)

    body = _exposition("\n".join(lines)) + "\n"
    return PlainTextResponse(
        content=body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )
