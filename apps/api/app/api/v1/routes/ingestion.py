"""ARGUS Phase 1 Ingestion Routes.

Covers the Phase 1 ingestion surface:
  - Source registry CRUD + health (§5, §45)
  - Batch ingestion of raw envelopes (§19, §44)
  - Configuration-change and health-check events (§14, §15)
  - Webhook ingestion foundation (§47)
  - Ingestion stats / dead-letter inspection (§45)
  - Mock source runner for development/demo
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from starlette.responses import JSONResponse
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.sources import MockObservabilitySource, RawObservabilityEvent
from app.models.ingestion import (
    ConfigurationChangeEvent,
    HealthCheckEvent,
    HealthStatus,
    IngestionFailure,
    ObservabilitySource,
    ObservabilitySourceCategory,
)
from app.models.observability import EventType
from app.services.queue import enqueue_anomaly_detect
from app.schemas.base import BaseSchema
from app.schemas.ingestion import (
    ConfigurationChangeEventCreate,
    ConfigurationChangeEventList,
    ConfigurationChangeEventResponse,
    HealthCheckEventCreate,
    HealthCheckEventList,
    HealthCheckEventResponse,
    IngestionFailureResponse,
    IngestionSourceHealth,
    IngestionSummary,
    ObservabilitySourceCreate,
    ObservabilitySourceList,
    ObservabilitySourceResponse,
    ObservabilitySourceUpdate,
)
from app.services.ingestion import IngestionPipeline
from app.services.ingestion_stats import IngestionStatsService
from app.services.queue import IngestionQueue, QueueUnavailable, make_job

router = APIRouter(prefix="/ingestion", tags=["Ingestion"])
settings = get_settings()

# Queue used by the async enrichment path (§37).
QUEUE_NAME_HINT = "argus:ingest:events"

# Keys that the API refuses to accept in source configuration / event payloads.
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


def _reject_secrets(payload: dict | None) -> None:
    """Reject payloads that leak known secrets at the boundary (§46)."""
    if not payload:
        return
    for key in payload:
        lower = str(key).lower()
        if any(f in lower for f in _FORBIDDEN_PAYLOAD_KEYS):
            raise HTTPException(
                status_code=422,
                detail=f"Payload key '{key}' is not allowed (secrets are never ingested)",
            )


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------
@router.post("/sources", response_model=ObservabilitySourceResponse, status_code=201)
async def register_source(
    data: ObservabilitySourceCreate,
    db: AsyncSession = Depends(get_db),
) -> ObservabilitySource:
    """Register a new observability source."""
    _reject_secrets(data.configuration)
    source = ObservabilitySource(**data.model_dump())
    db.add(source)
    await db.flush()
    await db.refresh(source)
    return source


@router.get("/sources", response_model=ObservabilitySourceList)
async def list_sources(
    project_id: Optional[uuid.UUID] = None,
    source_type: Optional[ObservabilitySourceCategory] = None,
    db: AsyncSession = Depends(get_db),
) -> ObservabilitySourceList:
    """List registered sources (optionally filtered)."""
    stmt = select(ObservabilitySource).order_by(ObservabilitySource.name)
    if project_id:
        stmt = stmt.where(ObservabilitySource.project_id == project_id)
    if source_type:
        stmt = stmt.where(ObservabilitySource.source_type == source_type)
    sources = (await db.execute(stmt)).scalars().all()
    return ObservabilitySourceList(
        items=[ObservabilitySourceResponse.model_validate(s) for s in sources],
        total=len(sources),
    )


@router.get("/sources/{source_id}", response_model=ObservabilitySourceResponse)
async def get_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ObservabilitySource:
    source = await db.get(ObservabilitySource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.patch("/sources/{source_id}", response_model=ObservabilitySourceResponse)
async def update_source(
    source_id: uuid.UUID,
    data: ObservabilitySourceUpdate,
    db: AsyncSession = Depends(get_db),
) -> ObservabilitySource:
    """Update mutable source fields (status/description)."""
    source = await db.get(ObservabilitySource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    updates = data.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(source, key, value)
    await db.flush()
    await db.refresh(source)
    return source


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Unregister a source. Missing source is treated as already gone."""
    source = await db.get(ObservabilitySource, source_id)
    if source:
        await db.delete(source)
        await db.flush()


