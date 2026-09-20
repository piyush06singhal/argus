"""ARGUS Observability Event Normalization."""

from __future__ import annotations

import logging
from typing import List, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sources import RawObservabilityEvent
from app.models.observability import EventType, ObservabilityEvent, Severity

logger = logging.getLogger(__name__)


class NormalizationError(Exception):
    """Raised when an event cannot be normalized."""

    def __init__(self, message: str, reason: str = "NORMALIZATION_ERROR"):
        self.reason = reason
        super().__init__(message)


class ObservabilityNormalizer:
    """Normalizes raw events into the ARGUS canonical event model.

    Pipeline:
        1. Validate raw event structure
        2. Map to canonical ObservabilityEvent
        3. Attach project/environment references
        4. Persist via repository
    """

    def __init__(self, db: AsyncSession):
        self._db = db

    async def normalize(
        self,
        raw: RawObservabilityEvent,
        project_id: UUID,
        environment_id: Optional[UUID] = None,
    ) -> ObservabilityEvent:
        """Normalize a single raw event into an ObservabilityEvent."""
        # Validate required payload fields. Case-insensitive so adapters that
        # deliver UPPERCASE source kinds (e.g. the webhook path's ``WEBHOOK``)
        # normalize cleanly; the allowlist still bounds what we accept.
        if raw.source_type.lower() not in (
            "log",
            "metric",
            "trace",
            "system",
            "deployment",
            "health",
            "configuration",
            "mock",
            "webhook",
        ):
            raise NormalizationError(
                f"Unknown source type: {raw.source_type}",
                reason="UNKNOWN_SOURCE_TYPE",
            )

        # Map event type
        try:
            event_type = EventType(raw.event_type.upper())
        except ValueError:
            raise NormalizationError(
                f"Unknown event type: {raw.event_type}",
                reason="UNKNOWN_EVENT_TYPE",
            )

        # Extract severity from payload if present
        severity = None
        if "severity" in raw.payload:
            try:
                severity = Severity(raw.payload["severity"].upper())
            except ValueError:
                severity = None

        event = ObservabilityEvent(
            project_id=project_id,
            environment_id=environment_id if environment_id else None,
            timestamp=raw.timestamp,
            source=f"{raw.source_type}:{raw.source_name}",
            event_type=event_type,
            severity=severity,
            payload=raw.payload,
            metadata_=raw.metadata,
        )

        # Extract correlation IDs from payload
        for key, field in [
            ("request_id", "request_id"),
            ("trace_id", "trace_id"),
            ("span_id", "span_id"),
        ]:
            if key in raw.payload:
                setattr(event, field, str(raw.payload[key]))

        self._db.add(event)
        return event

    async def normalize_batch(
        self,
        events: List[RawObservabilityEvent],
        project_id: UUID,
        environment_id: Optional[UUID] = None,
    ) -> List[ObservabilityEvent]:
        """Normalize a batch of raw events."""
        normalized = []
        for raw in events:
            try:
                normalized.append(await self.normalize(raw, project_id, environment_id))
            except NormalizationError as e:
                logger.warning(f"Skipping malformed event: {e}")
        return normalized
