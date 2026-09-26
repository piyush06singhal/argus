"""Phase 1 tests: ingestion helpers, redaction, fingerprinting, dedup,
source registry routes, batch ingestion, config/health events, stats.
"""

from __future__ import annotations

import re
import socket
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.sources import MockObservabilitySource, RawObservabilityEvent
from app.models.observability import ObservabilityEvent
from app.services.ingestion_helpers import (
    CorrelationEngine,
    EventFingerprint,
)
from app.services.redaction import RedactionEngine


_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helper: create a project via the API (avoids SQLite cross-thread locking)
# ---------------------------------------------------------------------------
def _create_project(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "IngestTest", "slug": _unique_slug("ingest")},
    )
    assert resp.status_code == 201
    return resp.json()


def _broker_reachable() -> bool:
    """Whether a Redis broker is listening, checked without importing redis."""
    from app.core.config import get_settings

    settings = get_settings()
    try:
        with socket.create_connection(
            (settings.REDIS_HOST, settings.REDIS_PORT), timeout=1.0
        ):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# RedactionEngine tests
# ---------------------------------------------------------------------------
class TestRedactionEngine:
    def test_redacts_sensitive_keys(self) -> None:
        engine = RedactionEngine()
        payload = {"password": "s3cr3t", "user": "alice", "api_key": "ABC123"}
        result = engine.redact(payload)
        assert result["password"] == "[REDACTED]"
        assert result["api_key"] == "[REDACTED]"
        assert result["user"] == "alice"

    def test_redacts_jwt_tokens(self) -> None:
        engine = RedactionEngine()
        fake_jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        payload = {"token": fake_jwt, "data": "safe"}
        result = engine.redact(payload)
        assert result["token"] == "[REDACTED]"
        assert result["data"] == "safe"

    def test_redacts_long_hex_strings(self) -> None:
        engine = RedactionEngine()
        payload = {"hash": "a" * 64, "msg": "hello"}
        result = engine.redact(payload)
        assert result["hash"] == "[REDACTED]"
        assert result["msg"] == "hello"

    def test_redacts_nested_dicts(self) -> None:
        engine = RedactionEngine()
        payload = {"outer": {"secret_token": "top", "safe": 1}}
        result = engine.redact(payload)
        assert result["outer"]["secret_token"] == "[REDACTED]"
        assert result["outer"]["safe"] == 1

    def test_redacts_nested_lists(self) -> None:
        engine = RedactionEngine()
        payload = {"items": ["user", "s3cr3t_password_val"]}
        result = engine.redact(payload)
        assert result["items"] == ["user", "s3cr3t_password_val"]

    def test_empty_payload(self) -> None:
        engine = RedactionEngine()
        assert engine.redact({}) == {}
        assert engine.redact(None) == {}

    def test_payload_summary_for_dead_letter(self) -> None:
        engine = RedactionEngine()
        payload = {"password": "secret", "name": "alice", "count": 42}
        summary = engine.payload_summary(payload)
        assert summary["password"] == "[REDACTED]"
        assert summary["name"] == "str"
        assert summary["count"] == "int"

    def test_long_base64_is_redacted(self) -> None:
        engine = RedactionEngine()
        long_b64 = "QUFBQkJCRUNERUZHSElKS0xNTk9QUFFRQUFBQkJD"
        payload = {"data": long_b64}
        result = engine.redact(payload)
        assert result["data"] == "[REDACTED]"

    def test_short_strings_not_redacted(self) -> None:
        engine = RedactionEngine()
        payload = {"token": "short"}
        result = engine.redact(payload)
        assert result["token"] == "short"

    def test_payload_summary_truncates_large_payloads(self) -> None:
        engine = RedactionEngine()
        payload = {f"key{i}": i for i in range(15)}
        summary = engine.payload_summary(payload, max_keys=5)
        assert len(summary) == 6  # 5 items + "..."

    def test_does_not_mutate_original(self) -> None:
        engine = RedactionEngine()
        original = {"password": "s3cr3t", "user": "alice"}
        redacted = engine.redact(original)
        assert original["password"] == "s3cr3t"
        assert redacted["password"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# EventFingerprint tests
# ---------------------------------------------------------------------------
class TestEventFingerprint:
    def test_same_inputs_same_fingerprint(self) -> None:
        pid = uuid.uuid4()
        fp1 = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello"},
        )
        fp2 = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello"},
        )
        assert fp1 == fp2

    def test_different_project_different_fingerprint(self) -> None:
        pid1, pid2 = uuid.uuid4(), uuid.uuid4()
        fp1 = EventFingerprint.compute(
            project_id=pid1,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello"},
        )
        fp2 = EventFingerprint.compute(
            project_id=pid2,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello"},
        )
        assert fp1 != fp2

    def test_volatile_underscore_keys_excluded(self) -> None:
        pid = uuid.uuid4()
        fp1 = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello", "_internal": "ignored"},
        )
        fp2 = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello", "_internal": "changed"},
        )
        assert fp1 == fp2

    def test_stable_keys_override(self) -> None:
        pid = uuid.uuid4()
        fp1 = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello", "latency_ms": 42.1},
            stable_keys=["msg"],
        )
        fp2 = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload={"msg": "hello", "latency_ms": 99.9},
            stable_keys=["msg"],
        )
        assert fp1 == fp2

    def test_empty_payload(self) -> None:
        pid = uuid.uuid4()
        fp = EventFingerprint.compute(
            project_id=pid,
            source="app:svc",
            event_type="LOG",
            timestamp=_TS,
            payload=None,
        )
        assert isinstance(fp, str) and len(fp) == 64

    def test_is_256bit_hex(self) -> None:
        fp = EventFingerprint.compute(
            project_id=uuid.uuid4(),
            source="x",
            event_type="LOG",
            timestamp=_TS,
            payload={"k": "v"},
        )
        assert re.fullmatch(r"[0-9a-f]{64}", fp)