# ---------------------------------------------------------------------------
# Async queue path
# ---------------------------------------------------------------------------
@router.post("/queue", status_code=202)
async def enqueue_batch(
    envelope: _BatchEnvelope,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Enqueue a batch for background processing (§37).

    Returns 202 when accepted into the queue. If the broker is unreachable the
    API degrades to the synchronous path.
    """
    for e in envelope.events:
        _reject_secrets(e.payload)

    job = make_job(
        kind="event",
        payload={
            "project_id": str(envelope.project_id),
            "environment_id": str(envelope.environment_id)
            if envelope.environment_id
            else None,
            "source_id": envelope.source_id,
            "events": [e.model_dump(mode="json") for e in envelope.events],
        },
    )
    queue = IngestionQueue()
    try:
        await queue.push(job)
    except QueueUnavailable:
        # Degrade to the synchronous path rather than dropping the data.
        raw_events = [
            RawObservabilityEvent(
                source_type=e.source_type,
                source_name=e.source_name,
                timestamp=e.timestamp,
                event_type=e.event_type,
                payload=e.payload,
                metadata=e.metadata,
            )
            for e in envelope.events
        ]
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db,
            project_id=envelope.project_id,
            environment_id=envelope.environment_id,
        )
        result = await pipeline.ingest_batch(raw_events, source_id=envelope.source_id)
        return JSONResponse(
            status_code=200,
            content={
                "queued": False,
                "reason": "broker_unavailable_processed_sync",
                **result.__dict__,
            },
        )
    return JSONResponse(
        status_code=202,
        content={"queued": True, "queue": QUEUE_NAME_HINT, "job_kind": "event"},
    )


# ---------------------------------------------------------------------------
# Batch ingestion of raw envelopes (§19, §44)
# ---------------------------------------------------------------------------
class _InboundEvent(BaseSchema):
    """A single event inside a batch envelope."""

    source_type: str = Field(..., min_length=1, max_length=100)
    source_name: str = Field(..., min_length=1, max_length=255)
    timestamp: datetime
    event_type: str = Field(..., min_length=1, max_length=50)
    payload: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class _BatchEnvelope(BaseSchema):
    """Raw events batch — the async ingestion entry point."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    source_id: Optional[str] = None
    events: List[_InboundEvent] = Field(..., min_length=1, max_length=500)


class _BatchResponse(BaseSchema):
    accepted: int
    duplicates: int
    failed: int
    failures: List[str] = Field(default_factory=list)


@router.post("/bulk", response_model=_BatchResponse)
async def ingest_batch(
    envelope: _BatchEnvelope,
    db: AsyncSession = Depends(get_db),
) -> _BatchResponse:
    """Ingest a batch of raw events through the pipeline (sync path).

    For high-volume async ingestion use the queue-based path
    POST /ingestion/queue instead.
    """
    raw_events = [
        RawObservabilityEvent(
            source_type=e.source_type,
            source_name=e.source_name,
            timestamp=e.timestamp,
            event_type=e.event_type,
            payload=e.payload,
            metadata=e.metadata,
        )
        for e in envelope.events
    ]
    # Boundary secret rejection on the raw payloads.
    for e in envelope.events:
        _reject_secrets(e.payload)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=envelope.project_id,
        environment_id=envelope.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events, source_id=envelope.source_id)
    # Phase 3 §20: queue detection for the accepted batch (ids only).
    if result.accepted:
        await enqueue_anomaly_detect(
            project_id=envelope.project_id, environment_id=envelope.environment_id
        )
    return _BatchResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        failed=result.failed,
        failures=result.failures,
    )


# ---------------------------------------------------------------------------
# Configuration change events (§14)
# ---------------------------------------------------------------------------
@router.post(
    "/config-changes", response_model=ConfigurationChangeEventResponse, status_code=201
)
async def create_config_change(
    data: ConfigurationChangeEventCreate,
    db: AsyncSession = Depends(get_db),
) -> ConfigurationChangeEvent:
    _reject_secrets(data.metadata_)
    event = ConfigurationChangeEvent(**data.model_dump())
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return event


