"""OTLP/Protobuf ingestion over real HTTP (hardening — stock-collector interop).

The property under test is interoperability, and the honest way to test
interoperability is to serialise with the protocol's own descriptors rather than
hand-crafting bytes that happen to match our parser. Every payload here is built
with ``opentelemetry-proto`` — the package a real exporter uses — sent as
``Content-Type: application/x-protobuf``, and must land as rows through the same
pipeline the JSON transport uses.

What these tests pin, beyond "it decodes":

* **Parity.** The same span sent as Protobuf and as JSON is accepted identically,
  because the middleware rewrites one into the other instead of growing a second
  ingestion path.
* **The JSON path is untouched.** An existing sender sees no behaviour change.
* **Limits still hold.** An oversized export is refused with ``413`` and is never
  buffered; malformed bytes are a clean ``400`` naming what was expected, never a
  stack trace.
* **Scope is not bypassed.** Proved in ``test_auth_security.py``, where real
  credentials exist: a Protobuf write naming a project the credential does not
  hold is refused, exactly like the JSON write.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.services.otlp_protobuf import (
    OtlpDecodeError,
    decode_otlp_protobuf,
    is_protobuf_content_type,
)

PROTOBUF = "application/x-protobuf"


def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_project(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "OTLP Proto", "slug": _unique_slug("otlp-proto")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _traces_payload(
    *, span_name: str = "GET /checkout", service: str = "checkout"
) -> bytes:
    """A realistic ExportTraceServiceRequest, serialised by the OTLP runtime."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as pb

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    request = pb.ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    attribute = resource_spans.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = service
    span = resource_spans.scope_spans.add().spans.add()
    span.name = span_name
    span.trace_id = uuid.uuid4().bytes
    span.span_id = uuid.uuid4().bytes[:8]
    span.start_time_unix_nano = now_ns
    span.end_time_unix_nano = now_ns + 5_000_000
    span.status.code = 2  # ERROR
    return request.SerializeToString()


def _logs_payload(*, body: str = "payment declined", severity: int = 17) -> bytes:
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2 as pb

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    request = pb.ExportLogsServiceRequest()
    resource_logs = request.resource_logs.add()
    attribute = resource_logs.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "checkout"
    record = resource_logs.scope_logs.add().log_records.add()
    record.time_unix_nano = now_ns
    record.severity_number = severity
    record.severity_text = "ERROR"
    record.body.string_value = body
    return request.SerializeToString()


def _metrics_payload(
    *, name: str = "http.checkout.latency.p95", value: float = 812.5
) -> bytes:
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2 as pb

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    request = pb.ExportMetricsServiceRequest()
    resource_metrics = request.resource_metrics.add()
    attribute = resource_metrics.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "checkout"
    metric = resource_metrics.scope_metrics.add().metrics.add()
    metric.name = name
    gauge = metric.gauge.data_points.add()
    gauge.time_unix_nano = now_ns
    gauge.as_double = value
    return request.SerializeToString()


