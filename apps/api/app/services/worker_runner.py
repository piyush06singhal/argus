"""ARGUS Production Worker Wiring (Phase 1 §41–§42).

The API surface enqueues typed jobs (see ``app/services/queue.py::make_job``).
This module binds the queue worker to the real ingestion pipeline so the app
lifespan (``app/main.py``) can start and stop it alongside the server.

This file is deliberately thin:
  * ``IngestionWorker`` (queue.py) owns dequeue / retry / backoff / dead-letter.
  * ``IngestionPipeline`` (ingestion.py) owns normalization / persistence.
This module owns only the connection between the two: translating a queued job
envelope into pipeline input, and applying the same secret-rejection guardrail
the synchronous API boundary enforces (§46).

Job envelope (kind="event") produced by the ingestion routes:
    {
      "kind": "event",
      "payload": {
        "project_id":   "<uuid>",
        "environment_id": "<uuid>" | None,
        "source_id":    "<uuid>" | None,
        "events": [
          {
            "source_type": "WEBHOOK" | ...,
            "source_name": "..." ,
            "timestamp":   "<iso8601>",
            "event_type":  "SYSTEM_EVENT" | ...,
            "payload":     {...},
            "metadata":    {...},
          },
        ],
      },
      "_retries": 0,
    }

Unknown job kinds are raised (not silently dropped) so they flow through the
worker's bounded retry and land in the dead-letter table with an explicit
message — nothing is dropped without a record.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.sources import MockObservabilitySource, RawObservabilityEvent
from app.models.graph import GraphEdgeSource
from app.models.system import SystemComponent
from app.services.ingestion import IngestionPipeline
from app.services.queue import IngestionWorker, PermanentJobError

logger = logging.getLogger(__name__)
settings = get_settings()

# Mirrors the API-boundary guardrail (§46): known secret keys are never
# admitted. The async path must apply the same rule so a job enqueued by any
# producer still cannot leak secrets into the domain model.
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


def _reject_secrets(payload: Optional[dict]) -> None:
    """Raise on any payload carrying a known secret key (§46)."""
    if not payload:
        return
    for key in payload:
        lower = str(key).lower()
        if any(fragment in lower for fragment in _FORBIDDEN_PAYLOAD_KEYS):
            raise ValueError(
                f"Payload key '{key}' is not allowed (secrets are never ingested)"
            )


def _as_uuid(value: Any, *, field: str) -> Optional[uuid_mod.UUID]:
    """Parse a UUID job field cleanly, so malformed jobs fail loudly (→ dead-letter)."""
    if value is None or value == "":
        return None
    try:
        return uuid_mod.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as e:
        raise ValueError(f"{field} is not a valid UUID: {value!r}") from e


def _as_datetime(value: Any, *, field: str) -> datetime:
    """Parse an ISO-8601 job timestamp cleanly."""
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError, AttributeError) as e:
        raise ValueError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from e


async def process_event_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str,
    payload: dict[str, Any],
) -> int:
    """Process one queued ingestion job through the pipeline.

    Returns the number of accepted events. Raises on validation or persistence
    errors so the worker's bounded retry → dead-letter path records them.
    Dispatches on ``kind``: ``event`` (Phase 1 ingestion) and ``graph_extract``
    (Phase 2 knowledge-graph extraction). Unknown kinds raise so they land in
    the dead-letter table with an explicit message.
    """
    if kind == "graph_extract":
        await process_graph_extract_job(session_factory, payload=payload)
        return 0
    if kind == "anomaly_detect":
        await process_anomaly_detect_job(session_factory, payload=payload)
        return 0
    if kind == "incident_correlate":
        await process_incident_correlate_job(session_factory, payload=payload)
        return 0
    if kind == "reproduction_run":
        await process_reproduction_run_job(session_factory, payload=payload)
        return 0
    if kind == "reliability_forecast":
        await process_reliability_forecast_job(session_factory, payload=payload)
        return 0
    if kind == "reliability_evaluate":
        await process_reliability_evaluate_job(session_factory, payload=payload)
        return 0
    if kind != "event":
        raise NotImplementedError(
            f"No pipeline provisioned for queue job kind={kind!r} (job dead-lettered)"
        )

    events = payload.get("events") or []
    if not events:
        logger.warning("Empty events list in queued job; nothing to ingest")
        return 0

    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    if project_id is None:
        raise ValueError("Queued job missing required project_id")
    environment_id = _as_uuid(payload.get("environment_id"), field="environment_id")
    source_id = payload.get("source_id")

    raw_events: list[RawObservabilityEvent] = []
    for raw in events:
        _reject_secrets(raw.get("payload"))
        _reject_secrets(raw.get("metadata"))
        raw_events.append(
            RawObservabilityEvent(
                source_type=str(raw.get("source_type", "WEBHOOK")),
                source_name=str(raw.get("source_name", "webhook")),
                timestamp=_as_datetime(raw.get("timestamp"), field="timestamp"),
                event_type=str(raw.get("event_type", "SYSTEM_EVENT")),
                payload=raw.get("payload") or {},
                metadata=raw.get("metadata") or {},
            )
        )

    async with session_factory() as session:
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=session,
            project_id=project_id,
            environment_id=environment_id,
        )
        # ingest_batch commits internally; the session must not be committed twice.
        result = await pipeline.ingest_batch(raw_events, source_id=source_id)

    if result.failed:
        logger.warning(
            "Queued batch: accepted=%d duplicates=%d failed=%d failures=%s",
            result.accepted,
            result.duplicates,
            result.failed,
            result.failures,
        )
    else:
        logger.info(
            "Queued batch: accepted=%d duplicates=%d",
            result.accepted,
            result.duplicates,
        )
    return result.accepted


async def process_graph_extract_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
) -> dict[str, int]:
    """Process a ``graph_extract`` job: telemetry → knowledge graph (§38).

    Runs the Phase 2 extraction pipeline over persisted data for one project:
    trace-span edges, log reference edges, deployment edges, repository
    IMPLEMENTS edges, and container edges, then a reconciliation pass (which
    also runs the stale sweep and the discovery scan). Idempotent — repeated
    jobs only refresh ``last_seen_at``/evidence metadata.

    Returns summary counts. Raises on persistence errors so the worker's
    bounded retry → dead-letter path records them.
    """
    from sqlalchemy import select

    from app.models.observability import EventType, ObservabilityEvent, SpanRecord
    from app.models.project import SoftwareProject
    from app.services.graph_extractor import GraphExtractor
    from app.services.graph_reconciler import GraphReconciler
    from app.services.graph_registry import ComponentRegistry

    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    if project_id is None:
        raise PermanentJobError("graph_extract job missing required project_id")
    environment_id = _as_uuid(payload.get("environment_id"), field="environment_id")

    async with session_factory() as session:
        # A job for a project that no longer exists can never succeed — its
        # enqueueing race (project deleted between POST and job run) means
        # retrying is futile. Dead-letter immediately and free the queue.
        project_exists = await session.scalar(
            select(
                select(SoftwareProject.id)
                .where(SoftwareProject.id == project_id)
                .exists()
            )
        )
        if not project_exists:
            raise PermanentJobError(f"SoftwareProject {project_id} not found")

        registry = ComponentRegistry(session)
        reconciler = GraphReconciler(session, registry)
        extractor = GraphExtractor(session, registry, reconciler=reconciler)

        span_limit = settings.GRAPH_EXTRACT_SPAN_LIMIT
        span_stmt = select(SpanRecord).where(SpanRecord.project_id == project_id)
        if environment_id is not None:
            span_stmt = span_stmt.join(
                SystemComponent, SystemComponent.id == SpanRecord.component_id
            ).where(SystemComponent.environment_id == environment_id)
        span_stmt = span_stmt.order_by(SpanRecord.start_time.desc()).limit(span_limit)
        spans = (await session.execute(span_stmt)).scalars().all()

        # OTLP-ingested spans live as TRACE-typed ObservabilityEvents whose
        # payloads carry the parent chain — feed both evidence shapes.
        event_stmt = (
            select(ObservabilityEvent)
            .where(
                ObservabilityEvent.project_id == project_id,
                ObservabilityEvent.event_type == EventType.TRACE,
            )
            .order_by(ObservabilityEvent.timestamp.desc())
            .limit(span_limit)
        )
        if environment_id is not None:
            event_stmt = event_stmt.where(
                ObservabilityEvent.environment_id == environment_id
            )
        trace_events = (await session.execute(event_stmt)).scalars().all()

        # First-seen services in telemetry must become real components before
        # edge extraction, or their relationships are unattributable (§12–§13).
        attributed = await _ensure_telemetry_components(
            session,
            project_id=project_id,
            environment_id=environment_id,
            trace_events=list(trace_events),
        )
        if attributed:
            logger.info(
                "graph_extract attributed %d event(s) to components", attributed
            )

        trace_stats = await extractor.extract_trace_graph(
            project_id=project_id,
            environment_id=environment_id,
            spans=spans,
            events=trace_events,
        )
        endpoint_stats = await extractor.extract_endpoint_refs(
            project_id=project_id,
            environment_id=environment_id,
            spans=spans,
        )
        log_stats = await extractor.extract_log_references(
            project_id=project_id, environment_id=environment_id
        )
        deploy_stats = await extractor.extract_deployment_edges(
            project_id=project_id, environment_id=environment_id
        )
        repo_stats = await extractor.extract_repository_edges(project_id=project_id)
        await extractor.extract_project_container(project_id)

        reconcile_result = await reconciler.reconcile(
            project_id=project_id,
            environment_id=environment_id,
            run_source=GraphEdgeSource.TRACE,
        )
        await session.commit()

    summary = {
        "spans_seen": trace_stats.spans_seen,
        "edges_created": (
            trace_stats.edges_created
            + log_stats.edges_created
            + deploy_stats.edges_created
            + repo_stats.edges_created
        ),
        "edges_updated": (
            trace_stats.edges_updated
            + log_stats.edges_updated
            + deploy_stats.edges_updated
            + repo_stats.edges_updated
        ),
        "endpoints_recorded": endpoint_stats.endpoints_recorded,
        "reconcile_nodes_created": reconcile_result.nodes_created,
        "reconcile_edges_created": reconcile_result.edges_created,
        "reconcile_edges_marked_stale": reconcile_result.edges_marked_stale,
    }
    logger.info(
        "graph_extract project=%s env=%s edges_created=%d edges_updated=%d",
        project_id,
        environment_id,
        summary["edges_created"],
        summary["edges_updated"],
    )
    return summary


async def process_anomaly_detect_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process an ``anomaly_detect`` job: telemetry → anomalies (Phase 3 §20).

    Deterministic and idempotent: re-running over unchanged telemetry does not
    create duplicate anomalies (the fingerprint registry collapses repeats).
    Respects the ``ANOMALY_DETECTION_ENABLED`` master switch, and dead-letters
    immediately for a project that no longer exists — no retry churn.
    """
    from app.services.anomaly_detection import AnomalyDetectionService

    if not settings.ANOMALY_DETECTION_ENABLED:
        logger.info("anomaly_detect skipped (ANOMALY_DETECTION_ENABLED=false)")
        return {"skipped": "detection_disabled"}

    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    if project_id is None:
        raise PermanentJobError("anomaly_detect job missing required project_id")
    environment_id = _as_uuid(payload.get("environment_id"), field="environment_id")

    from sqlalchemy import select

    from app.models.project import SoftwareProject

    async with session_factory() as session:
        project_exists = await session.scalar(
            select(
                select(SoftwareProject.id)
                .where(SoftwareProject.id == project_id)
                .exists()
            )
        )
        if not project_exists:
            raise PermanentJobError(f"SoftwareProject {project_id} not found")

        service = AnomalyDetectionService(session)
        result = await service.run(project_id=project_id, environment_id=environment_id)
        await session.commit()

    summary = result.as_dict()
    logger.info(
        "anomaly_detect project=%s env=%s opened=%d updated=%d suppressed=%d",
        project_id,
        environment_id,
        result.anomalies_opened,
        result.anomalies_updated,
        result.suppressed,
    )
    # Chain correlation after detection: grouping must see the fresh anomalies.
    # A failure to enqueue is degraded, never fatal — the sweep re-runs both.
    from app.services.queue import enqueue_incident_correlate

    await enqueue_incident_correlate(
        project_id=project_id, environment_id=environment_id
    )
    return summary


