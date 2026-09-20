"""ARGUS OTLP Ingestion Routes.

Accepts OpenTelemetry protocol (OTLP/JSON) payloads for traces, logs, and
metrics and feeds them through the standard ARGUS ingestion pipeline.

Phase 1 §17: OTLP ingestion adapter.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.sources import MockObservabilitySource
from app.schemas.base import BaseSchema
from app.services.ingestion import IngestionPipeline
from app.services.otlp_adapter import OTLPAdapter
from app.services.queue import enqueue_anomaly_detect, enqueue_graph_extract

router = APIRouter(prefix="/otlp", tags=["OTLP"])

# Shared adapter — stateless, safe to reuse.
_adapter = OTLPAdapter()


class _OTLPResponse(BaseSchema):
    """Standard OTLP ingest response."""

    accepted: int
    duplicates: int
    failed: int


class _OTLPRequestBase(BaseSchema):
    """Accept OTLP protojson camelCase top-level keys alongside snake_case.

    Real OTLP collectors emit ``resourceSpans`` / ``resourceLogs`` /
    ``resourceMetrics`` (lowerCamelCase); the ARGUS-native form uses snake_case.
    Both are accepted (§17: “protojson camelCase and snake_case accepted”).
    ``project_id`` / ``environment_id`` are ARGUS multi-tenancy extensions.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        from_attributes=True,
        use_enum_values=True,
    )


class _OTLPTracesRequest(_OTLPRequestBase):
    """OTLP ``ExportTraceServiceRequest`` JSON body."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    resource_spans: list[dict] = Field(default_factory=list)


class _OTLPLogsRequest(_OTLPRequestBase):
    """OTLP ``ExportLogsServiceRequest`` JSON body."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    resource_logs: list[dict] = Field(default_factory=list)


class _OTLPMetricsRequest(_OTLPRequestBase):
    """OTLP ``ExportMetricsServiceRequest`` JSON body."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    resource_metrics: list[dict] = Field(default_factory=list)


@router.post("/v1/traces", response_model=_OTLPResponse)
async def ingest_otlp_traces(
    body: _OTLPTracesRequest,
    db: AsyncSession = Depends(get_db),
) -> _OTLPResponse:
    """Accept OTLP trace spans (JSON transport).

    The ``resource_spans`` array follows the OTLP spec:
    ``[{ resource: { attributes: [...] }, spans: [...] }]``
    """
    raw_events = _adapter.convert_spans(
        {"resource_spans": body.resource_spans},
        source_name="otlp",
    )
    if not raw_events:
        return _OTLPResponse(accepted=0, duplicates=0, failed=0)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=body.project_id,
        environment_id=body.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events)
    # Post-ingest hook (Phase 2 §38): queue graph extraction for accepted spans.
    if result.accepted:
        await enqueue_graph_extract(
            project_id=body.project_id, environment_id=body.environment_id
        )
        # Phase 3 §20: detection shares the same post-ingest boundary.
        await enqueue_anomaly_detect(
            project_id=body.project_id, environment_id=body.environment_id
        )
    return _OTLPResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        failed=result.failed,
    )


@router.post("/v1/logs", response_model=_OTLPResponse)
async def ingest_otlp_logs(
    body: _OTLPLogsRequest,
    db: AsyncSession = Depends(get_db),
) -> _OTLPResponse:
    """Accept OTLP log records (JSON transport)."""
    raw_events = _adapter.convert_logs(
        {"resource_logs": body.resource_logs},
        source_name="otlp",
    )
    if not raw_events:
        return _OTLPResponse(accepted=0, duplicates=0, failed=0)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=body.project_id,
        environment_id=body.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events)
    # Phase 3 §20: logs feed log-pattern and error-rate detection.
    if result.accepted:
        await enqueue_anomaly_detect(
            project_id=body.project_id, environment_id=body.environment_id
        )
    return _OTLPResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        failed=result.failed,
    )


@router.post("/v1/metrics", response_model=_OTLPResponse)
async def ingest_otlp_metrics(
    body: _OTLPMetricsRequest,
    db: AsyncSession = Depends(get_db),
) -> _OTLPResponse:
    """Accept OTLP metric data (JSON transport)."""
    raw_events = _adapter.convert_metrics(
        {"resource_metrics": body.resource_metrics},
        source_name="otlp",
    )
    if not raw_events:
        return _OTLPResponse(accepted=0, duplicates=0, failed=0)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=body.project_id,
        environment_id=body.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events)
    # Phase 3 §20: metrics feed threshold/deviation/z-score detection.
    if result.accepted:
        await enqueue_anomaly_detect(
            project_id=body.project_id, environment_id=body.environment_id
        )
    return _OTLPResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        failed=result.failed,
    )
