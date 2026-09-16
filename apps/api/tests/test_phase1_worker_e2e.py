"""Phase 1 End-to-End Worker + Security Tests.

Covers:
  - process_event_job pipeline path (real job envelope → persisted ObservabilityEvent)
  - Secret rejection on direct-write routes (§46)
  - Source-scoped webhook (§47)
  - GET /events/{event_id} (§37)
  - Oversized payload / label cardinality guards (§27)
  - Corrupt queue job handling (graceful drop, no loop breakage)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.observability import ObservabilityEvent

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_project(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "WorkerE2E", "slug": _unique_slug("we2e")},
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# process_event_job → real pipeline → ObservabilityEvent persisted
# ---------------------------------------------------------------------------
class TestProcessEventJobE2E:
    """The production worker processor feeds real pipeline and persists."""

    async def test_end_to_end_job_persists_event(self, db_engine) -> None:
        from app.services.worker_runner import process_event_job

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        project_id = str(uuid.uuid4())

        accepted = await process_event_job(
            factory,
            kind="event",
            payload={
                "project_id": project_id,
                "environment_id": None,
                "source_id": None,
                "events": [{
                    "source_type": "WEBHOOK",
                    "source_name": "e2e-test",
                    "timestamp": "2026-01-01T12:00:00Z",
                    "event_type": "SYSTEM_EVENT",
                    "payload": {"message": "e2e pipeline test"},
                    "metadata": {"origin": "test"},
                }],
            },
        )
        assert accepted == 1

        async with factory() as session:
            result = await session.execute(
                select(ObservabilityEvent).where(ObservabilityEvent.project_id == uuid.UUID(project_id))
            )
            event = result.scalar_one_or_none()
            assert event is not None
            assert event.source == "WEBHOOK:e2e-test"
            assert event.event_type.value == "SYSTEM_EVENT"
            assert event.payload.get("message") == "e2e pipeline test"

    async def test_job_unknown_kind_dead_letters(self, db_engine) -> None:
        from app.services.worker_runner import process_event_job

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        with pytest.raises(NotImplementedError, match="No pipeline provisioned"):
            await process_event_job(factory, kind="trace", payload={})

    async def test_job_missing_project_id_raises(self, db_engine) -> None:
        from app.services.worker_runner import process_event_job

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        with pytest.raises(ValueError, match="missing required project_id"):
            await process_event_job(factory, kind="event", payload={"events": [{}]})

    async def test_job_secret_in_payload_raises(self, db_engine) -> None:
        from app.services.worker_runner import process_event_job

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        with pytest.raises(ValueError, match="not allowed"):
            await process_event_job(
                factory,
                kind="event",
                payload={
                    "project_id": str(uuid.uuid4()),
                    "events": [{
                        "source_type": "WEBHOOK",
                        "source_name": "s",
                        "timestamp": "2026-01-01T12:00:00Z",
                        "event_type": "SYSTEM_EVENT",
                        "payload": {"token": "secret123"},
                    }],
                },
            )

    async def test_batch_savepoint_isolates_failures(self, db_session: AsyncSession) -> None:
        """A mid-batch DB failure must not poison the rest (§44, §20).

        Regression for a live finding: a foreign-key / flush error used to
        poison the session, discarding already-accepted events and breaking
        dead-lettering. Each event now runs in its own SAVEPOINT.
        """
        from app.core.sources import MockObservabilitySource, RawObservabilityEvent
        from app.models.ingestion import IngestionFailure
        from app.services.ingestion import IngestionPipeline

        project_id = uuid.uuid4()
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(), db=db_session, project_id=project_id,
        )

        calls = {"n": 0}
        original = pipeline.ingest_one

        async def flaky(raw, *, source_id=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("simulated FK violation")
            return await original(raw, source_id=source_id)

        pipeline.ingest_one = flaky  # type: ignore[method-assign]

        def make(i: int) -> RawObservabilityEvent:
            return RawObservabilityEvent(
                source_type="mock",
                source_name=f"savepoint-{i}",
                timestamp=_TS,
                event_type="SYSTEM_EVENT",
                payload={"i": i},
            )

        result = await pipeline.ingest_batch([make(1), make(2), make(3)])

        assert result.accepted == 2
        assert result.failed == 1
        assert isinstance(result.failures[0], str)

        # Both good events persisted; the failed one did NOT — and is dead-lettered.
        persisted = (await db_session.execute(select(ObservabilityEvent))).scalars().all()
        assert len(persisted) == 2
        dl = (await db_session.execute(select(IngestionFailure))).scalars().all()
        assert len(dl) == 1
        assert dl[0].error_type == "ValueError"


# ---------------------------------------------------------------------------
# Source-scoped webhook  POST /ingestion/webhooks/{source_id}
# ---------------------------------------------------------------------------
class TestSourceScopedWebhook:
    """§47: Webhook routed by a registered source's project_id."""

    def test_webhook_for_existing_source(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/ingestion/sources", json={
            "project_id": project["id"],
            "name": f"src-{uuid.uuid4().hex[:6]}",
            "source_type": "OTEL",
            "configuration": {},
        })
        assert resp.status_code == 201
        source_id = resp.json()["id"]

        resp = client.post(f"/api/v1/ingestion/webhooks/{source_id}", json={
            "event_type": "LOG",
            "timestamp": "2026-01-01T12:00:00Z",
            "payload": {"message": "src-scoped webhook"},
        })
        assert resp.status_code == 202
        body = resp.json()
        assert body["received"] is True
        assert body["queued"] is True

    def test_webhook_for_missing_source_404(self, client: TestClient) -> None:
        fake_source_id = str(uuid.uuid4())
        resp = client.post(f"/api/v1/ingestion/webhooks/{fake_source_id}", json={
            "event_type": "LOG",
            "timestamp": "2026-01-01T12:00:00Z",
            "payload": {},
        })
        assert resp.status_code == 404

    def test_webhook_requires_project_or_source(self, client: TestClient) -> None:
        resp = client.post("/api/v1/ingestion/webhook", json={
            "event_type": "LOG",
            "timestamp": "2026-01-01T12:00:00Z",
            "payload": {},
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /events/{event_id}
# ---------------------------------------------------------------------------
class TestGetEventById:
    """§37: Single-event retrieval."""

    def test_get_existing_event(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/events", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "source": "unit-test",
            "event_type": "SYSTEM_EVENT",
            "severity": "INFO",
            "payload": {"test": True},
        })
        assert resp.status_code == 201
        event_id = resp.json()["id"]

        get_resp = client.get(f"/api/v1/observability/events/{event_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == event_id
        assert get_resp.json()["source"] == "unit-test"

    def test_get_nonexistent_event_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/observability/events/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Direct-write sanitization (§46)
# ---------------------------------------------------------------------------
class TestDirectWriteSecretRejection:
    """Secrets are never ingested — rejected at boundary with 422."""

    def test_event_secret_rejected(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/events", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "source": "test",
            "event_type": "SYSTEM_EVENT",
            "payload": {"api_key": "sk-live-abc123"},
        })
        assert resp.status_code == 422
        assert "not allowed" in resp.json()["detail"]

    def test_log_secret_rejected(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/logs", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "level": "INFO",
            "message": "ok",
            "metadata": {"password": "hunter2"},
        })
        assert resp.status_code == 422

    def test_metric_secret_rejected(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/metrics", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "metric_name": "cpu_usage",
            "metric_type": "GAUGE",
            "value": 42.0,
            "metadata": {"auth_token": "bearer xyz"},
        })
        assert resp.status_code == 422

    def test_trace_secret_rejected(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/traces", json={
            "project_id": project["id"],
            "trace_id": uuid.uuid4().hex,
            "metadata": {"secret": "do-not-ingest"},
        })
        assert resp.status_code == 422

    def test_span_secret_rejected(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/traces/spans", json={
            "trace_id": uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex,
            "project_id": project["id"],
            "start_time": "2026-01-01T12:00:00Z",
            "metadata": {"private_key": "-----BEGIN RSA PRIVATE KEY-----"},
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Oversized payload guards (§27)
# ---------------------------------------------------------------------------
class TestPayloadLimits:
    """Payload size / cardinality limits enforced on direct writes."""

    def test_log_message_oversized_422(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/observability/logs", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "level": "INFO",
            "message": "x" * 8193,  # MAX_LOG_MESSAGE_LENGTH = 8192
        })
        assert resp.status_code == 422
        assert "exceeds limit" in resp.json()["detail"]

    def test_metadata_oversized_422(self, client: TestClient) -> None:
        project = _create_project(client)
        # Plain text with spaces — deliberately NOT a base64/hex/JWT pattern so
        # the redactor leaves the value intact and the size check has to fire.
        bulky = ("lorem ipsum dolor sit amet " * 500)  # ~12.5k chars
        assert len(bulky) > 10000  # MAX_METADATA_LENGTH = 10000
        resp = client.post("/api/v1/observability/logs", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "level": "INFO",
            "message": "ok",
            "metadata": {"bulk": bulky},
        })
        assert resp.status_code == 422

    def test_metric_label_cardinality_guard(self, client: TestClient) -> None:
        project = _create_project(client)
        labels = {f"key{i}": f"val{i}" for i in range(33)}  # MAX_METRIC_LABELS = 32
        resp = client.post("/api/v1/observability/metrics", json={
            "project_id": project["id"],
            "timestamp": "2026-01-01T12:00:00Z",
            "metric_name": "requests_total",
            "metric_type": "COUNTER",
            "value": 1,
            "labels": labels,
        })
        assert resp.status_code == 422
        assert "label" in resp.json()["detail"].lower() or "cardinality" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Corrupt queue job handling (no loop breakage)