async def process_incident_correlate_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process an ``incident_correlate`` job: anomalies → incidents (Phase 3 §21).

    Idempotent — incidents are fingerprinted and their timeline/evidence rows are
    deduplicated, so re-running never duplicates an incident or its evidence.
    """
    from app.services.incident_manager import IncidentManager

    if not settings.ANOMALY_DETECTION_ENABLED:
        logger.info("incident_correlate skipped (ANOMALY_DETECTION_ENABLED=false)")
        return {"skipped": "detection_disabled"}

    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    if project_id is None:
        raise PermanentJobError("incident_correlate job missing required project_id")
    environment_id = _as_uuid(payload.get("environment_id"), field="environment_id")

    from sqlalchemy import select

    from app.models.project import SoftwareProject

    async with session_factory() as session:
        exists = await session.scalar(
            select(
                select(SoftwareProject.id)
                .where(SoftwareProject.id == project_id)
                .exists()
            )
        )
        if not exists:
            raise PermanentJobError(f"SoftwareProject {project_id} not found")

        manager = IncidentManager(session)
        result = await manager.process_scope(
            project_id=project_id, environment_id=environment_id
        )
        await session.commit()

    summary = result.as_dict()
    logger.info(
        "incident_correlate project=%s env=%s created=%d updated=%d linked=%d",
        project_id,
        environment_id,
        result.incidents_created,
        result.incidents_updated,
        result.anomalies_linked,
    )
    return summary


async def process_reproduction_run_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process a ``reproduction_run`` job: execute one experiment (Phase 5 §53).

    Idempotent by construction: :meth:`ReproductionOrchestrator.run_experiment`
    returns immediately when the experiment is already terminal, so a job that is
    retried after a worker crash cannot re-run a finished experiment or leave a
    second set of artifacts behind.

    A missing project is a *permanent* failure (no retry can fix it). A sandbox
    failure is not: the orchestrator records it on the experiment and returns, so
    the job itself succeeds — the experiment's state is the error channel, not
    the queue's.
    """
    from sqlalchemy import select

    from app.models.project import SoftwareProject
    from app.services.reproduction_orchestrator import (
        OrchestrationError,
        ReproductionOrchestrator,
    )

    experiment_id = _as_uuid(payload.get("experiment_id"), field="experiment_id")
    if experiment_id is None:
        raise PermanentJobError("reproduction_run job missing required experiment_id")
    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    if project_id is None:
        raise PermanentJobError("reproduction_run job missing required project_id")

    async with session_factory() as session:
        exists = await session.scalar(
            select(
                select(SoftwareProject.id)
                .where(SoftwareProject.id == project_id)
                .exists()
            )
        )
        if not exists:
            raise PermanentJobError(f"SoftwareProject {project_id} not found")

    if not settings.REPRODUCTION_ENABLED:
        logger.info("reproduction_run skipped (REPRODUCTION_ENABLED=false)")
        return {"skipped": "reproduction_disabled"}

    orchestrator = ReproductionOrchestrator(session_factory)
    try:
        experiment = await orchestrator.run_experiment(experiment_id)
    except OrchestrationError as exc:
        # The experiment row itself could not be driven (missing plan, deleted
        # incident): permanent, because retrying reaches the same wall.
        raise PermanentJobError(str(exc)) from exc

    logger.info(
        "reproduction_run experiment=%s status=%s result=%s",
        experiment_id,
        experiment.status.value,
        experiment.result.value,
    )
    return {
        "experiment_id": str(experiment_id),
        "status": experiment.status.value,
        "result": experiment.result.value,
        "confidence": experiment.confidence,
    }


