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
Jobs marked permanent (``PermanentJobError``) skip retries entirely — they can
never succeed, so re-enqueuing them would only starve the queue behind useful
work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Redis queue keys (logical queues by workload class).
QUEUE_EVENTS = "argus:ingest:events"
QUEUE_TRACES = "argus:ingest:traces"
#: Phase 3 detection runs on its own queue so a burst of telemetry cannot
#: starve detection (and vice versa).
QUEUE_DETECTION = "argus:detect:anomalies"
#: Phase 5 experiments run on their own queue for the same reason, and for one
#: more: an experiment can occupy a slot for minutes, so sharing a queue with
#: ingestion would stall telemetry behind a sandbox.
QUEUE_REPRODUCTION = "argus:repro:runs"
#: Phase 8 predictive reliability. Its own queue so a slow forecast sweep can
#: never delay ingestion or detection.
QUEUE_RELIABILITY = "argus:reliability:jobs"
#: Phase 9 safe autonomous remediation. Its own queue because an action can be
#: gated, verified over a window and (worst case) rolled back, none of which may
#: delay the telemetry that would let an operator see what is happening.
QUEUE_REMEDIATION = "argus:remediate:actions"
ALL_QUEUES = [
    QUEUE_EVENTS,
    QUEUE_TRACES,
    QUEUE_DETECTION,
    QUEUE_REPRODUCTION,
    QUEUE_RELIABILITY,
    QUEUE_REMEDIATION,
]

MAX_RETRIES = 3
BACKOFF_SECONDS = 1.0


class PermanentJobError(RuntimeError):
    """A job that can never succeed: skip retries and dead-letter immediately.

    Raised by processors for conditions that no retry can fix — a job for a
    project that was deleted will fail identically on every attempt, so
    re-enqueuing it only clogs the queue behind useful work (and, with enough
    such jobs, starves live traffic entirely).
    """


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

    async def pop(
        self, queue_name: Optional[str] = None, timeout: float = 0.5
    ) -> Optional[dict[str, Any]]:
        """Dequeue the next valid job, or ``None`` when the queue is empty.

        A ``timeout <= 0`` is a strictly non-blocking pop (``LPOP``): Redis
        ``BLPOP`` treats a zero timeout as *block forever*, which would wedge
        the worker on an empty queue and starve every other queue in the
        rotation. Positive timeouts use ``BLPOP`` as a long-poll.

        Corrupt (unparseable) payloads are consumed and dropped with a warning
        so they cannot stall the drain for the valid jobs behind them. A
        ``None`` return therefore always means *empty*, never "corrupt".
        """
        queue = queue_name or self._queue_name
        while True:
            try:
                if timeout and timeout > 0:
                    raw = await self._client().blpop(queue, timeout=timeout)
                    if raw is None:
                        return None
                    _, payload = raw
                else:
                    payload = await self._client().lpop(queue)
                    if payload is None:
                        return None
            except Exception as e:
                raise QueueUnavailable(f"cannot dequeue: {e}") from e
            try:
                return json.loads(payload)
            except json.JSONDecodeError:  # corrupt job — drop and move on
                logger.warning("Dropping corrupt queue payload: %.200s", payload)
                continue


def make_job(*, kind: str, payload: dict[str, Any], retries: int = 0) -> dict[str, Any]:
    """Build a typed ingestion job envelope."""
    return {"kind": kind, "payload": payload, "_retries": retries}


async def enqueue_graph_extract(
    *, project_id: Any, environment_id: Optional[Any] = None
) -> bool:
    """Queue a post-ingest knowledge-graph extraction job (Phase 2 §38).

    The payload carries only opaque ids — never raw telemetry. No-op when
    ``GRAPH_EXTRACT_ASYNC`` is disabled (tests / synchronous deployments).
    Returns ``False`` (degraded, no data lost) when the broker is unreachable;
    the graph is then built by the next explicit reconcile instead.
    """
    if not settings.GRAPH_EXTRACT_ASYNC:
        return False
    payload = {
        "project_id": str(project_id),
        "environment_id": str(environment_id) if environment_id else None,
    }
    try:
        await IngestionQueue().push(make_job(kind="graph_extract", payload=payload))
        return True
    except QueueUnavailable as e:
        logger.info("Graph-extract enqueue skipped (queue unavailable): %s", e)
        return False


