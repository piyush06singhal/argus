"""Tests for observability: events, logs, metrics, traces."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient


class TestObservabilityEvents:
    """Test event ingestion and filtering."""

    def _setup(self, client: TestClient) -> dict:
        project = client.post(
            "/api/v1/projects", json={"name": "Obs Proj", "slug": "obs-proj"}
        ).json()
        env = client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"name": "Prod", "environment_type": "PRODUCTION"},
        ).json()
        comp = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "API", "component_type": "SERVICE"},
        ).json()
        return {"project": project, "env": env, "comp": comp}

    def test_create_event(self, client: TestClient) -> None:
        """Create an observability event."""
        s = self._setup(client)
        response = client.post(
            "/api/v1/observability/events",
            json={
                "project_id": s["project"]["id"],
                "environment_id": s["env"]["id"],
                "component_id": s["comp"]["id"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "test-source",
                "event_type": "SYSTEM_EVENT",
                "severity": "INFO",
                "payload": {"message": "Health check passed"},
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "SYSTEM_EVENT"

    def test_missing_project(self, client: TestClient) -> None:
        """Event without project should be rejected."""
        response = client.post(
            "/api/v1/observability/events",
            json={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "x",
                "event_type": "LOG",
            },
        )
        assert response.status_code == 422

    def test_invalid_event_type(self, client: TestClient) -> None:
        """Invalid event type should be rejected."""
        s = self._setup(client)
        response = client.post(
            "/api/v1/observability/events",
            json={
                "project_id": s["project"]["id"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "x",
                "event_type": "NOT_A_TYPE",
            },
        )
        assert response.status_code == 422

    def test_invalid_timestamp(self, client: TestClient) -> None:
        """Invalid timestamp should be rejected."""
        s = self._setup(client)
        response = client.post(
            "/api/v1/observability/events",
            json={
                "project_id": s["project"]["id"],
                "timestamp": "not-a-date",
                "source": "x",
                "event_type": "LOG",
            },
        )
        assert response.status_code == 422

    def test_list_and_filter_events(self, client: TestClient) -> None:
        """Filter events by severity."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        client.post(
            "/api/v1/observability/events",
            json={
                "project_id": s["project"]["id"],
                "timestamp": ts,
                "source": "x",
                "event_type": "LOG",
                "severity": "ERROR",
            },
        )
        client.post(
            "/api/v1/observability/events",
            json={
                "project_id": s["project"]["id"],
                "timestamp": ts,
                "source": "x",
                "event_type": "LOG",
                "severity": "INFO",
            },
        )

        response = client.get(
            f"/api/v1/projects/{s['project']['id']}/observability/events"
        )
        # No project-scoped event route in Phase 0; use the filter on /events instead
        response = client.get("/api/v1/observability/events?severity=ERROR")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    def test_pagination(self, client: TestClient) -> None:
        """Events should paginate with bounded page sizes."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        for i in range(5):
            client.post(
                "/api/v1/observability/events",
                json={
                    "project_id": s["project"]["id"],
                    "timestamp": ts,
                    "source": "x",
                    "event_type": "LOG",
                },
            )

        response = client.get("/api/v1/observability/events?page_size=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["total"] == 5


class TestLogsMetricsTraces:
    """Test log, metric, and trace ingestion."""

    def _setup(self, client: TestClient) -> dict:
        project = client.post(
            "/api/v1/projects", json={"name": "Obs Proj 2", "slug": "obs-proj-2"}
        ).json()
        return {"project": project}

    def test_create_log(self, client: TestClient) -> None:
        """Create a log record."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        response = client.post(
            "/api/v1/observability/logs",
            json={
                "project_id": s["project"]["id"],
                "timestamp": ts,
                "level": "ERROR",
                "message": "Ambiguous error occurred",
                "service": "checkout-service",
            },
        )
        assert response.status_code == 201
        assert response.json()["level"] == "ERROR"

    def test_filter_logs_by_level(self, client: TestClient) -> None:
        """Filter logs by level."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        for level in ("INFO", "ERROR", "WARN"):
            client.post(
                "/api/v1/observability/logs",
                json={
                    "project_id": s["project"]["id"],
                    "timestamp": ts,
                    "level": level,
                    "message": f"message {level}",
                },
            )

        response = client.get("/api/v1/observability/logs?level=ERROR")
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_create_metric(self, client: TestClient) -> None:
        """Create a metric and query it."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        response = client.post(
            "/api/v1/observability/metrics",
            json={
                "project_id": s["project"]["id"],
                "timestamp": ts,
                "metric_name": "http_requests_total",
                "metric_type": "COUNTER",
                "value": 1234.5,
                "unit": "requests",
            },
        )
        assert response.status_code == 201
        assert response.json()["value"] == 1234.5

    def test_invalid_metric_value(self, client: TestClient) -> None:
        """Non-numeric metric value should be rejected."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        response = client.post(
            "/api/v1/observability/metrics",
            json={
                "project_id": s["project"]["id"],
                "timestamp": ts,
                "metric_name": "x",
                "metric_type": "GAUGE",
                "value": "not-a-number",
            },
        )
        assert response.status_code == 422

    def test_create_trace_and_spans(self, client: TestClient) -> None:
        """Create a trace with parent/child spans."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()

        trace_resp = client.post(
            "/api/v1/observability/traces",
            json={
                "project_id": s["project"]["id"],
                "trace_id": "trace-test-abc",
                "name": "checkout",
                "start_time": ts,
                "duration_ms": 500,
                "status": "OK",
            },
        )
        assert trace_resp.status_code == 201

        span_parent = client.post(
            "/api/v1/observability/traces/spans",
            json={
                "trace_id": "trace-test-abc",
                "span_id": "span-test-1",
                "project_id": s["project"]["id"],
                "operation": "root",
                "start_time": ts,
                "duration_ms": 500,
                "status": "OK",
            },
        )
        assert span_parent.status_code == 201

        span_child = client.post(
            "/api/v1/observability/traces/spans",
            json={
                "trace_id": "trace-test-abc",
                "span_id": "span-test-2",
                "parent_span_id": "span-test-1",
                "project_id": s["project"]["id"],
                "operation": "child",
                "start_time": ts,
                "duration_ms": 100,
                "status": "OK",
            },
        )
        assert span_child.status_code == 201

        # Fetch trace with spans
        response = client.get("/api/v1/observability/traces/trace-test-abc")
        assert response.status_code == 200
        data = response.json()
        assert data["trace"]["trace_id"] == "trace-test-abc"
        assert len(data["spans"]) == 2
        # Verify parent/child relationship
        child = [s for s in data["spans"] if s["span_id"] == "span-test-2"][0]
        assert child["parent_span_id"] == "span-test-1"

    def test_invalid_trace_relationship(self, client: TestClient) -> None:
        """Malformed trace reference should not crash the API."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        response = client.post(
            "/api/v1/observability/traces/spans",
            json={
                "trace_id": "no-such-trace",
                "span_id": "orphan-span",
                "project_id": s["project"]["id"],
                "start_time": ts,
            },
        )
        # Foundation accepts the span; cross-trace validation is a Phase 1 concern
        assert response.status_code == 201

    def test_get_missing_trace(self, client: TestClient) -> None:
        """Querying a missing trace returns 404."""
        response = client.get("/api/v1/observability/traces/does-not-exist")
        assert response.status_code == 404

    def test_duplicate_trace_id_rejected(self, client: TestClient) -> None:
        """Re-using an existing trace_id returns 409, not a 500."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()

        first = client.post(
            "/api/v1/observability/traces",
            json={
                "project_id": s["project"]["id"],
                "trace_id": "dup-trace",
                "name": "first",
                "start_time": ts,
            },
        )
        assert first.status_code == 201

        second = client.post(
            "/api/v1/observability/traces",
            json={
                "project_id": s["project"]["id"],
                "trace_id": "dup-trace",
                "name": "second",
                "start_time": ts,
            },
        )
        assert second.status_code == 409

    def test_duplicate_span_id_rejected(self, client: TestClient) -> None:
        """Re-using an existing span_id returns 409, not a 500."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()

        first = client.post(
            "/api/v1/observability/traces/spans",
            json={
                "trace_id": "sp-dup-trace",
                "span_id": "dup-span",
                "project_id": s["project"]["id"],
                "start_time": ts,
            },
        )
        assert first.status_code == 201

        second = client.post(
            "/api/v1/observability/traces/spans",
            json={
                "trace_id": "sp-dup-trace",
                "span_id": "dup-span",
                "project_id": s["project"]["id"],
                "start_time": ts,
            },
        )
        assert second.status_code == 409