async def process_reliability_forecast_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process a ``reliability_forecast`` job (Phase 8 §57).

    Deterministic and idempotent: generation deduplicates per scope inside its
    refresh window, so re-running over unchanged telemetry refreshes the
    existing forecast rather than producing another row. Respects the
    ``RELIABILITY_FORECASTING_ENABLED`` master switch, and dead-letters a
    project that no longer exists instead of retrying forever.
    """
    from app.services.reliability_forecast_service import ReliabilityForecastService

    if not settings.RELIABILITY_FORECASTING_ENABLED:
        logger.info("reliability_forecast skipped (forecasting disabled)")
        return {"skipped": "forecasting_disabled"}

    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    if project_id is None:
        raise PermanentJobError("reliability_forecast job missing required project_id")
    environment_id = _as_uuid(payload.get("environment_id"), field="environment_id")

    from sqlalchemy import select

    from app.models.project import SoftwareProject

    async with session_factory() as session:
        exists = await session.scalar(
            select(
                select(SoftwareProject.id)
                .where(SoftwareProject.id == project_id)
                .exists()
            )
        )
        if not exists:
            raise PermanentJobError(f"SoftwareProject {project_id} not found")

        service = ReliabilityForecastService(session)
        result = await service.generate_for_project(
            project_id=project_id, environment_id=environment_id
        )
        await session.commit()

    summary = result.as_dict()
    logger.info(
        "reliability_forecast project=%s env=%s scopes=%d created=%d updated=%d "
        "revised=%d refusals=%d",
        project_id,
        environment_id,
        summary["scopes"],
        summary["forecasts_created"],
        summary["forecasts_updated"],
        summary["forecasts_revised"],
        summary["refusals"],
    )
    return summary


async def process_reliability_evaluate_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process a ``reliability_evaluate`` job (Phase 8 §57, §28).

    Scores every forecast whose window has elapsed and that has no outcome yet.
    Safe to run at any frequency: the gate is "no outcome row", not a timer.
    """
    from app.services.reliability_evaluation import PredictionEvaluationService

    project_id = _as_uuid(payload.get("project_id"), field="project_id")
    async with session_factory() as session:
        service = PredictionEvaluationService(session)
        summary = await service.evaluate_due(project_id=project_id)
        await session.commit()

    logger.info(
        "reliability_evaluate project=%s candidates=%d scored=%d",
        project_id,
        summary.get("candidates", 0),
        summary.get("scored", 0),
    )
    return summary