# ---------------------------------------------------------------------------
# CorrelationEngine tests
# ---------------------------------------------------------------------------
class TestCorrelationEngine:
    def test_shared_trace_id_same_correlation(self) -> None:
        engine = CorrelationEngine()
        c1 = engine.correlate(request_id=None, trace_id="trace-A", deployment_id=None)
        c2 = engine.correlate(request_id=None, trace_id="trace-A", deployment_id=None)
        assert c1 == c2

    def test_shared_request_id_same_correlation(self) -> None:
        engine = CorrelationEngine()
        c1 = engine.correlate(request_id="req-1", trace_id=None, deployment_id=None)
        c2 = engine.correlate(request_id="req-1", trace_id=None, deployment_id=None)
        assert c1 == c2

    def test_no_anchors_gets_random_correlation(self) -> None:
        engine = CorrelationEngine()
        c1 = engine.correlate(request_id=None, trace_id=None, deployment_id=None)
        c2 = engine.correlate(request_id=None, trace_id=None, deployment_id=None)
        assert isinstance(c1, str) and len(c1) == 32
        assert c1 != c2

    def test_different_anchors_different_correlations(self) -> None:
        engine = CorrelationEngine()
        c1 = engine.correlate(request_id="r1", trace_id=None, deployment_id=None)
        c2 = engine.correlate(request_id="r2", trace_id=None, deployment_id=None)
        assert c1 != c2

    def test_deployment_id_links_events(self) -> None:
        engine = CorrelationEngine()
        c1 = engine.correlate(
            request_id=None, trace_id=None, deployment_id="deploy-123"
        )
        c2 = engine.correlate(
            request_id=None, trace_id=None, deployment_id="deploy-123"
        )
        assert c1 == c2

    def test_different_trace_ids_different_correlations(self) -> None:
        engine = CorrelationEngine()
        c1 = engine.correlate(request_id=None, trace_id="t1", deployment_id=None)
        c2 = engine.correlate(request_id=None, trace_id="t2", deployment_id=None)
        assert c1 != c2