@router.get("/config-changes", response_model=ConfigurationChangeEventList)
async def list_config_changes(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ConfigurationChangeEventList:
    stmt = select(ConfigurationChangeEvent)
    count_stmt = select(func.count(ConfigurationChangeEvent.id))
    if project_id:
        stmt = stmt.where(ConfigurationChangeEvent.project_id == project_id)
        count_stmt = count_stmt.where(ConfigurationChangeEvent.project_id == project_id)
    if environment_id:
        stmt = stmt.where(ConfigurationChangeEvent.environment_id == environment_id)
        count_stmt = count_stmt.where(
            ConfigurationChangeEvent.environment_id == environment_id
        )
    if component_id:
        stmt = stmt.where(ConfigurationChangeEvent.component_id == component_id)
        count_stmt = count_stmt.where(
            ConfigurationChangeEvent.component_id == component_id
        )
    if start_time:
        stmt = stmt.where(ConfigurationChangeEvent.timestamp >= start_time)
        count_stmt = count_stmt.where(ConfigurationChangeEvent.timestamp >= start_time)
    if end_time:
        stmt = stmt.where(ConfigurationChangeEvent.timestamp <= end_time)
        count_stmt = count_stmt.where(ConfigurationChangeEvent.timestamp <= end_time)

    total = (await db.execute(count_stmt)).scalar() or 0
    stmt = (
        stmt.order_by(ConfigurationChangeEvent.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()
    return ConfigurationChangeEventList(
        items=[ConfigurationChangeEventResponse.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# ---------------------------------------------------------------------------
# Health check events (§15)
# ---------------------------------------------------------------------------
@router.post("/health-checks", response_model=HealthCheckEventResponse, status_code=201)
async def create_health_check(
    data: HealthCheckEventCreate,
    db: AsyncSession = Depends(get_db),
) -> HealthCheckEvent:
    _reject_secrets(data.metadata_)
    event = HealthCheckEvent(**data.model_dump())
    db.add(event)
    await db.flush()
    await db.refresh(event)
    # Phase 3 §15/§20: health transitions are detection input.
    await enqueue_anomaly_detect(
        project_id=event.project_id, environment_id=event.environment_id
    )
    return event


@router.get("/health-checks", response_model=HealthCheckEventList)
async def list_health_checks(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    status: Optional[HealthStatus] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> HealthCheckEventList:
    stmt = select(HealthCheckEvent)
    count_stmt = select(func.count(HealthCheckEvent.id))
    if project_id:
        stmt = stmt.where(HealthCheckEvent.project_id == project_id)
        count_stmt = count_stmt.where(HealthCheckEvent.project_id == project_id)
    if environment_id:
        stmt = stmt.where(HealthCheckEvent.environment_id == environment_id)
        count_stmt = count_stmt.where(HealthCheckEvent.environment_id == environment_id)
    if component_id:
        stmt = stmt.where(HealthCheckEvent.component_id == component_id)
        count_stmt = count_stmt.where(HealthCheckEvent.component_id == component_id)
    if status:
        stmt = stmt.where(HealthCheckEvent.status == status)
        count_stmt = count_stmt.where(HealthCheckEvent.status == status)
    if start_time:
        stmt = stmt.where(HealthCheckEvent.timestamp >= start_time)
        count_stmt = count_stmt.where(HealthCheckEvent.timestamp >= start_time)
    if end_time:
        stmt = stmt.where(HealthCheckEvent.timestamp <= end_time)
        count_stmt = count_stmt.where(HealthCheckEvent.timestamp <= end_time)

    total = (await db.execute(count_stmt)).scalar() or 0
    stmt = (
        stmt.order_by(HealthCheckEvent.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()
    return HealthCheckEventList(
        items=[HealthCheckEventResponse.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# ---------------------------------------------------------------------------
# Ingestion stats & dead-letter inspection (§45)
# ---------------------------------------------------------------------------
@router.get("/stats", response_model=IngestionSummary)
async def ingestion_stats(
    project_id: Optional[uuid.UUID] = None,
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = IngestionStatsService(db)
    return await service.source_summary(project_id=project_id, days=days)


@router.get("/sources-health", response_model=List[IngestionSourceHealth])
async def sources_health(
    project_id: uuid.UUID,
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    service = IngestionStatsService(db)
    return await service.top_sources(project_id=project_id, days=days)


@router.get("/dead-letter", response_model=List[IngestionFailureResponse])
async def list_dead_letter(
    project_id: Optional[uuid.UUID] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[IngestionFailure]:
    service = IngestionStatsService(db)
    return await service.dead_letter_list(project_id=project_id, limit=limit)


# ---------------------------------------------------------------------------
# Webhook ingestion foundation (§47)
# ---------------------------------------------------------------------------
class _WebhookEnvelope(BaseSchema):
    """Webhook-delivered event. Signature verification is out of scope here —
    the foundation records, validates, and *queues* the envelope for async
    processing. A project (or a registered source that resolves to one) is
    required so the event can be routed; unknown sources 404."""

    event_type: EventType
    timestamp: datetime
    payload: dict = Field(default_factory=dict)
    source_name: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    project_id: Optional[uuid.UUID] = None
    source_id: Optional[str] = None


class _WebhookResponse(BaseSchema):
    received: bool
    event_type: str
    queued: bool
    redacted: bool


async def _enqueue_webhook(
    envelope: _WebhookEnvelope,
    *,
    db: AsyncSession,
    source_id_hint: Optional[str] = None,
) -> _WebhookResponse:
    """Route a webhook envelope into the ingestion pipeline (§47).

    Resolution: explicit project_id wins; otherwise a registered source's
    project is used. The event is handed to the async queue; if the broker is
    unreachable it degrades to the synchronous pipeline rather than dropping.
    """
    _reject_secrets(envelope.payload)
    _reject_secrets(envelope.metadata)

    project_id = envelope.project_id
    source_name = envelope.source_name
    source_type = "WEBHOOK"

    source_id = source_id_hint or envelope.source_id
    if source_id:
        # Validate the source identifier before touching the DB: a malformed
        # non-UUID string must be a clean 404, never a 500 from the binder.
        try:
            parsed_source_id = uuid.UUID(str(source_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=404, detail=f"Source '{source_id}' not found"
            )
        source = await db.get(ObservabilitySource, parsed_source_id)
        if source is None:
            raise HTTPException(
                status_code=404, detail=f"Source '{source_id}' not found"
            )
        project_id = project_id or source.project_id
        source_name = source_name or source.name
        source_type = (
            source.source_type.value
            if hasattr(source.source_type, "value")
            else str(source.source_type)
        )

    if project_id is None:
        raise HTTPException(
            status_code=422,
            detail="Webhook requires a project_id or a registered source_id",
        )

    # use_enum_values=True means ``envelope.event_type`` is already the enum's
    # string value; ``str()`` is safe for both enum instances and plain strings.
    event_type = (
        str(envelope.event_type.value)
        if hasattr(envelope.event_type, "value")
        else str(envelope.event_type)
    )

    job = make_job(
        kind="event",
        payload={
            "project_id": str(project_id),
            "environment_id": None,
            "source_id": source_id,
            "events": [
                {
                    "source_type": source_type,
                    "source_name": source_name or "webhook",
                    "timestamp": envelope.timestamp.isoformat(),
                    "event_type": event_type,
                    "payload": envelope.payload,
                    "metadata": envelope.metadata,
                }
            ],
        },
    )

    queue = IngestionQueue()
    try:
        await queue.push(job)
    except QueueUnavailable:
        # Degrade to the synchronous path rather than dropping the data.
        from app.core.sources import RawObservabilityEvent
        from app.services.ingestion import IngestionPipeline

        raw_events = [
            RawObservabilityEvent(
                source_type=source_type,
                source_name=source_name or "webhook",
                timestamp=envelope.timestamp,
                event_type=event_type,
                payload=envelope.payload,
                metadata=envelope.metadata,
            )
        ]
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db,
            project_id=project_id,
            environment_id=None,
        )
        await pipeline.ingest_batch(raw_events, source_id=source_id)

    return _WebhookResponse(
        received=True,
        event_type=event_type,
        queued=True,
        redacted=False,
    )


@router.post("/webhook", response_model=_WebhookResponse, status_code=202)
async def ingest_webhook(
    envelope: _WebhookEnvelope,
    db: AsyncSession = Depends(get_db),
) -> _WebhookResponse:
    """Accept a webhook-delivered event (generic path).

    Requires ``project_id`` or ``source_id`` to route; secrets are rejected at
    the boundary; the event is queued for async processing (Phase 1 §47).
    """
    return await _enqueue_webhook(envelope, db=db)


@router.post("/webhooks/{source_id}", response_model=_WebhookResponse, status_code=202)
async def ingest_webhook_for_source(
    source_id: str,
    envelope: _WebhookEnvelope,
    db: AsyncSession = Depends(get_db),
) -> _WebhookResponse:
    """Accept a webhook for a specific registered source (§47).

    Source must exist; its project is used for routing. Signature verification
    and replay protection are documented future work for the webhook pass.
    """
    return await _enqueue_webhook(envelope, db=db, source_id_hint=source_id)


# ---------------------------------------------------------------------------
# Data retention (§22)
# ---------------------------------------------------------------------------
class _RetentionResult(BaseSchema):
    table: str
    deleted: int
    threshold_days: int


class _RetentionSummary(BaseSchema):
    results: list[_RetentionResult]
    total_deleted: int


@router.get("/retention/policy")
async def get_retention_policy() -> dict:
    """Return the current retention policy (table → days)."""

    # Return static policy — no db needed
    from app.core.config import get_settings

    s = get_settings()
    return {
        "observability_events": s.RETENTION_EVENTS,
        "log_records": s.RETENTION_LOGS,
        "metric_records": s.RETENTION_METRICS,
        "traces": s.RETENTION_TRACES,
        "spans": s.RETENTION_TRACES,
        "configuration_change_events": s.RETENTION_LOGS,
        "health_check_events": s.RETENTION_LOGS,
        "ingestion_failures": 30,
    }


@router.get("/retention/preview")
async def retention_preview(
    db: AsyncSession = Depends(get_db),
) -> _RetentionSummary:
    """Preview what would be deleted (dry-run)."""
    from app.services.retention import RetentionService

    service = RetentionService(db)
    summary = await service.preview()
    return _RetentionSummary(
        results=[
            _RetentionResult(
                table=r.table,
                deleted=r.deleted,
                threshold_days=r.threshold_days,
            )
            for r in summary.results
        ],
        total_deleted=summary.total_deleted,
    )


@router.post("/retention/sweep", response_model=_RetentionSummary)
async def retention_sweep(
    db: AsyncSession = Depends(get_db),
) -> _RetentionSummary:
    """Execute the retention sweep — delete old data."""
    from app.services.retention import RetentionService

    service = RetentionService(db)
    summary = await service.run_sweep()
    return _RetentionSummary(
        results=[
            _RetentionResult(
                table=r.table,
                deleted=r.deleted,
                threshold_days=r.threshold_days,
            )
            for r in summary.results
        ],
        total_deleted=summary.total_deleted,
    )


# ---------------------------------------------------------------------------
# Trace cross-reference validation (§20)
# ---------------------------------------------------------------------------
class _OrphanSpan(BaseSchema):
    span_id: str
    trace_id: str
    parent_span_id: str
    operation: Optional[str] = None


class _TraceValidationResult(BaseSchema):
    trace_id: str
    total_spans: int
    orphan_spans: list[_OrphanSpan]
    missing_trace_record: bool
    duration_anomalies: list[str]
    is_valid: bool


class _ValidationSummary(BaseSchema):
    traces_checked: int
    valid_traces: int
    traces_with_orphans: int
    total_orphan_spans: int
    missing_trace_records: int
    duration_anomalies: int


@router.get("/trace-validation/{trace_id}", response_model=_TraceValidationResult)
async def validate_trace(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> _TraceValidationResult:
    """Validate a single trace for orphan spans and integrity issues."""
    from app.services.trace_validator import TraceValidator

    validator = TraceValidator(db)
    result = await validator.validate_trace(trace_id)
    return _TraceValidationResult(
        trace_id=result.trace_id,
        total_spans=result.total_spans,
        orphan_spans=[
            _OrphanSpan(
                span_id=o.span_id,
                trace_id=o.trace_id,
                parent_span_id=o.parent_span_id,
                operation=o.operation,
            )
            for o in result.orphan_spans
        ],
        missing_trace_record=result.missing_trace_record,
        duration_anomalies=result.duration_anomalies,
        is_valid=result.is_valid,
    )


@router.get("/trace-validation/{project_id}/project", response_model=_ValidationSummary)
async def validate_project_traces(
    project_id: uuid.UUID,
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> _ValidationSummary:
    """Validate all traces for a project (bounded by limit)."""
    from app.services.trace_validator import TraceValidator

    validator = TraceValidator(db)
    summary = await validator.validate_project(project_id, limit=limit)
    return _ValidationSummary(
        traces_checked=summary.traces_checked,
        valid_traces=summary.valid_traces,
        traces_with_orphans=summary.traces_with_orphans,
        total_orphan_spans=summary.total_orphan_spans,
        missing_trace_records=summary.missing_trace_records,
        duration_anomalies=summary.duration_anomalies,
    )


@router.get("/orphan-spans/{project_id}", response_model=list[_OrphanSpan])
async def find_orphan_spans(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list:
    """Find all orphan spans in a project."""
    from app.services.trace_validator import TraceValidator

    validator = TraceValidator(db)
    orphans = await validator.find_orphan_spans(project_id, limit=limit)
    return [
        _OrphanSpan(
            span_id=o.span_id,
            trace_id=o.trace_id,
            parent_span_id=o.parent_span_id,
            operation=o.operation,
        )
        for o in orphans
    ]