async def enqueue_anomaly_detect(
    *, project_id: Any, environment_id: Optional[Any] = None
) -> bool:
    """Queue a post-ingest anomaly detection job (Phase 3 §20).

    The payload carries only opaque ids — never raw telemetry. No-op when
    ``ANOMALY_DETECTION_ASYNC`` is disabled (tests / synchronous deployments).
    Returns ``False`` (degraded, no data lost) when the broker is unreachable;
    the scheduled sweep then picks the data up instead.
    """
    if not settings.ANOMALY_DETECTION_ASYNC:
        return False
    payload = {
        "project_id": str(project_id),
        "environment_id": str(environment_id) if environment_id else None,
    }
    try:
        await IngestionQueue(QUEUE_DETECTION).push(
            make_job(kind="anomaly_detect", payload=payload)
        )
        return True
    except QueueUnavailable as e:
        logger.info("Anomaly-detect enqueue skipped (queue unavailable): %s", e)
        return False


async def enqueue_incident_correlate(
    *, project_id: Any, environment_id: Optional[Any] = None
) -> bool:
    """Queue a post-detection incident correlation job (Phase 3 §21).

    Shares the detection queue: detection → correlation is one pipeline, and
    keeping them ordered avoids correlating while anomalies are still being
    written. Returns ``False`` (degraded) when the broker is unreachable — the
    scheduled sweep performs the same work.
    """
    if not settings.ANOMALY_DETECTION_ASYNC:
        return False
    payload = {
        "project_id": str(project_id),
        "environment_id": str(environment_id) if environment_id else None,
    }
    try:
        await IngestionQueue(QUEUE_DETECTION).push(
            make_job(kind="incident_correlate", payload=payload)
        )
        return True
    except QueueUnavailable as e:
        logger.info("Incident-correlate enqueue skipped (queue unavailable): %s", e)
        return False


async def enqueue_reproduction_run(*, experiment_id: Any, project_id: Any) -> bool:
    """Queue an experiment execution job (Phase 5 §53).

    The payload carries opaque ids only — never a plan, never a command. The
    worker re-reads the plan from the database, so a queued message cannot be
    used to smuggle execution parameters past planning and validation.

    Returns ``False`` when the broker is unreachable so the API can report a
    clear failure instead of leaving the experiment stuck in ``VALIDATING``.
    """
    payload = {
        "experiment_id": str(experiment_id),
        "project_id": str(project_id),
    }
    try:
        await IngestionQueue(QUEUE_REPRODUCTION).push(
            make_job(kind="reproduction_run", payload=payload)
        )
        return True
    except QueueUnavailable as e:
        logger.warning("Reproduction enqueue failed (queue unavailable): %s", e)
        return False


async def enqueue_reliability_forecast(
    *, project_id: Any, environment_id: Optional[Any] = None
) -> bool:
    """Queue a forecast generation job (Phase 8 §57).

    The payload carries opaque ids only: the worker re-reads the scope from the
    database, so a queued message cannot smuggle a component list or a feature
    set past the service that validates them.

    No-op when ``RELIABILITY_ASYNC`` is disabled (tests, synchronous
    deployments). Returns ``False`` when the broker is unreachable — no data is
    lost, because the scheduled sweep performs the same work.
    """
    if not settings.RELIABILITY_ASYNC:
        return False
    payload = {
        "project_id": str(project_id),
        "environment_id": str(environment_id) if environment_id else None,
    }
    try:
        await IngestionQueue(QUEUE_RELIABILITY).push(
            make_job(kind="reliability_forecast", payload=payload)
        )
        return True
    except QueueUnavailable as e:
        logger.info("Reliability forecast enqueue skipped (queue unavailable): %s", e)
        return False