# ---------------------------------------------------------------------------
class TestCorruptQueueJob:
    """Corrupt (non-JSON) queue entries are dropped; processing continues."""

    async def test_pop_drops_corrupt_payload(self) -> None:
        from app.services.queue import IngestionQueue

        queue = IngestionQueue()
        # Fake the Redis client to return a corrupt payload without a broker.
        fake_responses = iter([
            ("argus:ingest:events", "{not valid json"),
            ("argus:ingest:events", '{"kind": "event", "payload": {"ok": 1}, "_retries": 0}'),
            None,
        ])

        class FakeRedis:
            async def blpop(self, queue, timeout=0):
                try:
                    return next(fake_responses)
                except StopIteration:
                    return None

        with patch.object(queue, "_client", return_value=FakeRedis()):
            # Corrupt entry is consumed & dropped; the next call returns the good job.
            assert await queue.pop() == {"kind": "event", "payload": {"ok": 1}, "_retries": 0}
            assert await queue.pop() is None  # queue fully drained

    async def test_worker_survives_corrupt_job(self, db_engine) -> None:
        """A corrupt-first queue still drains the good job — no loop breakage."""
        from app.services.queue import IngestionQueue, IngestionWorker, QUEUE_EVENTS

        processed: list = []

        async def process(kind, payload):
            processed.append(payload)

        factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

        good_job = {"kind": "event", "payload": {"source_id": "good"}, "_retries": 0}
        # Redis hands us JSON strings, not dicts.
        items = ["{not valid json", json.dumps(good_job)]
        # A real IngestionQueue whose Redis client returns corrupt + good payloads.
        queue = IngestionQueue()

        class FakeRedis:
            async def blpop(self, queue, timeout=0):
                if not items:
                    return None
                return (queue, items.pop(0))

        with patch.object(queue, "_client", return_value=FakeRedis()):
            worker = IngestionWorker(
                session_factory=factory,
                process=process,
                queues=[QUEUE_EVENTS],
                max_retries=1,
                poll_interval=0.001,
            )
            worker._queue = queue
            drained = await worker.run_once()

        # Corrupt entry skipped (counts nothing), one good job processed.
        assert drained == 1
        assert len(processed) == 1
        assert processed[0] == {"source_id": "good"}


# ---------------------------------------------------------------------------
# Webhook use_enum_values edge-case (fixed during this session)
# ---------------------------------------------------------------------------
class TestWebhookEnumEdgeCase:
    """The webhook route must tolerate use_enum_values (string on wire)"""

    def test_webhook_returns_event_type_string(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post("/api/v1/ingestion/webhook", json={
            "event_type": "LOG",
            "timestamp": "2026-01-01T12:00:00Z",
            "payload": {"message": "enum test"},
            "project_id": project["id"],
        })
        assert resp.status_code == 202
        body = resp.json()
        assert body["event_type"] == "LOG"
        assert isinstance(body["event_type"], str)