async def _ensure_telemetry_components(
    session: AsyncSession,
    *,
    project_id: uuid_mod.UUID,
    environment_id: Optional[uuid_mod.UUID],
    trace_events: list[Any],
) -> int:
    """Materialize first-seen services from trace evidence and attribute spans.

    ``ComponentResolver`` only matches *existing* components, so a brand-new
    service that appears in telemetry would stay ``component_id=None`` forever
    and its trace relationships could never reach the graph. The registry is
    explicitly responsible for discovery/identity resolution (§12–§13), so the
    extraction job promotes authoritative ``service.name`` evidence (source
    ``trace:<name>``, the OTLP adapter's shape) into real components before
    edge extraction runs. Existing components always win — never renamed,
    never merged blindly.

    Returns the number of events newly attributed to components.
    """
    from app.models.system import ComponentCategory, ComponentStatus, SystemComponent

    by_source: dict[str, list[Any]] = {}
    for event in trace_events:
        if event.component_id is None:
            source = event.source or ""
            if source.startswith("trace:") and len(source) > len("trace:"):
                by_source.setdefault(source[len("trace:") :], []).append(event)

    attributed = 0
    for service_name, events in by_source.items():
        existing = (
            await session.execute(
                select(SystemComponent.id).where(
                    SystemComponent.project_id == project_id,
                    SystemComponent.name == service_name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            component = SystemComponent(
                project_id=project_id,
                environment_id=environment_id,
                name=service_name,
                component_type=ComponentCategory.SERVICE,
                status=ComponentStatus.UNKNOWN,
                metadata_={"discovered_from": "trace_service_name"},
            )
            session.add(component)
            await session.flush()  # id available for attribution below
            component_id = component.id
        else:
            component_id = existing
        for event in events:
            event.component_id = component_id
            attributed += 1
    return attributed


def make_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    poll_interval: float = 0.5,
) -> IngestionWorker:
    """Build the production ingestion worker bound to the real pipeline."""

    async def _process(*, kind: str, payload: dict[str, Any]) -> Any:
        return await process_event_job(session_factory, kind=kind, payload=payload)

    return IngestionWorker(
        session_factory, process=_process, poll_interval=poll_interval
    )