async def enqueue_reliability_evaluate(*, project_id: Optional[Any] = None) -> bool:
    """Queue a forecast evaluation job (Phase 8 §57).

    Scoring is time-driven rather than ingestion-driven: an outcome only exists
    once a horizon has elapsed. The job therefore takes no scope beyond the
    project and lets the service pick the forecasts that are due.
    """
    if not settings.RELIABILITY_ASYNC:
        return False
    payload = {"project_id": str(project_id) if project_id else None}
    try:
        await IngestionQueue(QUEUE_RELIABILITY).push(
            make_job(kind="reliability_evaluate", payload=payload)
        )
        return True
    except QueueUnavailable as e:
        logger.info("Reliability evaluate enqueue skipped (queue unavailable): %s", e)
        return False


async def enqueue_remediation_action(*, action_id: Any, project_id: Any) -> bool:
    """Queue an authorized remediation action (Phase 9 §39).

    The payload carries opaque ids only — never parameters, never a target. The
    worker re-reads the action from the database and re-checks every gate before
    applying anything, so a queued message cannot be used to execute something the
    gates would refuse.

    No-op when ``REMEDIATION_ASYNC`` is disabled. Returns ``False`` when the broker
    is unreachable; the action stays ``AUTHORIZED`` and the remediation sweep picks
    it up, so a Redis outage cannot lose an approved remediation.
    """
    if not settings.REMEDIATION_ASYNC:
        return False
    payload = {
        "action_id": str(action_id),
        "project_id": str(project_id),
    }
    try:
        await IngestionQueue(QUEUE_REMEDIATION).push(
            make_job(kind="remediation_run", payload=payload)
        )
        return True
    except QueueUnavailable as e:
        logger.warning("Remediation enqueue skipped (queue unavailable): %s", e)
        return False


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
        except PermanentJobError as e:
            # Never retryable: dead-letter now, regardless of retry count.
            logger.error(
                "Job permanent-failed (kind=%s retries=%d): %s", kind, retries, e
            )
            try:
                await self._dead_letter(kind, payload, retries, e)
            except Exception as dl_e:
                logger.error("Dead-letter write failed: %s", dl_e)
            return
        except Exception as e:
            error = e
            logger.error("Job failed (kind=%s retries=%d): %s", kind, retries, e)

        if retries < self._max_retries:
            await asyncio.sleep(BACKOFF_SECONDS * (2**retries))
            await self._queue.push(
                make_job(kind=kind, payload=payload, retries=retries + 1)
            )
        else:
            try:
                await self._dead_letter(kind, payload, retries, error)
            except Exception as dl_e:
                logger.error("Dead-letter write failed: %s", dl_e)

    async def _dead_letter(
        self, kind: str, payload: dict[str, Any], retries: int, exc: Exception
    ) -> None:
        """Persist an exhausted job to ingestion_failures (redacted).

        The failure record must outlive the entity it complains about: if the
        referenced project no longer exists (a common cause of permanent
        failures), an FK-bound ``project_id`` would make the dead-letter write
        itself fail and the job's fate would go unrecorded. Dangling ids are
        kept only in the redacted ``payload_summary``.
        """
        from app.models.ingestion import IngestionFailure
        from app.models.project import SoftwareProject
        from app.services.redaction import RedactionEngine

        raw_project_id = payload.get("project_id")
        project_id = None
        if raw_project_id is not None:
            try:
                project_id = uuid.UUID(str(raw_project_id))
            except (ValueError, TypeError, AttributeError):
                project_id = None
        if project_id is not None:
            from sqlalchemy import select

            async with self._factory() as session:
                exists = await session.scalar(
                    select(
                        select(SoftwareProject.id)
                        .where(SoftwareProject.id == project_id)
                        .exists()
                    )
                )
                if not exists:
                    raw_project_id, project_id = str(raw_project_id), None

        async with self._factory() as session:
            session.add(
                IngestionFailure(
                    fingerprint=f"queue:{kind}:{retries}:{abs(hash(repr(payload))):x}",
                    source_id=payload.get("source_id"),
                    source=payload.get("source"),
                    project_id=project_id,
                    event_type=payload.get("event_type"),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:2000],
                    retry_count=retries,
                    payload_summary=RedactionEngine().payload_summary(payload),
                )
            )
            await session.commit()
