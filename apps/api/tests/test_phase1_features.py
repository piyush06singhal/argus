"""Phase 1 tests: OTLP ingestion, Prometheus scrape, data retention,
trace cross-reference validation, orphan span detection.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.observability import (
    ObservabilityEvent,
    SpanRecord,
    TraceRecord,
)


_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helper: create a project via the API
# ---------------------------------------------------------------------------
def _create_project(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "Phase1FeatTest", "slug": _unique_slug("p1feat")},
    )
    assert resp.status_code == 201
    return resp.json()


# ===================================================================
# OTLP Adapter unit tests
# ===================================================================
class TestOTLPAdapter:
    """Test the OTLP adapter conversion logic (no DB needed)."""

    def test_convert_spans_basic(self) -> None:
        from app.services.otlp_adapter import OTLPAdapter

        adapter = OTLPAdapter()
        payload = {
            "resource_spans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"string_value": "api-gw"}},
                        ]
                    },
                    "spans": [
                        {
                            "trace_id": "abc123",
                            "span_id": "def456",
                            "parent_span_id": None,
                            "name": "HTTP GET /users",
                            "kind": 2,
                            "start_time_unix_nano": 1704110400000000000,
                            "end_time_unix_nano": 1704110400100000000,
                            "attributes": [],
                            "status": {"code": 1, "message": ""},
                            "events": [],
                        },
                        {
                            "trace_id": "abc123",
                            "span_id": "ghi789",
                            "parent_span_id": "def456",
                            "name": "db.query",
                            "kind": 1,
                            "start_time_unix_nano": 1704110400020000000,
                            "end_time_unix_nano": 1704110400080000000,
                            "attributes": [
                                {"key": "db.statement", "value": {"string_value": "SELECT *"}},
                            ],
                            "status": {"code": 1, "message": ""},
                            "events": [],
                        },
                    ],
                }
            ]
        }

        raw_events = adapter.convert_spans(payload, source_name="api-gw")
        assert len(raw_events) == 2
        assert raw_events[0].source_type == "trace"
        assert raw_events[0].source_name == "api-gw"
        assert raw_events[0].event_type == "TRACE"
        assert raw_events[0].payload["trace_id"] == "abc123"
        assert raw_events[0].payload["span_id"] == "def456"
        assert raw_events[0].payload["name"] == "HTTP GET /users"
        assert raw_events[0].payload["kind"] == "SERVER"
        assert raw_events[1].payload["parent_span_id"] == "def456"
        assert raw_events[1].payload["duration_ms"] == pytest.approx(60.0, abs=1)

    def test_convert_spans_empty(self) -> None:
        from app.services.otlp_adapter import OTLPAdapter

        adapter = OTLPAdapter()
        raw_events = adapter.convert_spans({"resource_spans": []})
        assert raw_events == []

    def test_convert_logs_basic(self) -> None:
        from app.services.otlp_adapter import OTLPAdapter

        adapter = OTLPAdapter()
        payload = {
            "resource_logs": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"string_value": "payment-svc"}},
                        ]
                    },
                    "log_records": [
                        {
                            "time_unix_nano": 1704110400000000000,
                            "severity_number": 17,
                            "severity_text": "ERROR",
                            "body": "Connection refused",
                            "attributes": [],
                            "trace_id": "trace-abc",
                            "span_id": "span-def",
                        }
                    ],
                }
            ]
        }

        raw_events = adapter.convert_logs(payload, source_name="payment")
        assert len(raw_events) == 1
        event = raw_events[0]
        assert event.source_type == "log"
        assert event.source_name == "payment-svc"
        assert event.event_type == "LOG"
        assert event.payload["severity"] == "ERROR"
        assert event.payload["message"] == "Connection refused"
        assert event.payload["trace_id"] == "trace-abc"

    def test_convert_metrics_gauge(self) -> None:
        from app.services.otlp_adapter import OTLPAdapter

        adapter = OTLPAdapter()
        payload = {
            "resource_metrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"string_value": "api"}},
                        ]
                    },
                    "metrics": [
                        {
                            "name": "http_requests_total",
                            "description": "Total HTTP requests",
                            "unit": "1",
                            "gauge": {
                                "data_points": [
                                    {
                                        "start_time_unix_nano": 1704110400000000000,
                                        "as_double": 42.5,
                                        "attributes": [
                                            {"key": "method", "value": {"string_value": "GET"}},
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }

        raw_events = adapter.convert_metrics(payload, source_name="api")
        assert len(raw_events) == 1
        event = raw_events[0]
        assert event.source_type == "metric"
        assert event.source_name == "api"
        assert event.event_type == "METRIC"
        assert event.payload["metric_name"] == "http_requests_total"
        assert event.payload["value"] == 42.5
        assert event.payload["labels"]["method"] == "GET"

    def test_convert_metrics_empty(self) -> None:
        from app.services.otlp_adapter import OTLPAdapter

        adapter = OTLPAdapter()
        raw_events = adapter.convert_metrics({"resource_metrics": []})
        assert raw_events == []

    def test_convert_logs_severity_mapping(self) -> None:
        """OTLP severity numbers map to ARGUS severity strings."""
        from app.services.otlp_adapter import OTLPAdapter

        adapter = OTLPAdapter()
        # severity_number 9 = INFO
        payload = {
            "resource_logs": [
                {
                    "resource": {"attributes": []},
                    "log_records": [
                        {
                            "time_unix_nano": 1704110400000000000,
                            "severity_number": 9,
                            "severity_text": "INFO",
                            "body": "test",
                            "attributes": [],
                        }
                    ],
                }
            ]
        }
        raw_events = adapter.convert_logs(payload)
        assert raw_events[0].payload["severity"] == "INFO"


def test_convert_spec_conformant_camelcase() -> None:
    """Accept the protojson (camelCase) OTLP shape emitted by real collectors.

    The OTLP JSON wire format is lowerCamelCase: traceId/spanId/timeUnixNano,
    stringValue for AnyValues, dataPoints/asDouble on metric point values,
    and log records nested under scopeLogs[].logRecords.
    """
    from app.services.otlp_adapter import OTLPAdapter

    adapter = OTLPAdapter()

    # --- traces (camelCase span fields) ---
    traces = adapter.convert_spans({
        "resource_spans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "cart-svc"}},
            ]},
            "spans": [{
                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                "spanId": "00f067aa0ba902b7",
                "parentSpanId": "",
                "name": "process request",
                "kind": 2,
                # protojson encodes uint64 nanos as base-10 *strings*, not numbers.
                "startTimeUnixNano": "1704110400000000000",
                "endTimeUnixNano": "1704110400050000000",
                "attributes": [],
                "events": [],
            }],
        }],
    })
    assert len(traces) == 1
    assert traces[0].payload["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert traces[0].payload["span_id"] == "00f067aa0ba902b7"
    assert traces[0].payload["duration_ms"] == 50.0  # 50ms between ns timestamps

    # --- logs nested under scopeLogs[].logRecords (protojson shape) ---
    logs = adapter.convert_logs({
        "resource_logs": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "cart-svc"}},
            ]},
            "scopeLogs": [{
                "logRecords": [{
                    "timeUnixNano": "1704110400000000000",
                    "severityNumber": 17,
                    "severityText": "ERROR",
                    "body": {"stringValue": "checkout failed"},
                    "attributes": [{"key": "http.method", "value": {"stringValue": "POST"}}],
                }],
            }],
        }],
    })
    assert len(logs) == 1
    assert logs[0].payload["severity"] == "ERROR"
    assert logs[0].payload["message"] == "checkout failed"
    assert logs[0].payload["attributes"]["http.method"] == "POST"

    # --- metrics (dataPoints / asDouble camelCase) ---
    metrics = adapter.convert_metrics({
        "resource_metrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "cart-svc"}},
            ]},
            "metrics": [{
                "name": "cart_checkout_duration",
                "unit": "ms",
                "gauge": {
                    "dataPoints": [{
                        "startTimeUnixNano": "1704110400000000000",
                        "timeUnixNano": "1704110400050000000",
                        "asDouble": 8.5,
                    }],
                },
            }],
        }],
    })
    assert len(metrics) == 1
    assert metrics[0].payload["metric_name"] == "cart_checkout_duration"
    assert metrics[0].payload["value"] == 8.5


# ===================================================================
# OTLP Route tests (through the API)
# ===================================================================
class TestOTLPRoutes:
    """Integration tests for the OTLP ingestion routes."""

    def test_ingest_otlp_traces(self) -> None:
        client = TestClient.__new__(TestClient)
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        payload = {
            "project_id": project_id,
            "resource_spans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"string_value": "test-svc"}},
                        ]
                    },
                    "spans": [
                        {
                            "trace_id": "trace-otlp-001",
                            "span_id": "span-otlp-001",
                            "parent_span_id": None,
                            "name": "test-operation",
                            "kind": 2,
                            "start_time_unix_nano": 1704110400000000000,
                            "end_time_unix_nano": 1704110400050000000,
                            "attributes": [],
                            "status": {"code": 1, "message": ""},
                            "events": [],
                        }
                    ],
                }
            ],
        }

        resp = client.post("/api/v1/otlp/v1/traces", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] >= 1
        assert data["failed"] == 0

    def test_ingest_otlp_logs(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        payload = {
            "project_id": project_id,
            "resource_logs": [
                {
                    "resource": {"attributes": []},
                    "log_records": [
                        {
                            "time_unix_nano": 1704110400000000000,
                            "severity_number": 17,
                            "severity_text": "ERROR",
                            "body": "Something failed",
                            "attributes": [],
                        }
                    ],
                }
            ],
        }

        resp = client.post("/api/v1/otlp/v1/logs", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] >= 1
        assert data["failed"] == 0

    def test_ingest_otlp_metrics(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        payload = {
            "project_id": project_id,
            "resource_metrics": [
                {
                    "resource": {"attributes": []},
                    "metrics": [
                        {
                            "name": "cpu_usage",
                            "description": "CPU usage percentage",
                            "unit": "%",
                            "gauge": {
                                "data_points": [
                                    {
                                        "start_time_unix_nano": 1704110400000000000,
                                        "as_double": 75.3,
                                        "attributes": [],
                                    }
                                ]
                            },
                        }
                    ],
                }
            ],
        }

        resp = client.post("/api/v1/otlp/v1/metrics", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] >= 1
        assert data["failed"] == 0

    def test_ingest_otlp_traces_protojson_camel_case(self) -> None:
        """§17: real OTLP protojson wire format is accepted end to end.

        Protojson encodes top-level keys lowerCamelCase (``resourceSpans``,
        ``scopeSpans``), field names inside spans are camelCase (``traceId``,
        ``startTimeUnixNano``), nanosecond timestamps are base-10 *strings*,
        and ARGUS multi-tenancy fields accept the camelCase ``projectId``.
        """
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        now_ns = 1704110400000000000
        payload = {
            "projectId": project_id,
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "camel-svc"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "name": "test-scope",
                            "spans": [
                                {
                                    "traceId": "camelcase-trace-001",
                                    "spanId": "ab11cd22ef33aa44",
                                    "parentSpanId": None,
                                    "name": "camel-root",
                                    "kind": 2,
                                    "startTimeUnixNano": str(now_ns),
                                    "endTimeUnixNano": str(now_ns + 50_000_000),
                                    "attributes": [],
                                    "status": {"code": 1, "message": ""},
                                    "events": [],
                                },
                                {
                                    "traceId": "camelcase-trace-001",
                                    "spanId": "bb11cd22ef33aa44",
                                    "parentSpanId": "ab11cd22ef33aa44",
                                    "name": "camel-child",
                                    "kind": 3,
                                    "startTimeUnixNano": str(now_ns),
                                    "endTimeUnixNano": str(now_ns + 50_000_000),
                                    "attributes": [],
                                    "status": {"code": 1, "message": ""},
                                    "events": [],
                                },
                            ],
                        }
                    ],
                }
            ],
        }

        resp = client.post("/api/v1/otlp/v1/traces", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 2
        assert data["failed"] == 0

        # Both spans persisted as events under the correct project.
        events = client.get(
            f"/api/v1/observability/events?project_id={project_id}&event_type=TRACE"
        )
        bodies = [e["payload"] for e in events.json()["items"]]
        assert any(e.get("span_id") == "ab11cd22ef33aa44" for e in bodies)
        assert any(e.get("span_id") == "bb11cd22ef33aa44" for e in bodies)

    def test_ingest_otlp_logs_protojson_camel_case(self) -> None:
        """§17: camelCase OTLP log records (string nanos) accepted."""
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        payload = {
            "projectId": project_id,
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": "1704110400000000000",
                                    "severityNumber": 9,
                                    "severityText": "INFO",
                                    "body": {"stringValue": "camel otlp log"},
                                    "attributes": [],
                                }
                            ]
                        }
                    ],
                }
            ],
        }

        resp = client.post("/api/v1/otlp/v1/logs", json=payload)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 1

    def test_ingest_otlp_traces_empty(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        resp = client.post("/api/v1/otlp/v1/traces", json={
            "project_id": project_id,
            "resource_spans": [],
        })
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 0


# ===================================================================
# Prometheus scrape endpoint tests
# ===================================================================
class TestPrometheusScrape:
    """Tests for the Prometheus-compatible /metrics endpoint."""

    def test_metrics_endpoint_returns_text(self) -> None:
        from app.main import app
        client = TestClient(app)

        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "argus_api_up" in resp.text
        assert "argus_sources_total" in resp.text

    def test_metrics_endpoint_includes_expected_metrics(self) -> None:
        from app.main import app
        client = TestClient(app)

        resp = client.get("/metrics")
        text = resp.text
        assert "# HELP argus_sources_total" in text
        assert "# TYPE argus_sources_total" in text
        assert "argus_dead_letter_total" in text
        assert "argus_events_24h_total" in text
        assert "argus_traces_24h_total" in text
        assert "argus_api_up 1.0" in text

    def test_metrics_with_project_filter(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)

        resp = client.get(f"/metrics?project_id={project['id']}")
        assert resp.status_code == 200
        assert "argus_api_up" in resp.text

    def test_metrics_always_reachable(self) -> None:
        """The /metrics endpoint must never error (Prometheus needs a stable target)."""
        from app.main import app
        client = TestClient(app)

        resp = client.get("/metrics")
        assert resp.status_code == 200
        # Verify valid Prometheus text format
        lines = resp.text.strip().split("\n")
        assert len(lines) > 0
        # Must contain HELP/TYPE metadata
        help_lines = [l for l in lines if l.startswith("# HELP")]
        assert len(help_lines) > 0


# ===================================================================
# Data retention tests
# ===================================================================
class TestRetentionPolicy:
    """Tests for data retention policy service and API."""

    def test_retention_policy_endpoint(self) -> None:
        from app.main import app
        client = TestClient(app)

        resp = client.get("/api/v1/ingestion/retention/policy")
        assert resp.status_code == 200
        data = resp.json()
        assert "observability_events" in data
        assert "log_records" in data
        assert "metric_records" in data
        assert "traces" in data
        assert "spans" in data
        assert data["traces"] == 30  # default
        assert data["log_records"] == 90  # default

    def test_retention_preview_empty_db(self) -> None:
        from app.main import app
        client = TestClient(app)

        resp = client.get("/api/v1/ingestion/retention/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert data["total_deleted"] == 0

    def test_retention_sweep_empty_db(self) -> None:
        from app.main import app
        client = TestClient(app)

        resp = client.post("/api/v1/ingestion/retention/sweep")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_deleted"] == 0

    def test_retention_preview_shows_old_data(self) -> None:
        """Insert an event dated far in the past, verify preview finds it."""
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        # Insert an event dated 200 days ago (exceeds default 90d retention)
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=200)).isoformat()
        resp = client.post("/api/v1/observability/events", json={
            "project_id": project_id,
            "timestamp": old_ts,
            "source": "retention-test",
            "event_type": "SYSTEM_EVENT",
            "payload": {"message": "old event"},
        })
        assert resp.status_code == 201

        # Preview should show 1 event to delete
        resp = client.get("/api/v1/ingestion/retention/preview")
        assert resp.status_code == 200
        data = resp.json()
        events_row = next(r for r in data["results"] if r["table"] == "observability_events")
        assert events_row["deleted"] >= 1

    def test_retention_sweep_deletes_old_data(self) -> None:
        """Insert an event dated far in the past, sweep should remove it."""
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        # Insert an event dated 200 days ago
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=200)).isoformat()
        resp = client.post("/api/v1/observability/events", json={
            "project_id": project_id,
            "timestamp": old_ts,
            "source": "retention-test",
            "event_type": "SYSTEM_EVENT",
            "payload": {"message": "old event to delete"},
        })
        assert resp.status_code == 201

        # Sweep
        resp = client.post("/api/v1/ingestion/retention/sweep")
        assert resp.status_code == 200
        data = resp.json()
        events_row = next(r for r in data["results"] if r["table"] == "observability_events")
        assert events_row["deleted"] >= 1

    def test_retention_keeps_recent_data(self) -> None:
        """Recent data should survive a retention sweep."""
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)
        project_id = project["id"]

        # Insert a recent event
        recent_ts = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
        resp = client.post("/api/v1/observability/events", json={
            "project_id": project_id,
            "timestamp": recent_ts,
            "source": "retention-test",
            "event_type": "SYSTEM_EVENT",
            "payload": {"message": "recent event"},
        })
        assert resp.status_code == 201

        # Sweep should not delete recent data
        resp = client.post("/api/v1/ingestion/retention/sweep")
        assert resp.status_code == 200

        # Verify event still exists
        resp = client.get(f"/api/v1/observability/events?project_id={project_id}")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1


# ===================================================================
# Trace cross-reference validation tests
# ===================================================================
class TestTraceValidation:
    """Tests for trace cross-reference validation and orphan span detection."""

    def _create_trace_and_spans(self, client: TestClient, project_id: str) -> dict:
        """Helper: create a trace with parent + child spans."""
        # Create trace
        resp = client.post("/api/v1/observability/traces", json={
            "project_id": project_id,
            "trace_id": "trace-val-001",
            "name": "test-trace",
            "start_time": _TS.isoformat(),
            "end_time": (datetime(2026, 1, 1, 12, 0, 0, 100000, tzinfo=timezone.utc)).isoformat(),
            "duration_ms": 0.1,
            "status": "OK",
        })
        assert resp.status_code == 201

        # Create parent span
        resp = client.post("/api/v1/observability/traces/spans", json={
            "trace_id": "trace-val-001",
            "span_id": "span-parent-001",
            "parent_span_id": None,
            "project_id": project_id,
            "operation": "HTTP GET /users",
            "start_time": _TS.isoformat(),
            "end_time": (datetime(2026, 1, 1, 12, 0, 0, 80000, tzinfo=timezone.utc)).isoformat(),
            "duration_ms": 0.08,
            "status": "OK",
        })
        assert resp.status_code == 201

        # Create child span
        resp = client.post("/api/v1/observability/traces/spans", json={
            "trace_id": "trace-val-001",
            "span_id": "span-child-001",
            "parent_span_id": "span-parent-001",
            "project_id": project_id,
            "operation": "db.query",
            "start_time": _TS.isoformat(),
            "end_time": (datetime(2026, 1, 1, 12, 0, 0, 50000, tzinfo=timezone.utc)).isoformat(),
            "duration_ms": 0.05,
            "status": "OK",
        })
        assert resp.status_code == 201

    def test_validate_trace_valid(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)

        self._create_trace_and_spans(client, project["id"])

        resp = client.get("/api/v1/ingestion/trace-validation/trace-val-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_id"] == "trace-val-001"
        assert data["total_spans"] == 2
        assert data["orphan_spans"] == []
        assert data["missing_trace_record"] is False
        assert data["is_valid"] is True

    def test_validate_trace_with_orphan(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)

        # Create trace
        resp = client.post("/api/v1/observability/traces", json={
            "project_id": project["id"],
            "trace_id": "trace-orphan-001",
            "name": "orphan-trace",
            "start_time": _TS.isoformat(),
            "status": "OK",
        })
        assert resp.status_code == 201

        # Create span with non-existent parent
        resp = client.post("/api/v1/observability/traces/spans", json={
            "trace_id": "trace-orphan-001",
            "span_id": "orphan-span-001",
            "parent_span_id": "nonexistent-parent",
            "project_id": project["id"],
            "operation": "orphan-op",
            "start_time": _TS.isoformat(),
            "status": "OK",
        })
        assert resp.status_code == 201

        resp = client.get("/api/v1/ingestion/trace-validation/trace-orphan-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_spans"] == 1
        assert len(data["orphan_spans"]) == 1
        assert data["orphan_spans"][0]["span_id"] == "orphan-span-001"
        assert data["orphan_spans"][0]["parent_span_id"] == "nonexistent-parent"
        assert data["is_valid"] is False

    def test_validate_trace_missing_trace_record(self) -> None:
        """A trace_id with spans but no TraceRecord should be flagged."""
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)

        # Create span referencing a trace that doesn't exist as a TraceRecord
        resp = client.post("/api/v1/observability/traces/spans", json={
            "trace_id": "trace-missing-record",
            "span_id": "span-no-record",
            "parent_span_id": None,
            "project_id": project["id"],
            "operation": "orphan-trace",
            "start_time": _TS.isoformat(),
            "status": "OK",
        })
        assert resp.status_code == 201

        resp = client.get("/api/v1/ingestion/trace-validation/trace-missing-record")
        assert resp.status_code == 200
        data = resp.json()
        assert data["missing_trace_record"] is True
        assert data["is_valid"] is False

    def test_find_orphan_spans(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)

        # Create a valid trace
        self._create_trace_and_spans(client, project["id"])

        # Create an orphan span
        resp = client.post("/api/v1/observability/traces", json={
            "project_id": project["id"],
            "trace_id": "trace-orphan-list",
            "name": "orphan-list-trace",
            "start_time": _TS.isoformat(),
            "status": "OK",
        })
        assert resp.status_code == 201

        resp = client.post("/api/v1/observability/traces/spans", json={
            "trace_id": "trace-orphan-list",
            "span_id": "orphan-in-list",
            "parent_span_id": "nonexistent-list-parent",
            "project_id": project["id"],
            "operation": "orphan-list-op",
            "start_time": _TS.isoformat(),
            "status": "OK",
        })
        assert resp.status_code == 201

        resp = client.get(f"/api/v1/ingestion/orphan-spans/{project['id']}")
        assert resp.status_code == 200
        orphans = resp.json()
        assert len(orphans) >= 1
        orphan_ids = [o["span_id"] for o in orphans]
        assert "orphan-in-list" in orphan_ids

    def test_validate_project_traces(self) -> None:
        from app.main import app
        client = TestClient(app)
        project = _create_project(client)

        self._create_trace_and_spans(client, project["id"])

        resp = client.get(f"/api/v1/ingestion/trace-validation/{project['id']}/project")
        assert resp.status_code == 200
        data = resp.json()
        assert data["traces_checked"] >= 1
        assert data["valid_traces"] >= 1
