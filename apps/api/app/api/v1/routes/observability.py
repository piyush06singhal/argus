"""ARGUS Observability Routes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.observability import (
    LogRecord,
    MetricRecord,
    ObservabilityEvent,
    SpanRecord,
    TraceRecord,
)
from app.services.queue import enqueue_anomaly_detect, enqueue_graph_extract
from app.services.redaction import RedactionEngine
from app.schemas.observability import (
    LogRecordCreate,
    LogRecordList,
    LogRecordResponse,
    MetricRecordCreate,
    MetricRecordList,
    MetricRecordResponse,
    ObservabilityEventCreate,
    ObservabilityEventList,
    ObservabilityEventResponse,
    SpanRecordCreate,
    SpanRecordResponse,
    TraceRecordCreate,
    TraceRecordList,
    TraceRecordResponse,
    TraceWithSpans,
)

# Forbidden keys that are never accepted at the ingestion boundary (§46).
_FORBIDDEN_PAYLOAD_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "bearer",
    "private_key",
    "token",
}

_redaction = RedactionEngine()
_settings = get_settings()


def _reject_secrets(payload: Optional[dict]) -> None:
    """422 on any payload that carries a known secret key (§46, §28)."""
    if not payload:
        return
    for key in payload:
        lower = str(key).lower()
        if any(fragment in lower for fragment in _FORBIDDEN_PAYLOAD_KEYS):
            raise HTTPException(
                status_code=422,
                detail=f"Payload key '{key}' is not allowed (secrets are never ingested)",
            )


def _sanitize_dict(
    value: Optional[dict], *, ctx: str, max_keys: int = 0
) -> Dict[str, Any]:
    """Reject forbidden keys, redact sensitive values, enforce size limits (§27, §46).

    Returns a *sanitized copy*; the caller's dict is never mutated.
    """
    if not value:
        return value or {}
    _reject_secrets(value)
    safe = _redaction.redact(value)
    dumped = str(safe)
    if len(dumped) > _settings.MAX_METADATA_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"{ctx} payload exceeds size limit ({_settings.MAX_METADATA_LENGTH} chars)",
        )
    if max_keys and len(safe) > max_keys:
        raise HTTPException(
            status_code=422,
            detail=f"{ctx} exceeds {max_keys} label keys (cardinality guard)",
        )
    return safe


def _truncate_or_reject_message(message: str) -> str:
    """Enforce the maximum log-message length (§27)."""
    if len(message) > _settings.MAX_LOG_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Log message exceeds limit ({_settings.MAX_LOG_MESSAGE_LENGTH} chars)",
        )
    return message


router = APIRouter(prefix="/observability", tags=["Observability"])


# Events
@router.post("/events", response_model=ObservabilityEventResponse, status_code=201)
async def create_event(
    event_data: ObservabilityEventCreate,
    db: AsyncSession = Depends(get_db),
) -> ObservabilityEvent:
    """Create an observability event."""
    data = event_data.model_dump()
    data["payload"] = _sanitize_dict(data.get("payload"), ctx="event")
    data["metadata_"] = _sanitize_dict(data.get("metadata_"), ctx="event metadata")
    event = ObservabilityEvent(**data)
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return event


@router.get("/events", response_model=ObservabilityEventList)
async def list_events(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ObservabilityEventList:
    """List observability events with filtering."""
    query = select(ObservabilityEvent)
    count_query = select(func.count(ObservabilityEvent.id))

    if project_id:
        query = query.where(ObservabilityEvent.project_id == project_id)
        count_query = count_query.where(ObservabilityEvent.project_id == project_id)
    if environment_id:
        query = query.where(ObservabilityEvent.environment_id == environment_id)
        count_query = count_query.where(
            ObservabilityEvent.environment_id == environment_id
        )
    if event_type:
        query = query.where(ObservabilityEvent.event_type == event_type)
        count_query = count_query.where(ObservabilityEvent.event_type == event_type)
    if severity:
        query = query.where(ObservabilityEvent.severity == severity)
        count_query = count_query.where(ObservabilityEvent.severity == severity)
    if start_time:
        query = query.where(ObservabilityEvent.timestamp >= start_time)
        count_query = count_query.where(ObservabilityEvent.timestamp >= start_time)
    if end_time:
        query = query.where(ObservabilityEvent.timestamp <= end_time)
        count_query = count_query.where(ObservabilityEvent.timestamp <= end_time)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(ObservabilityEvent.timestamp.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    events = result.scalars().all()

    return ObservabilityEventList(
        items=[ObservabilityEventResponse.model_validate(e) for e in events],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/events/{event_id}", response_model=ObservabilityEventResponse)
async def get_event(
    event_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ObservabilityEvent:
    """Get a single observability event by ID (§37)."""
    event = await db.get(ObservabilityEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


# Logs
@router.post("/logs", response_model=LogRecordResponse, status_code=201)
async def create_log(
    log_data: LogRecordCreate,
    db: AsyncSession = Depends(get_db),
) -> LogRecord:
    """Create a log record."""
    data = log_data.model_dump()
    data["message"] = _truncate_or_reject_message(data["message"])
    data["metadata_"] = _sanitize_dict(data.get("metadata_"), ctx="log metadata")
    data["raw_payload"] = _sanitize_dict(data.get("raw_payload"), ctx="log raw payload")
    log = LogRecord(**data)
    db.add(log)
    await db.flush()
    await db.refresh(log)
    # Post-ingest hook (Phase 3 §20): queue anomaly detection. Ids only; queue
    # unavailability degrades to the scheduled sweep and never fails ingestion.
    await enqueue_anomaly_detect(
        project_id=log.project_id, environment_id=log.environment_id
    )
    return log


@router.get("/logs", response_model=LogRecordList)
async def list_logs(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    level: Optional[str] = None,
    service: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> LogRecordList:
    """List log records with filtering."""
    query = select(LogRecord)
    count_query = select(func.count(LogRecord.id))

    if project_id:
        query = query.where(LogRecord.project_id == project_id)
        count_query = count_query.where(LogRecord.project_id == project_id)
    if environment_id:
        query = query.where(LogRecord.environment_id == environment_id)
        count_query = count_query.where(LogRecord.environment_id == environment_id)
    if level:
        query = query.where(LogRecord.level == level)
        count_query = count_query.where(LogRecord.level == level)
    if service:
        query = query.where(LogRecord.service == service)
        count_query = count_query.where(LogRecord.service == service)
    if start_time:
        query = query.where(LogRecord.timestamp >= start_time)
        count_query = count_query.where(LogRecord.timestamp >= start_time)
    if end_time:
        query = query.where(LogRecord.timestamp <= end_time)
        count_query = count_query.where(LogRecord.timestamp <= end_time)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(LogRecord.timestamp.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()

    return LogRecordList(
        items=[LogRecordResponse.model_validate(record) for record in logs],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# Metrics
@router.post("/metrics", response_model=MetricRecordResponse, status_code=201)
async def create_metric(
    metric_data: MetricRecordCreate,
    db: AsyncSession = Depends(get_db),
) -> MetricRecord:
    """Create a metric record."""
    data = metric_data.model_dump()
    data["labels"] = _sanitize_dict(
        data.get("labels"), ctx="metric labels", max_keys=_settings.MAX_METRIC_LABELS
    )
    data["metadata_"] = _sanitize_dict(data.get("metadata_"), ctx="metric metadata")
    metric = MetricRecord(**data)
    db.add(metric)
    await db.flush()
    await db.refresh(metric)
    await enqueue_anomaly_detect(
        project_id=metric.project_id, environment_id=metric.environment_id
    )
    return metric


@router.get("/metrics", response_model=MetricRecordList)
async def list_metrics(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    metric_name: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> MetricRecordList:
    """List metric records with filtering."""
    query = select(MetricRecord)
    count_query = select(func.count(MetricRecord.id))

    if project_id:
        query = query.where(MetricRecord.project_id == project_id)
        count_query = count_query.where(MetricRecord.project_id == project_id)
    if environment_id:
        query = query.where(MetricRecord.environment_id == environment_id)
        count_query = count_query.where(MetricRecord.environment_id == environment_id)
    if metric_name:
        query = query.where(MetricRecord.metric_name == metric_name)
        count_query = count_query.where(MetricRecord.metric_name == metric_name)
    if start_time:
        query = query.where(MetricRecord.timestamp >= start_time)
        count_query = count_query.where(MetricRecord.timestamp >= start_time)
    if end_time:
        query = query.where(MetricRecord.timestamp <= end_time)
        count_query = count_query.where(MetricRecord.timestamp <= end_time)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(MetricRecord.timestamp.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    metrics = result.scalars().all()

    return MetricRecordList(
        items=[MetricRecordResponse.model_validate(m) for m in metrics],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# Traces
@router.post("/traces", response_model=TraceRecordResponse, status_code=201)
async def create_trace(
    trace_data: TraceRecordCreate,
    db: AsyncSession = Depends(get_db),
) -> TraceRecord:
    """Create a trace record."""
    # Reject duplicate trace_id cleanly (volume-1 unique constraint backstop).
    existing = await db.execute(
        select(TraceRecord).where(TraceRecord.trace_id == trace_data.trace_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="Trace with this trace_id already exists"
        )

    data = trace_data.model_dump()
    data["metadata_"] = _sanitize_dict(data.get("metadata_"), ctx="trace metadata")
    trace = TraceRecord(**data)
    db.add(trace)
    await db.flush()
    await db.refresh(trace)
    await enqueue_anomaly_detect(
        project_id=trace.project_id, environment_id=trace.environment_id
    )
    return trace


@router.post("/traces/spans", response_model=SpanRecordResponse, status_code=201)
async def create_span(
    span_data: SpanRecordCreate,
    db: AsyncSession = Depends(get_db),
) -> SpanRecord:
    """Create a span record."""
    # Reject duplicate span_id cleanly (volume-1 unique constraint backstop).
    existing = await db.execute(
        select(SpanRecord).where(SpanRecord.span_id == span_data.span_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="Span with this span_id already exists"
        )

    data = span_data.model_dump()
    data["metadata_"] = _sanitize_dict(data.get("metadata_"), ctx="span metadata")
    span = SpanRecord(**data)
    db.add(span)
    await db.flush()
    await db.refresh(span)
    # Post-ingest hook (Phase 2 §38): queue a graph-extract job — payload is
    # ids only; never blocks or fails the ingestion on queue unavailability.
    # SpanRecord carries no environment column, so scoping happens at extract.
    await enqueue_graph_extract(project_id=span.project_id, environment_id=None)
    # Deliberately no anomaly-detection hook here: no detector reads spans
    # (detection consumes metrics, logs, traces and health checks, all of which
    # carry ``environment_id`` and are hooked at their own ingest points), so a
    # per-span enqueue could only ever duplicate work — and one trace would push
    # hundreds of identical detection jobs. Spans still feed the Phase 2
    # structural graph above, and the scheduled sweep covers every environment.
    return span


@router.get("/traces", response_model=TraceRecordList)
async def list_traces(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    trace_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> TraceRecordList:
    """List traces with filtering."""
    query = select(TraceRecord)
    count_query = select(func.count(TraceRecord.id))

    if trace_id:
        query = query.where(TraceRecord.trace_id == trace_id)
        count_query = count_query.where(TraceRecord.trace_id == trace_id)
    if project_id:
        query = query.where(TraceRecord.project_id == project_id)
        count_query = count_query.where(TraceRecord.project_id == project_id)
    if environment_id:
        query = query.where(TraceRecord.environment_id == environment_id)
        count_query = count_query.where(TraceRecord.environment_id == environment_id)
    if start_time:
        query = query.where(TraceRecord.start_time >= start_time)
        count_query = count_query.where(TraceRecord.start_time >= start_time)
    if end_time:
        query = query.where(TraceRecord.start_time <= end_time)
        count_query = count_query.where(TraceRecord.start_time <= end_time)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(TraceRecord.start_time.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    traces = result.scalars().all()

    return TraceRecordList(
        items=[TraceRecordResponse.model_validate(t) for t in traces],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/traces/{trace_id}", response_model=TraceWithSpans)
async def get_trace_with_spans(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> TraceWithSpans:
    """Get a trace with all its spans."""
    # Get the trace
    trace_result = await db.execute(
        select(TraceRecord).where(TraceRecord.trace_id == trace_id)
    )
    trace = trace_result.scalar_one_or_none()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    # Get all spans for this trace
    spans_result = await db.execute(
        select(SpanRecord).where(SpanRecord.trace_id == trace_id)
    )
    spans = spans_result.scalars().all()

    return TraceWithSpans(
        trace=TraceRecordResponse.model_validate(trace),
        spans=[SpanRecordResponse.model_validate(s) for s in spans],
    )