# ---------------------------------------------------------------------------
# Ingestion pipeline dedup + persistence tests (service-level, use db_session)
# ---------------------------------------------------------------------------
class TestIngestionPipelineDedup:
    async def test_dedup_rejects_duplicate_event(
        self, db_session: AsyncSession
    ) -> None:
        from app.services.ingestion import IngestionPipeline

        project_id = uuid.uuid4()
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db_session,
            project_id=project_id,
        )
        raw = RawObservabilityEvent(
            source_type="mock",
            source_name="dedup-test",
            timestamp=_TS,
            event_type="SYSTEM_EVENT",
            payload={"message": "hello"},
        )
        r1 = await pipeline.ingest_batch([raw])
        assert r1.accepted == 1
        assert r1.duplicates == 0

        r2 = await pipeline.ingest_batch([raw])
        assert r2.accepted == 0
        assert r2.duplicates == 1

    async def test_ingest_batch_counts_failures(self, db_session: AsyncSession) -> None:
        from app.services.ingestion import IngestionPipeline

        project_id = uuid.uuid4()
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db_session,
            project_id=project_id,
        )
        good = RawObservabilityEvent(
            source_type="mock",
            source_name="test",
            timestamp=_TS,
            event_type="SYSTEM_EVENT",
            payload={"message": "ok"},
        )
        bad = RawObservabilityEvent(
            source_type="UNKNOWN!!!",
            source_name="bad",
            timestamp=_TS,
            event_type="SYSTEM_EVENT",
            payload={},
        )
        result = await pipeline.ingest_batch([good, bad])
        assert result.accepted == 1
        assert result.failed == 1

    async def test_correlation_id_is_persisted(self, db_session: AsyncSession) -> None:
        from app.services.ingestion import IngestionPipeline

        project_id = uuid.uuid4()
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db_session,
            project_id=project_id,
        )
        raw = RawObservabilityEvent(
            source_type="mock",
            source_name="corr-test",
            timestamp=_TS,
            event_type="SYSTEM_EVENT",
            payload={"message": "trace me", "trace_id": "abc-123"},
        )
        await pipeline.ingest_batch([raw])

        events = (
            (
                await db_session.execute(
                    select(ObservabilityEvent).where(
                        ObservabilityEvent.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].correlation_id is not None
        assert len(events[0].correlation_id) == 32
        assert events[0].trace_id == "abc-123"

    async def test_fingerprint_is_persisted(self, db_session: AsyncSession) -> None:
        from app.services.ingestion import IngestionPipeline

        project_id = uuid.uuid4()
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db_session,
            project_id=project_id,
        )
        raw = RawObservabilityEvent(
            source_type="mock",
            source_name="fp-test",
            timestamp=_TS,
            event_type="SYSTEM_EVENT",
            payload={"message": "fingerprint me"},
        )
        await pipeline.ingest_batch([raw])

        event = (
            await db_session.execute(
                select(ObservabilityEvent).where(
                    ObservabilityEvent.project_id == project_id
                )
            )
        ).scalar_one()
        assert event.fingerprint is not None
        assert len(event.fingerprint) == 64

    async def test_redaction_in_pipeline(self, db_session: AsyncSession) -> None:
        from app.services.ingestion import IngestionPipeline

        project_id = uuid.uuid4()
        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db_session,
            project_id=project_id,
        )
        raw = RawObservabilityEvent(
            source_type="mock",
            source_name="redact-test",
            timestamp=_TS,
            event_type="SYSTEM_EVENT",
            payload={"message": "hi", "api_key": "LEAKED"},
        )
        await pipeline.ingest_batch([raw])

        event = (
            await db_session.execute(
                select(ObservabilityEvent).where(
                    ObservabilityEvent.project_id == project_id
                )
            )
        ).scalar_one()
        assert event.payload["api_key"] == "[REDACTED]"
        assert event.payload["message"] == "hi"

    async def test_empty_batch(self, db_session: AsyncSession) -> None:
        from app.services.ingestion import IngestionPipeline

        pipeline = IngestionPipeline(
            source=MockObservabilitySource(),
            db=db_session,
            project_id=uuid.uuid4(),
        )
        result = await pipeline.ingest_batch([])
        assert result.accepted == 0
        assert result.duplicates == 0
        assert result.failed == 0


# ---------------------------------------------------------------------------
# Source registry API tests (all data via API, no db_session)
# ---------------------------------------------------------------------------
class TestSourceRegistryAPI:
    def test_create_source(self, client: TestClient) -> None:
        project = _create_project(client)
        pid = project["id"]

        resp = client.post(
            "/api/v1/ingestion/sources",
            json={
                "project_id": pid,
                "name": "app-logger",
                "source_type": "APPLICATION",
                "description": "Main app log stream",
                "configuration": {"endpoint": "https://logs.example.com"},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "app-logger"
        assert body["source_type"] == "APPLICATION"
        assert body["status"] == "UNKNOWN"
        assert body["error_count"] == 0
        source_id = body["id"]

        # GET single
        resp2 = client.get(f"/api/v1/ingestion/sources/{source_id}")
        assert resp2.status_code == 200
        assert resp2.json()["id"] == source_id

        # GET list
        resp3 = client.get("/api/v1/ingestion/sources")
        assert resp3.status_code == 200
        assert resp3.json()["total"] >= 1

        # PATCH
        resp4 = client.patch(
            f"/api/v1/ingestion/sources/{source_id}",
            json={"status": "HEALTHY", "description": "Updated"},
        )
        assert resp4.status_code == 200
        assert resp4.json()["status"] == "HEALTHY"
        assert resp4.json()["description"] == "Updated"

        # DELETE
        resp5 = client.delete(f"/api/v1/ingestion/sources/{source_id}")
        assert resp5.status_code == 204

        # Verify deleted
        resp6 = client.get(f"/api/v1/ingestion/sources/{source_id}")
        assert resp6.status_code == 404

    def test_source_rejects_secrets(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post(
            "/api/v1/ingestion/sources",
            json={
                "project_id": project["id"],
                "name": "bad",
                "source_type": "CUSTOM",
                "configuration": {"api_key": "LEAKED"},
            },
        )
        assert resp.status_code == 422
        assert "api_key" in resp.json()["detail"]

    def test_source_404_on_missing(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/ingestion/sources/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_source_list_filters_by_project(self, client: TestClient) -> None:
        p1 = _create_project(client)
        p2 = _create_project(client)

        client.post(
            "/api/v1/ingestion/sources",
            json={
                "project_id": p1["id"],
                "name": "src1",
                "source_type": "CUSTOM",
            },
        )
        client.post(
            "/api/v1/ingestion/sources",
            json={
                "project_id": p2["id"],
                "name": "src2",
                "source_type": "CUSTOM",
            },
        )

        resp = client.get(f"/api/v1/ingestion/sources?project_id={p1['id']}")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_source_list_is_paginated(self, client: TestClient) -> None:
        """The list is bounded, and `total` reflects the whole registry.

        The source registry grows with the estate, so an uncapped SELECT here
        was the last endpoint whose response size scaled with its table. The
        page cap is the guarantee; `total` is what makes pagination honest.
        """
        project = _create_project(client)
        for i in range(3):
            client.post(
                "/api/v1/ingestion/sources",
                json={
                    "project_id": project["id"],
                    "name": f"paged-src-{i}",
                    "source_type": "CUSTOM",
                },
            )

        # A small page returns a slice, not the registry.
        page1 = client.get(
            f"/api/v1/ingestion/sources?project_id={project['id']}&page_size=2"
        )
        assert page1.status_code == 200
        body = page1.json()
        assert len(body["items"]) == 2
        assert body["total"] == 3

        # The second page carries the remainder, with no overlap.
        page2 = client.get(
            f"/api/v1/ingestion/sources?project_id={project['id']}&page=2&page_size=2"
        )
        assert page2.status_code == 200
        names1 = {s["name"] for s in body["items"]}
        names2 = {s["name"] for s in page2.json()["items"]}
        assert len(names2) == 1
        assert not names1 & names2

        # The cap is enforced, not decorative.
        oversized = client.get(
            f"/api/v1/ingestion/sources?project_id={project['id']}&page_size=1000"
        )
        assert oversized.status_code == 422


# ---------------------------------------------------------------------------
# Config change events API
# ---------------------------------------------------------------------------
class TestConfigChangeEventsAPI:
    def test_create_and_list(self, client: TestClient) -> None:
        project = _create_project(client)
        pid = project["id"]

        payload = {
            "project_id": pid,
            "change_id": "config-001",
            "timestamp": "2026-01-01T12:00:00Z",
            "description": "Added caching layer",
        }
        resp = client.post("/api/v1/ingestion/config-changes", json=payload)
        assert resp.status_code == 201
        assert resp.json()["change_id"] == "config-001"

        resp2 = client.get(f"/api/v1/ingestion/config-changes?project_id={pid}")
        assert resp2.status_code == 200
        assert resp2.json()["total"] == 1

    def test_rejects_secret_keys(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post(
            "/api/v1/ingestion/config-changes",
            json={
                "project_id": project["id"],
                "change_id": "bad",
                "timestamp": "2026-01-01T12:00:00Z",
                "metadata": {"secret": "value"},
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Health check events API
# ---------------------------------------------------------------------------
class TestHealthCheckEventsAPI:
    def test_create_and_list(self, client: TestClient) -> None:
        project = _create_project(client)
        pid = project["id"]

        # Create a component via the API
        comp_resp = client.post(
            f"/api/v1/projects/{pid}/components",
            json={"name": "api-gateway", "component_type": "SERVICE"},
        )
        assert comp_resp.status_code == 201
        comp_id = comp_resp.json()["id"]

        payload = {
            "project_id": pid,
            "component_id": comp_id,
            "timestamp": "2026-01-01T12:00:00Z",
            "status": "HEALTHY",
            "latency_ms": 12.5,
        }
        resp = client.post("/api/v1/ingestion/health-checks", json=payload)
        assert resp.status_code == 201
        assert resp.json()["status"] == "HEALTHY"
        assert resp.json()["latency_ms"] == 12.5

        resp2 = client.get(
            f"/api/v1/ingestion/health-checks?project_id={pid}&status=HEALTHY"
        )
        assert resp2.status_code == 200
        assert resp2.json()["total"] == 1


# ---------------------------------------------------------------------------
# Bulk ingestion API
# ---------------------------------------------------------------------------
class TestBulkIngestionAPI:
    def test_bulk_ingestion_accepted(self, client: TestClient) -> None:
        project = _create_project(client)

        payload = {
            "project_id": project["id"],
            "events": [
                {
                    "source_type": "mock",
                    "source_name": "app-logger",
                    "timestamp": "2026-01-01T12:00:00Z",
                    "event_type": "SYSTEM_EVENT",
                    "payload": {"message": "hello from bulk"},
                },
                {
                    "source_type": "mock",
                    "source_name": "prometheus",
                    "timestamp": "2026-01-01T12:00:01Z",
                    "event_type": "SYSTEM_EVENT",
                    "payload": {"metric_name": "cpu", "value": 0.8},
                },
            ],
        }
        resp = client.post("/api/v1/ingestion/bulk", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 2
        assert body["duplicates"] == 0
        assert body["failed"] == 0

    def test_bulk_rejects_secret_keys(self, client: TestClient) -> None:
        project = _create_project(client)
        resp = client.post(
            "/api/v1/ingestion/bulk",
            json={
                "project_id": project["id"],
                "events": [
                    {
                        "source_type": "mock",
                        "source_name": "app",
                        "timestamp": "2026-01-01T12:00:00Z",
                        "event_type": "SYSTEM_EVENT",
                        "payload": {"password": "secret123", "msg": "hi"},
                    }
                ],
            },
        )
        assert resp.status_code == 422
        assert "password" in resp.json()["detail"]

    def test_bulk_deduplicates(self, client: TestClient) -> None:
        project = _create_project(client)
        event = {
            "source_type": "mock",
            "source_name": "dup",
            "timestamp": "2026-01-01T12:00:00Z",
            "event_type": "SYSTEM_EVENT",
            "payload": {"message": "same"},
        }
        resp = client.post(
            "/api/v1/ingestion/bulk",
            json={
                "project_id": project["id"],
                "events": [event, event],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["duplicates"] == 1


# ---------------------------------------------------------------------------
# Ingestion stats API
# ---------------------------------------------------------------------------
class TestIngestionStatsAPI:
    def test_stats_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/ingestion/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_count"] == 0
        assert body["dead_letter_count"] == 0
        assert body["healthy_sources"] == 0
        assert body["failing_sources"] == 0


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
class TestWebhookAPI:
    def test_webhook_receives_event(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/ingestion/webhook",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {"message": "webhook event"},
                "source_name": "github-webhook",
                "project_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 202
        assert resp.json()["received"] is True
        assert resp.json()["event_type"] == "LOG"
        assert resp.json()["queued"] is True

    def test_webhook_rejects_secrets(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/ingestion/webhook",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {"token": "secret"},
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Queue endpoint (redis-unavailable graceful degradation)
# ---------------------------------------------------------------------------
class TestIngestionQueueDegradation:
    def test_queue_endpoint_falls_back_to_sync(self, client: TestClient) -> None:
        """When Redis is unreachable, the queue endpoint processes synchronously."""
        from unittest.mock import patch, AsyncMock
        from app.services.queue import QueueUnavailable

        project = _create_project(client)

        # Mock the queue push to raise QueueUnavailable
        with patch("app.api.v1.routes.ingestion.IngestionQueue") as MockQueue:
            mock_instance = MockQueue.return_value
            mock_instance.push = AsyncMock(side_effect=QueueUnavailable("no redis"))

            resp = client.post(
                "/api/v1/ingestion/queue",
                json={
                    "project_id": project["id"],
                    "events": [
                        {
                            "source_type": "mock",
                            "source_name": "queue-fallback",
                            "timestamp": "2026-01-01T12:00:00Z",
                            "event_type": "SYSTEM_EVENT",
                            "payload": {"message": "degraded"},
                        }
                    ],
                },
            )
            # Should be 200 (sync fallback) not 202 (queued)
            assert resp.status_code == 200
            body = resp.json()
            assert body["queued"] is False
            assert body["reason"] == "broker_unavailable_processed_sync"
            assert body["accepted"] == 1

    def test_queue_endpoint_returns_202_when_broker_available(
        self, client: TestClient
    ) -> None:
        """When Redis is available, the queue endpoint returns 202 accepted.

        Skips — rather than fails — when no broker is reachable. The assertion
        is about what the endpoint does *given* a broker; a machine without
        Redis cannot make a claim either way, and a suite that fails there is
        reporting the environment, not the code.
        """
        if not _broker_reachable():
            pytest.skip("no Redis broker reachable, so 202 cannot be observed")
        project = _create_project(client)
        resp = client.post(
            "/api/v1/ingestion/queue",
            json={
                "project_id": project["id"],
                "events": [
                    {
                        "source_type": "mock",
                        "source_name": "queue-ok",
                        "timestamp": "2026-01-01T12:00:00Z",
                        "event_type": "SYSTEM_EVENT",
                        "payload": {"message": "queued"},
                    }
                ],
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["queued"] is True


# ---------------------------------------------------------------------------
# Dead-letter list
# ---------------------------------------------------------------------------
class TestDeadLetterAPI:
    def test_dead_letter_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/ingestion/dead-letter")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# IngestionWorker (Redis queue consumer §37): retry + backoff + dead-letter
# ---------------------------------------------------------------------------
class _FakeQueue:
    """In-memory queue that mimics IngestionQueue.pop/push without Redis."""

    def __init__(self, initial: list[dict] | None = None):
        from collections import deque

        self._items: deque[dict] = deque(initial or [])
        self.pushed: list[dict] = []

    async def pop(
        self, queue_name: str | None = None, timeout: float = 0.0
    ) -> dict | None:
        return self._items.popleft() if self._items else None

    async def push(self, job: dict) -> None:
        self._items.append(job)
        self.pushed.append(job)


class TestIngestionWorker:
    """Phase 1 §37–§44: worker retry, backoff, dead-letter, and batching."""

    @staticmethod
    def _make_worker(db_engine, process_fn, *, max_retries=2, queues=None):
        from app.services.queue import IngestionWorker, QUEUE_EVENTS
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        return IngestionWorker(
            session_factory=factory,
            process=process_fn,
            queues=queues or [QUEUE_EVENTS],
            max_retries=max_retries,
            poll_interval=0.001,
        )

    # -- success path (no retry) ----------------------------------------------
    async def test_worker_success_no_retry(self, db_engine) -> None:
        from unittest.mock import patch
        from app.services.queue import make_job

        processed: list[tuple] = []

        async def process(kind, payload):
            processed.append((kind, payload))

        fake = _FakeQueue(initial=[make_job(kind="event", payload={"source_id": "s1"})])
        with patch("app.services.queue.IngestionQueue", return_value=fake):
            worker = self._make_worker(db_engine, process)
            drained = await worker.run_once()

        assert drained == 1
        assert len(processed) == 1
        assert processed[0] == ("event", {"source_id": "s1"})
        assert fake.pushed == []  # no re-enqueue needed

    # -- retry then succeed ---------------------------------------------------
    async def test_worker_retries_then_succeeds(self, db_engine) -> None:
        from unittest.mock import patch
        from app.services.queue import make_job

        attempts: list[str] = []

        async def process(kind, payload):
            attempts.append(payload.get("source_id"))
            if len(attempts) == 1:
                raise RuntimeError("transient")

        initial = make_job(kind="event", payload={"source_id": "fragile"})
        fake = _FakeQueue(initial=[initial])

        with (
            patch("app.services.queue.IngestionQueue", return_value=fake),
            patch("app.services.queue.BACKOFF_SECONDS", 0.001),
        ):
            worker = self._make_worker(db_engine, process, max_retries=2)
            # run_once drains in a loop, so the re-enqueued retry job is picked
            # up within the same batch: fail → re-enqueue(retries=1) → succeed.
            drained = await worker.run_once()

        assert drained == 2
        assert attempts == ["fragile", "fragile"]
        assert len(fake.pushed) == 1
        assert fake.pushed[0]["_retries"] == 1

    # -- dead-letter after exhausted retries ----------------------------------
    async def test_worker_dead_letters_after_exhausted_retries(self, db_engine) -> None:
        from unittest.mock import patch
        from app.services.queue import make_job
        from app.models.ingestion import IngestionFailure
        from sqlalchemy import select

        async def process(kind, payload):
            raise RuntimeError("always fails")

        initial = make_job(
            kind="event",
            payload={"source_id": "doomed", "secret": "hunter2", "msg": "boom"},
        )
        fake = _FakeQueue(initial=[initial])

        with (
            patch("app.services.queue.IngestionQueue", return_value=fake),
            patch("app.services.queue.BACKOFF_SECONDS", 0.001),
        ):
            worker = self._make_worker(db_engine, process, max_retries=2)
            # retries=0 → re-enqueue retries=1
            await worker.run_once()
            # retries=1 → re-enqueue retries=2
            await worker.run_once()
            # retries=2 (== max_retries) → dead-letter, no further push
            await worker.run_once()

        assert len(fake.pushed) == 2  # only retries 1 and 2 were pushed
        assert fake.pushed[0]["_retries"] == 1
        assert fake.pushed[1]["_retries"] == 2

        # Persisted to ingestion_failures
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            rows = (await session.execute(select(IngestionFailure))).scalars().all()
            assert len(rows) == 1
            f = rows[0]
            assert f.error_type == "RuntimeError"
            assert f.retry_count == 2
            # payload_summary is redacted (sensitive keys → [REDACTED])
            assert f.payload_summary["secret"] == "[REDACTED]"
            assert f.payload_summary["source_id"] == "str"
            assert f.payload_summary["msg"] == "str"

    # -- permanent failures skip retries --------------------------------------
    async def test_worker_permanent_error_dead_letters_immediately(
        self, db_engine
    ) -> None:
        from unittest.mock import patch
        from app.services.queue import PermanentJobError, make_job
        from app.models.ingestion import IngestionFailure

        attempts: list[str] = []

        async def process(kind, payload):
            attempts.append(payload.get("id"))
            raise PermanentJobError("SoftwareProject ... not found")

        fake = _FakeQueue(initial=[make_job(kind="event", payload={"id": "gone"})])
        with (
            patch("app.services.queue.IngestionQueue", return_value=fake),
            patch("app.services.queue.BACKOFF_SECONDS", 0.001),
        ):
            worker = self._make_worker(db_engine, process, max_retries=3)
            drained = await worker.run_once()

        assert drained == 1
        assert attempts == ["gone"]  # tried exactly once — no retry churn
        assert fake.pushed == []  # nothing re-enqueued

        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            rows = (await session.execute(select(IngestionFailure))).scalars().all()
            assert len(rows) == 1
            assert rows[0].error_type == "PermanentJobError"
            assert rows[0].retry_count == 0

    async def test_worker_dead_letter_survives_deleted_project(self, db_engine) -> None:
        """A failure for a now-deleted project still records (no FK violation)."""
        from unittest.mock import patch
        from app.services.queue import PermanentJobError, make_job
        from app.models.ingestion import IngestionFailure
        import uuid as uuid_mod

        async def process(kind, payload):
            raise PermanentJobError("SoftwareProject ghost not found")

        ghost_id = str(uuid_mod.uuid4())
        fake = _FakeQueue(
            initial=[make_job(kind="graph_extract", payload={"project_id": ghost_id})]
        )
        with patch("app.services.queue.IngestionQueue", return_value=fake):
            worker = self._make_worker(db_engine, process, max_retries=3)
            await worker.run_once()

        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            rows = (await session.execute(select(IngestionFailure))).scalars().all()
            assert len(rows) == 1
            assert rows[0].project_id is None  # dangling id not bound to FK
            # The id survives only as a redacted key/type summary (never a value).
            assert rows[0].payload_summary.get("project_id") == "str"

    # -- batching (run_once drains up to max_jobs_per_batch) -------------------
    async def test_worker_batching_limits_drain(self, db_engine) -> None:
        from unittest.mock import patch
        from app.services.queue import make_job

        processed: list[str] = []

        async def process(kind, payload):
            processed.append(payload.get("id"))

        jobs = [make_job(kind="event", payload={"id": f"j{i}"}) for i in range(5)]
        fake = _FakeQueue(initial=jobs)

        with patch("app.services.queue.IngestionQueue", return_value=fake):
            worker = self._make_worker(db_engine, process)
            worker._max_jobs_per_batch = 3
            drained = await worker.run_once()

        assert drained == 3
        assert processed == ["j0", "j1", "j2"]
        assert len(fake._items) == 2  # j3, j4 remain

    # -- run_forever / stop ----------------------------------------------------
    async def test_worker_run_forever_stops(self, db_engine) -> None:
        import asyncio
        from unittest.mock import patch
        from app.services.queue import make_job

        processed: list[str] = []

        async def process(kind, payload):
            processed.append(payload.get("id"))

        fake = _FakeQueue(initial=[make_job(kind="event", payload={"id": "bg1"})])

        with patch("app.services.queue.IngestionQueue", return_value=fake):
            worker = self._make_worker(db_engine, process, max_retries=0)
            task = asyncio.create_task(worker.run_forever())
            await asyncio.sleep(0.05)
            worker.stop()
            await asyncio.wait_for(task, timeout=2)

        assert "bg1" in processed
