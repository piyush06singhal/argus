"""ARGUS Observability Ingestion Pipeline.

Phase 1 unified pipeline: Source → Adapter → Validate → Redact →
Resolve (component/environment) → Normalize → Correlate → Dedup → Persist.

Every event that fails validation or dedup handling is counted and surfaced;
nothing is silently dropped. Deduplicated events are counted separately.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.sources import ObservabilitySource, RawObservabilityEvent
from app.models.ingestion import (
    ObservabilitySource as SourceRegistryModel,
    ObservabilitySourceStatus,
    IngestionFailure,
)
from app.models.observability import ObservabilityEvent
from app.services.ingestion_helpers import (
    ComponentResolver,
    CorrelationEngine,
    EnvironmentResolver,
    EventFingerprint,
)
from app.services.normalizer import ObservabilityNormalizer
from app.services.redaction import RedactionEngine

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class IngestionResult:
    """Outcome of an ingest batch (§44)."""

    accepted: int = 0
    duplicates: int = 0
    failed: int = 0
    failures: List[str] = field(default_factory=list)


class IngestionPipeline:
    """The ARGUS ingestion pipeline.

    Pipeline flow:
        Source → Adapter → Validation → Redaction → Resolution →
        Normalization → Correlation → Dedup → Persistence
    """

    def __init__(
        self,
        source: ObservabilitySource,
        db: AsyncSession,
        project_id: UUID,
        environment_id: Optional[UUID] = None,
    ):
        self._source = source
        self._db = db
        self._project_id = project_id
        self._environment_id = environment_id
        self._normalizer = ObservabilityNormalizer(db)
        self._redactor = RedactionEngine()
        self._component_resolver = ComponentResolver(db)
        self._environment_resolver = EnvironmentResolver(db)
        self._correlation = CorrelationEngine()
        self._running = False
        # Drawn from the persistent source registry when available.
        self._registry_source: Optional[SourceRegistryModel] = None

    async def _load_registry_source(self) -> None:
        """Bind the pipeline to the persistent source registry row (if any)."""
        from sqlalchemy import select

        result = await self._db.execute(
            select(SourceRegistryModel).where(
                SourceRegistryModel.project_id == self._project_id,
                SourceRegistryModel.name == self._source.name,
            )
        )
        self._registry_source = result.scalar_one_or_none()

    async def _record_source_signal(
        self, *, success: bool, event_count: int = 0, error: str | None = None
    ) -> None:
        """Update the persistent source health signal (§45)."""
        if self._registry_source is None:
            return
        source = self._registry_source
        source.event_count += event_count
        source.last_event_at = datetime.utcnow()
        if success:
            source.last_success_at = datetime.utcnow()
            source.consecutive_errors = 0
            source.status = ObservabilitySourceStatus.HEALTHY
        else:
            source.consecutive_errors += 1
            source.error_count += 1
            source.last_error = (error or "unknown error")[:500]
            if source.consecutive_errors >= 3:
                source.status = ObservabilitySourceStatus.FAILING
            elif source.consecutive_errors >= 1:
                source.status = ObservabilitySourceStatus.DEGRADED

    async def _dead_letter(
        self, raw: RawObservabilityEvent, error: Exception, retry_count: int
    ) -> None:
        """Persist a failed event to the dead-letter store (§44)."""
        from app.services.ingestion_helpers import EventFingerprint

        # If the session's transaction was rolled back by the original error,
        # recover it before we attempt any further DB work, so dead-lettering a
        # failed event never itself fails on a poisoned session.
        #
        # The probe goes through `sync_session` on purpose. The async
        # `AsyncSession.get_transaction()` rebuilds a proxy for the underlying
        # transaction, and SQLAlchemy 2.0.36 raises a bare `NotImplementedError`
        # when the current transaction is the savepoint proxy that
        # `begin_nested()` leaves behind — which is precisely the state a failed
        # event in a batch leaves the session in. The bare exception carries no
        # message, and it was swallowed by the handler below, so no dead-letter
        # row was written at all: the failure was counted in the response and
        # never preserved for inspection. Reading the same state through
        # `sync_session.get_transaction()` involves no proxy regeneration and
        # works on every 2.0.x.
        transaction = self._db.sync_session.get_transaction()
        if transaction is not None and not transaction.is_active:
            await self._db.rollback()

        summary = self._redactor.payload_summary(raw.payload)

        # If a fingerprint already exists for this failure, update it instead
        # of creating a duplicate (idempotent dead-lettering).
        fingerprint = EventFingerprint().compute(
            project_id=self._project_id,
            source=f"{raw.source_type}:{raw.source_name}",
            event_type=raw.event_type,
            timestamp=raw.timestamp,
            payload=raw.payload,
        )
        existing = await self._db.execute(
            select(IngestionFailure).where(IngestionFailure.fingerprint == fingerprint)
        )
        failure = existing.scalar_one_or_none()
        if failure is None:
            failure = IngestionFailure(
                fingerprint=fingerprint,
                source_id=raw.metadata.get("source_id"),
                source=f"{raw.source_type}:{raw.source_name}",
                project_id=self._project_id,
                event_type=raw.event_type,
                error_type=type(error).__name__,
                error_message=str(error)[:2000],
                retry_count=retry_count,
                received_at=raw.timestamp,
                payload_summary=summary,
            )
            self._db.add(failure)
        else:
            failure.retry_count = max(failure.retry_count, retry_count)
            failure.failed_at = datetime.utcnow()
            failure.error_message = str(error)[:2000]

    async def ingest_one(
        self, raw: RawObservabilityEvent, *, source_id: Optional[str] = None
    ) -> bool:
        """Ingest a single raw event.

        Returns ``True`` when accepted, ``False`` when it was a duplicate.
        Raises on validation/persistence errors (batch handling wraps this).
        """
        payload = raw.payload or {}

        # 1. Redaction (defence in depth — the schema already rejects secrets).
        safe_payload = self._redactor.redact(payload)

        # 2. Resolve knowledge-graph references.
        component_id = await self._component_resolver.resolve(
            project_id=self._project_id,
            payload=safe_payload,
            source=f"{raw.source_type}:{raw.source_name}",
        )
        environment_id = (
            await self._environment_resolver.resolve(
                project_id=self._project_id, payload=safe_payload
            )
            or self._environment_id
        )

        # 3. Normalize into the canonical event (with the redacted payload).
        safe_raw = RawObservabilityEvent(
            source_type=raw.source_type,
            source_name=raw.source_name,
            timestamp=raw.timestamp,
            event_type=raw.event_type,
            payload=safe_payload,
            metadata=raw.metadata,
        )
        event = await self._normalizer.normalize(
            safe_raw, self._project_id, environment_id
        )
        event.component_id = component_id

        # 4. Correlate (shared request/trace/deployment anchors).
        event.correlation_id = self._correlation.correlate(
            request_id=event.request_id,
            trace_id=event.trace_id,
            deployment_id=getattr(event, "deployment_id", None),
        )
        event.source_id = source_id

        # 5. Deduplicate via canonical fingerprint.
        fingerprint = EventFingerprint().compute(
            project_id=self._project_id,
            source=event.source,
            event_type=event.event_type.value,
            timestamp=event.timestamp,
            payload=safe_payload,
        )
        dug = await self._db.execute(
            select(ObservabilityEvent.id).where(
                ObservabilityEvent.fingerprint == fingerprint
            )
        )
        if dug.first():
            return False

        event.fingerprint = fingerprint
        self._db.add(event)
        await self._db.flush()
        return True

    async def ingest_batch(
        self,
        raw_events: List[RawObservabilityEvent],
        *,
        source_id: Optional[str] = None,
    ) -> IngestionResult:
        """Ingest a batch of raw events; returns counts per outcome.

        Each event runs in its own SAVEPOINT (``begin_nested``) so a DB-level
        failure (e.g. a foreign-key violation) rolls back only that event and
        cannot poison the session for the rest of the batch — previously
        accepted events stay intact and can commit at the end. The failed event
        is dead-lettered in the outer transaction.
        """
        result = IngestionResult()
        for raw in raw_events:
            try:
                async with self._db.begin_nested():
                    ok = await self.ingest_one(raw, source_id=source_id)
                if ok:
                    result.accepted += 1
                else:
                    result.duplicates += 1
            except Exception as e:
                logger.error(f"Ingestion failure (event {raw.source_type}): {e}")
                result.failed += 1
                result.failures.append(
                    f"{raw.source_type}:{raw.source_name} → {type(e).__name__}"
                )
                try:
                    await self._dead_letter(raw, e, retry_count=1)
                except Exception as dl_e:
                    # `exc_info=True` because a failed dead-letter is how this
                    # class of defect stays invisible: the exception may carry
                    # no message at all, and the traceback is the only evidence
                    # of why the failure was not recorded.
                    logger.error(f"Dead-lettering failed: {dl_e!r}", exc_info=True)
        await self._record_source_signal(
            success=result.failed == 0,
            event_count=result.accepted + result.duplicates,
            error=result.failures[0] if result.failures else None,
        )
        await self._db.commit()
        return result

    async def poll_and_ingest(
        self, lookback_minutes: int = 5, *, source_id: Optional[str] = None
    ) -> IngestionResult:
        """Poll the source for new events and ingest them."""
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(minutes=lookback_minutes)

        await self._load_registry_source()
        events = await self._source.fetch_events(start_time, end_time)
        if not events:
            return IngestionResult()

        return await self.ingest_batch(events, source_id=source_id)

    async def run_continuous(self, interval_seconds: int = 30) -> None:
        """Run continuous ingestion from the source."""
        self._running = True
        while self._running:
            try:
                result = await self.poll_and_ingest()
                if result.accepted > 0:
                    logger.info(
                        f"Ingested {result.accepted} events from {self._source.name}"
                    )
            except Exception as e:
                logger.error(f"Continuous ingestion error: {e}")
            await asyncio.sleep(interval_seconds)

    def stop(self) -> None:
        """Stop continuous ingestion."""
        self._running = False
