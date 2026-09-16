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

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.sources import MockObservabilitySource, RawObservabilityEvent
from app.services.ingestion import IngestionPipeline
from app.services.queue import IngestionWorker

logger = logging.getLogger(__name__)

# Mirrors the API-boundary guardrail (§46): known secret keys are never
# admitted. The async path must apply the same rule so a job enqueued by any
# producer still cannot leak secrets into the domain model.
_FORBIDDEN_PAYLOAD_KEYS = {
    "password", "passwd", "pwd", "secret", "api_key", "apikey",
    "access_token", "auth_token", "bearer", "private_key", "token",
}


def _reject_secrets(payload: Optional[dict]) -> None:
    """Raise on any payload carrying a known secret key (§46)."""
    if not payload:
        return
    for key in payload:
        lower = str(key).lower()
        if any(fragment in lower for fragment in _FORBIDDEN_PAYLOAD_KEYS):
            raise ValueError(f"Payload key '{key}' is not allowed (secrets are never ingested)")


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
    """
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
            result.accepted, result.duplicates, result.failed, result.failures,
        )
    else:
        logger.info(
            "Queued batch: accepted=%d duplicates=%d", result.accepted, result.duplicates,
        )
    return result.accepted


def make_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    poll_interval: float = 0.5,
) -> IngestionWorker:
    """Build the production ingestion worker bound to the real pipeline."""
    async def _process(*, kind: str, payload: dict[str, Any]) -> Any:
        return await process_event_job(session_factory, kind=kind, payload=payload)

    return IngestionWorker(session_factory, process=_process, poll_interval=poll_interval)