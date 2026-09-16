"""ARGUS Ingestion Queue & Worker.

Phase 1 §37–§44: asynchronous ingestion via a Redis-backed queue. The API
enqueues typed jobs; a background worker drains the queue, applies bounded
retries with exponential backoff, and dead-letters events that exhaust their
retry budget.

Queue layout (lists, one per priority class):
    argus:ingest:events   – normalized event ingest jobs
    argus:ingest:traces   – trace (with spans) ingest jobs

Retry design: a job's retry count is tracked on the payload (``_retries``,
never persisted to the domain model). On failure the job is re-enqueued with a
backoff executed by the worker to give transient dependencies time to recover.
Exhausted jobs are written to the ingestion_failures table via the pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Redis queue keys (logical queues by workload class).
QUEUE_EVENTS = "argus:ingest:events"
QUEUE_TRACES = "argus:ingest:traces"
ALL_QUEUES = [QUEUE_EVENTS, QUEUE_TRACES]

MAX_RETRIES = 3
BACKOFF_SECONDS = 1.0


class QueueUnavailable(RuntimeError):
    """Raised when the configured message broker cannot be reached."""


class IngestionQueue:
    """Minimal Redis-based queue adapter.

    Deliberately small — one enqueue, one dequeue, a repair primitive — so the
    worker has no opinion about payloads it does not own. Redis connection is
    created lazily so unit tests never need a broker.
    """

    def __init__(self, queue_name: str = QUEUE_EVENTS):
        self._queue_name = queue_name
        self._redis = None

    def _client(self):
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
            except ImportError as e:  # pragma: no cover
                raise QueueUnavailable("redis library not installed") from e
            self._redis = aioredis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=0,
                decode_responses=True,
            )
        return self._redis

    async def push(self, job: dict[str, Any]) -> None:
        try:
            await self._client().rpush(self._queue_name, json.dumps(job))
        except Exception as e:
            raise QueueUnavailable(f"cannot enqueue: {e}") from e

    async def pop(self, queue_name: Optional[str] = None, timeout: float = 0.5) -> Optional[dict[str, Any]]:
        """Dequeue the next valid job, or ``None`` when the queue is empty.

        Corrupt (unparseable) payloads are consumed and dropped with a warning
        so they cannot stall the drain for the valid jobs behind them. A
        ``None`` return therefore always means *empty*, never "corrupt".
        """
        queue = queue_name or self._queue_name
        while True:
            try:
                raw = await self._client().blpop(queue, timeout=timeout)
            except Exception as e:
                raise QueueUnavailable(f"cannot dequeue: {e}") from e
            if raw is None:
                return None
            _, payload = raw
            try:
                return json.loads(payload)
            except json.JSONDecodeError:  # corrupt job — drop and move on
                logger.warning("Dropping corrupt queue payload: %.200s", payload)
                continue


def make_job(*, kind: str, payload: dict[str, Any], retries: int = 0) -> dict[str, Any]:
    """Build a typed ingestion job envelope."""
    return {"kind": kind, "payload": payload, "_retries": retries}


class IngestionWorker:
    """Drains ingestion queues and writes through the pipeline.

    Decoupling from the pipeline is deliberately thin: the worker receives a
    ``process`` coroutine functor so tests can substitute a fake processor and
    still exercise retry/backoff/dead-letter bookkeeping.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        process: Any,
        queues: Optional[list[str]] = None,
        max_retries: int = MAX_RETRIES,
        poll_interval: float = 0.5,
        max_jobs_per_batch: int = 50,
    ):
        self._factory = session_factory
        self._process = process
        self._queues = queues or ALL_QUEUES
        self._max_retries = max_retries
        self._poll_interval = poll_interval
        self._max_jobs_per_batch = max_jobs_per_batch
        self._running = False
        self._queue = IngestionQueue()

    async def run_once(self) -> int:
        """Dequeue and process up to ``max_jobs_per_batch`` jobs.

        Returns the number of jobs drained (success + permanent failure).
        """
        drained = 0
        for queue_name in self._queues:
            for _ in range(self._max_jobs_per_batch):
                job = await self._queue.pop(queue_name, timeout=0)
                if job is None:
                    break
                await self._handle(job)
                drained += 1
        return drained

    async def run_forever(self) -> None:
        """Poll the queues continuously until stopped."""
        self._running = True
        while self._running:
            try:
                await self.run_once()
            except QueueUnavailable as e:
                logger.warning("Queue unavailable, retrying: %s", e)
            except Exception as e:  # defensively keep the loop alive
                logger.error("Worker error: %s", e)
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _handle(self, job: dict[str, Any]) -> None:
        """Process one job with bounded retry + dead-letter on exhaustion."""
        kind = job.get("kind", "event")
        payload = job.get("payload", {})
        retries = int(job.get("_retries", 0))

        error: Optional[Exception] = None
        try:
            await self._process(kind=kind, payload=payload)
            return
        except Exception as e:
            error = e
            logger.error("Job failed (kind=%s retries=%d): %s", kind, retries, e)

        if retries < self._max_retries:
            await asyncio.sleep(BACKOFF_SECONDS * (2**retries))
            await self._queue.push(make_job(kind=kind, payload=payload, retries=retries + 1))
        else:
            try:
                await self._dead_letter(kind, payload, retries, error)
            except Exception as dl_e:
                logger.error("Dead-letter write failed: %s", dl_e)

    async def _dead_letter(self, kind: str, payload: dict[str, Any], retries: int, exc: Exception) -> None:
        """Persist an exhausted job to ingestion_failures (redacted)."""
        from app.models.ingestion import IngestionFailure
        from app.services.redaction import RedactionEngine

        async with self._factory() as session:
            session.add(
                IngestionFailure(
                    fingerprint=f"queue:{kind}:{retries}:{abs(hash(repr(payload))):x}",
                    source_id=payload.get("source_id"),
                    source=payload.get("source"),
                    project_id=payload.get("project_id"),
                    event_type=payload.get("event_type"),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:2000],
                    retry_count=retries,
                    payload_summary=RedactionEngine().payload_summary(payload),
                )
            )
            await session.commit()