def _json_traces_body(project_id: str) -> dict:
    """The same information in the JSON transport, for the parity assertion."""
    return {
        "project_id": project_id,
        "resource_spans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"string_value": "checkout"}}
                    ]
                },
                "spans": [
                    {
                        "trace_id": "1" * 32,
                        "span_id": "2" * 16,
                        "name": "GET /checkout",
                        "kind": 2,
                        "start_time_unix_nano": 1704110400000000000,
                        "end_time_unix_nano": 1704110400050000000,
                        "attributes": [],
                        "status": {"code": 2, "message": ""},
                        "events": [],
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


class TestDecoding:
    def test_a_real_span_decodes_to_the_protojson_shape(self) -> None:
        decoded = decode_otlp_protobuf("/api/v1/otlp/v1/traces", _traces_payload())

        # The camelCase key is what the JSON transport produces, and therefore
        # what OTLPAdapter already reads — the whole reason the transform lives
        # at the edge rather than in a second adapter.
        assert "resourceSpans" in decoded
        span = decoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "GET /checkout"
        assert span["status"]["code"] == "STATUS_CODE_ERROR"

    def test_logs_and_metrics_decode_too(self) -> None:
        logs = decode_otlp_protobuf("/api/v1/otlp/v1/logs", _logs_payload())
        body = logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"]
        assert body["stringValue"] == "payment declined"

        metrics = decode_otlp_protobuf("/api/v1/otlp/v1/metrics", _metrics_payload())
        point = metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        assert point["name"] == "http.checkout.latency.p95"
        assert point["gauge"]["dataPoints"][0]["asDouble"] == 812.5

    def test_an_empty_export_is_empty_not_an_error(self) -> None:
        """OTLP defines an empty export as valid; collectors do send one."""
        assert decode_otlp_protobuf("/api/v1/otlp/v1/traces", b"") == {
            "resourceSpans": []
        }

    def test_malformed_bytes_are_a_typed_client_error(self) -> None:
        with pytest.raises(OtlpDecodeError) as excinfo:
            decode_otlp_protobuf("/api/v1/otlp/v1/traces", b"\xff\xff\xff\xff")
        assert "ExportTraceServiceRequest" in str(excinfo.value)

    def test_a_non_export_path_is_refused_with_the_expected_list(self) -> None:
        with pytest.raises(OtlpDecodeError) as excinfo:
            decode_otlp_protobuf("/api/v1/otlp/v1/profiles", b"")
        assert "/v1/traces" in str(excinfo.value)

    def test_content_type_detection_covers_real_senders(self) -> None:
        assert is_protobuf_content_type("application/x-protobuf")
        assert is_protobuf_content_type("application/x-protobuf; charset=binary")
        assert is_protobuf_content_type("application/protobuf")
        assert not is_protobuf_content_type("application/json")
        assert not is_protobuf_content_type(None)


# ---------------------------------------------------------------------------
# Over HTTP, through the real app
# ---------------------------------------------------------------------------


class TestOverHttp:
    def test_protobuf_traces_are_ingested(self, client: TestClient) -> None:
        project_id = _create_project(client)

        response = client.post(
            "/api/v1/otlp/v1/traces",
            content=_traces_payload(),
            headers={"Content-Type": PROTOBUF, "X-Argus-Project-Id": project_id},
        )

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1, response.text

    def test_protobuf_logs_and_metrics_are_ingested(self, client: TestClient) -> None:
        project_id = _create_project(client)
        headers = {"Content-Type": PROTOBUF, "X-Argus-Project-Id": project_id}

        logs = client.post(
            "/api/v1/otlp/v1/logs", content=_logs_payload(), headers=headers
        )
        assert logs.status_code == 200, logs.text
        assert logs.json()["accepted"] == 1, logs.text

        metrics = client.post(
            "/api/v1/otlp/v1/metrics", content=_metrics_payload(), headers=headers
        )
        assert metrics.status_code == 200, metrics.text
        assert metrics.json()["accepted"] == 1, metrics.text

    def test_a_protobuf_export_can_name_its_project_in_the_query(self, client) -> None:
        """OTLP/Protobuf has no field for ARGUS's tenancy extension.

        An exporter therefore cannot put ``projectId`` in the payload at all, so
        the destination has to be nameable out of band — here in the query
        string. The scope check still applies to whatever is named.
        """
        project_id = _create_project(client)

        response = client.post(
            f"/api/v1/otlp/v1/traces?project_id={project_id}",
            content=_traces_payload(),
            headers={"Content-Type": PROTOBUF},
        )

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1, response.text

    def test_the_json_transport_still_works_unchanged(self, client: TestClient) -> None:
        project_id = _create_project(client)

        response = client.post(
            "/api/v1/otlp/v1/traces", json=_json_traces_body(project_id)
        )

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1, response.text

    def test_protobuf_and_json_reach_the_same_behaviour(
        self, client: TestClient
    ) -> None:
        """Parity: the accepted count is what a caller sees either way."""
        project_id = _create_project(client)

        as_json = client.post(
            "/api/v1/otlp/v1/traces", json=_json_traces_body(project_id)
        )
        as_protobuf = client.post(
            "/api/v1/otlp/v1/traces",
            content=_traces_payload(span_name="GET /parity"),
            headers={"Content-Type": PROTOBUF, "X-Argus-Project-Id": project_id},
        )

        assert as_json.status_code == as_protobuf.status_code == 200
        assert as_json.json()["accepted"] == as_protobuf.json()["accepted"] == 1

    def test_spec_correct_nesting_is_ingested_for_every_signal(
        self, client: TestClient
    ) -> None:
        """The regression the Protobuf work surfaced.

        OTLP nests spans/metrics/logs under a *scope* level. Traces and logs
        already descended into it; metrics did not, so a spec-correct metric
        export — every metric a real collector sends, in either transport — was
        silently dropped and reported as success. Protobuf payloads are generated
        from the protocol's own descriptors, so they are always spec-correct, and
        this is where the gap showed up.
        """
        project_id = _create_project(client)
        nested_metrics = {
            "project_id": project_id,
            "resource_metrics": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"string_value": "checkout"},
                            }
                        ]
                    },
                    "scope_metrics": [
                        {
                            "scope": {"name": "argus.test"},
                            "metrics": [
                                {
                                    "name": "http.checkout.latency.p95",
                                    "unit": "ms",
                                    "gauge": {
                                        "data_points": [
                                            {
                                                "time_unix_nano": 1704110400000000000,
                                                "as_double": 812.5,
                                                "attributes": [],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        as_json = client.post("/api/v1/otlp/v1/metrics", json=nested_metrics)
        assert as_json.status_code == 200, as_json.text
        assert as_json.json()["accepted"] == 1, (
            "a spec-correct (scope-nested) OTLP metrics payload was dropped: "
            + as_json.text
        )

        as_protobuf = client.post(
            "/api/v1/otlp/v1/metrics",
            content=_metrics_payload(),
            headers={"Content-Type": PROTOBUF, "X-Argus-Project-Id": project_id},
        )
        assert as_protobuf.status_code == 200, as_protobuf.text
        assert as_protobuf.json()["accepted"] == 1, as_protobuf.text

    def test_metrics_are_still_accepted_in_the_flat_legacy_shape(
        self, client: TestClient
    ) -> None:
        """The descent must be additive: older collectors keep working."""
        project_id = _create_project(client)

        response = client.post(
            "/api/v1/otlp/v1/metrics",
            json={
                "project_id": project_id,
                "resource_metrics": [
                    {
                        "resource": {"attributes": []},
                        "metrics": [
                            {
                                "name": "cpu_usage",
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
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1, response.text

    def test_malformed_protobuf_is_a_clean_400(self, client: TestClient) -> None:
        project_id = _create_project(client)

        response = client.post(
            "/api/v1/otlp/v1/traces",
            content=b"\xff\xff\xff\xff definitely not protobuf",
            headers={"Content-Type": PROTOBUF, "X-Argus-Project-Id": project_id},
        )

        assert response.status_code == 400, response.text
        body = response.json()
        # Readable, actionable, and no leaked internals.
        assert body["error_code"] == "OTLP_PROTOBUF_DECODE_FAILED"
        assert "ExportTraceServiceRequest" in body["expected"]
        assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# The middleware's own limits
# ---------------------------------------------------------------------------


def _middleware_app(max_bytes: int) -> FastAPI:
    """A minimal app: the middleware under test, then a JSON echo endpoint.

    Built small on purpose — the size limit is a property of the middleware, and
    this exercises it without a 10 MB body or a database.
    """
    from app.core.edge import OtlpProtobufMiddleware

    app = FastAPI()
    app.add_middleware(OtlpProtobufMiddleware, max_bytes=max_bytes)

    @app.post("/api/v1/otlp/v1/traces")
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse(await request.json())

    return app


class TestMiddlewareLimits:
    def test_an_oversized_declared_body_is_refused_with_413(self) -> None:
        app = _middleware_app(max_bytes=64)

        response = TestClient(app).post(
            "/api/v1/otlp/v1/traces",
            content=_traces_payload(),
            headers={"Content-Type": PROTOBUF},
        )

        assert response.status_code == 413, response.text
        assert response.json()["error_code"] == "REQUEST_TOO_LARGE"

    def test_the_decoded_body_is_handed_on_as_json(self) -> None:
        app = _middleware_app(max_bytes=10_000_000)

        response = TestClient(app).post(
            "/api/v1/otlp/v1/traces",
            content=_traces_payload(span_name="handed-on"),
            headers={"Content-Type": PROTOBUF},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == (
            "handed-on"
        )

    def test_a_json_body_passes_through_the_middleware_untouched(self) -> None:
        app = _middleware_app(max_bytes=10_000_000)

        response = TestClient(app).post(
            "/api/v1/otlp/v1/traces",
            content=json.dumps({"resourceSpans": [{"marker": "json"}]}),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"resourceSpans": [{"marker": "json"}]}

    def test_content_length_is_never_lost_on_the_rewrite(self) -> None:
        """The rewritten request must carry the *new* length, not the old one."""
        from app.core.edge import OtlpProtobufMiddleware

        seen: dict[str, str] = {}

        async def inner(scope, receive, send):
            seen.update(
                {key.decode(): value.decode() for key, value in scope["headers"]}
            )
            message = await receive()
            seen["body_bytes"] = str(len(message["body"]))
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = OtlpProtobufMiddleware(inner, max_bytes=10_000_000)
        payload = _traces_payload()

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        import asyncio

        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "path": "/api/v1/otlp/v1/traces",
                    "headers": [
                        (b"content-type", PROTOBUF.encode()),
                        (b"content-length", str(len(payload)).encode()),
                    ],
                    "query_string": b"",
                },
                receive,
                send,
            )
        )

        assert seen["content-type"] == "application/json"
        assert int(seen["content-length"]) == int(seen["body_bytes"])
