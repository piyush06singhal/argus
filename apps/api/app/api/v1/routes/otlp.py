"""ARGUS OTLP Ingestion Routes.

Accepts OpenTelemetry protocol (OTLP/JSON) payloads for traces, logs, and
metrics and feeds them through the standard ARGUS ingestion pipeline.

Phase 1 §17: OTLP ingestion adapter.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.edge import require_auth_context
from app.core.security import enforce_ingest_scope
from app.core.sources import MockObservabilitySource
from app.schemas.base import BaseSchema
from app.services.ingestion import IngestionPipeline
from app.services.otlp_adapter import OTLPAdapter
from app.services.queue import enqueue_anomaly_detect, enqueue_graph_extract

router = APIRouter(prefix="/otlp", tags=["OTLP"])

# Shared adapter — stateless, safe to reuse.
_adapter = OTLPAdapter()


def _scope_to_credential(body_project_id: Optional[uuid.UUID]) -> uuid.UUID:
    """Return the project this ingestion is allowed to write (W1).

    The credential decides the scope, not the body: an ingest token's project
    must match ``project_id``, a scoped API token must hold the grant, and an
    admin token passes through. Applied identically by all three OTLP verbs.

    ``projectId`` may be **omitted**, and that is the shape a stock OpenTelemetry
    exporter actually emits: the exporter serialises an
    ``ExportTraceServiceRequest`` and has no way to add a top-level ARGUS field,
    so requiring one made the documented "point your collector here" path
    impossible. When it is absent the project is resolved from the credential
    instead — which is stricter, not looser, because the caller no longer names
    its own destination.

    Fail closed: the context is required, so a guard that cannot see it refuses
    the request instead of silently allowing a cross-project write.
    """
    auth = require_auth_context()
    if body_project_id is None:
        return _credential_project(auth)
    enforce_ingest_scope(auth, body_project_id)
    return body_project_id


def _credential_project(auth) -> uuid.UUID:
    """The single project a credential implies, when the body names none.

    * an ingest token (or a token holding exactly one grant) implies its project;
    * an unscoped ADMIN token implies nothing — it must say which project it
      means, because guessing would write telemetry into an arbitrary tenant.
    """
    project_ids = auth.project_ids
    if auth.is_admin or not project_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "resourceSpans/resourceLogs/resourceMetrics were sent without "
                "projectId, and this credential does not imply a single "
                "project. Send projectId, or use a per-source ingest token."
            ),
        )
    if len(project_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "projectId is required: this credential is granted more than "
                "one project, so the destination cannot be inferred."
            ),
        )
    return next(iter(project_ids))


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

    project_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    resource_spans: list[dict] = Field(default_factory=list)


class _OTLPLogsRequest(_OTLPRequestBase):
    """OTLP ``ExportLogsServiceRequest`` JSON body."""

    project_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    resource_logs: list[dict] = Field(default_factory=list)


class _OTLPMetricsRequest(_OTLPRequestBase):
    """OTLP ``ExportMetricsServiceRequest`` JSON body."""

    project_id: Optional[uuid.UUID] = None
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
    project_id = _scope_to_credential(body.project_id)
    raw_events = _adapter.convert_spans(
        {"resource_spans": body.resource_spans},
        source_name="otlp",
    )
    if not raw_events:
        return _OTLPResponse(accepted=0, duplicates=0, failed=0)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=project_id,
        environment_id=body.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events)
    # Post-ingest hook (Phase 2 §38): queue graph extraction for accepted spans.
    if result.accepted:
        await enqueue_graph_extract(
            project_id=project_id, environment_id=body.environment_id
        )
        # Phase 3 §20: detection shares the same post-ingest boundary.
        await enqueue_anomaly_detect(
            project_id=project_id, environment_id=body.environment_id
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
    project_id = _scope_to_credential(body.project_id)
    raw_events = _adapter.convert_logs(
        {"resource_logs": body.resource_logs},
        source_name="otlp",
    )
    if not raw_events:
        return _OTLPResponse(accepted=0, duplicates=0, failed=0)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=project_id,
        environment_id=body.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events)
    # Phase 3 §20: logs feed log-pattern and error-rate detection.
    if result.accepted:
        await enqueue_anomaly_detect(
            project_id=project_id, environment_id=body.environment_id
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
    project_id = _scope_to_credential(body.project_id)
    raw_events = _adapter.convert_metrics(
        {"resource_metrics": body.resource_metrics},
        source_name="otlp",
    )
    if not raw_events:
        return _OTLPResponse(accepted=0, duplicates=0, failed=0)

    pipeline = IngestionPipeline(
        source=MockObservabilitySource(),
        db=db,
        project_id=project_id,
        environment_id=body.environment_id,
    )
    result = await pipeline.ingest_batch(raw_events)
    # Phase 3 §20: metrics feed threshold/deviation/z-score detection.
    if result.accepted:
        await enqueue_anomaly_detect(
            project_id=project_id, environment_id=body.environment_id
        )
    return _OTLPResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        failed=result.failed,
    )
