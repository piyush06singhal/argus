"""ARGUS OTLP (OpenTelemetry) Ingestion Adapter.

Converts OpenTelemetry protocol (OTLP/JSON) spans, logs, and metrics into
the ARGUS internal ``RawObservabilityEvent`` format so they can flow through
the existing ingestion pipeline.

Only the JSON transport is supported (not gRPC/protobuf).  The adapter is
stateless and deterministic — all side-effects happen in the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.sources import RawObservabilityEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public schemas for the OTLP JSON payload
# ---------------------------------------------------------------------------


@dataclass
class OTLPSpan:
    """A single span inside an OTLP trace batch."""

    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    kind: str  # INTERNAL, CLIENT, SERVER, PRODUCER, CONSUMER
    start_time_unix_nano: int
    end_time_unix_nano: int
    attributes: Dict[str, Any]
    status_code: str  # OK, ERROR, UNSET
    status_message: str
    events: List[Dict[str, Any]]
    resource: Dict[str, Any]


@dataclass
class OTLPLogRecord:
    """A single log record inside an OTLP log batch."""

    time_unix_nano: int
    severity_number: int  # 1-24 per OTLP spec
    severity_text: str
    body: str
    attributes: Dict[str, Any]
    resource: Dict[str, Any]
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


@dataclass
class OTLPMetric:
    """A single metric inside an OTLP metrics batch."""

    name: str
    description: str
    unit: str
    data_points: List[Dict[str, Any]]
    metric_type: str  # Gauge, Sum, Histogram, Summary
    resource: Dict[str, Any]


@dataclass
class OTLPBatch:
    """Parsed OTLP JSON request — one of spans, logs, or metrics."""

    resource_spans: List[OTLPSpan] = field(default_factory=list)
    resource_logs: List[OTLPLogRecord] = field(default_factory=list)
    resource_metrics: List[OTLPMetric] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

_SEVERITY_MAP: Dict[int, str] = {
    1: "TRACE",
    2: "TRACE",
    3: "TRACE",
    4: "TRACE",
    5: "DEBUG",
    6: "DEBUG",
    7: "DEBUG",
    8: "DEBUG",
    9: "INFO",
    10: "INFO",
    11: "INFO",
    12: "INFO",
    13: "INFO",
    14: "WARN",
    15: "WARN",
    16: "WARN",
    17: "ERROR",
    18: "ERROR",
    19: "ERROR",
    20: "FATAL",
    21: "FATAL",
    22: "FATAL",
    23: "FATAL",
    24: "FATAL",
}

_SEV_TO_ARGUS: Dict[str, str] = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARN": "WARN",
    "ERROR": "ERROR",
    "FATAL": "FATAL",
}


def _ns_to_datetime(ns: Any) -> datetime:
    """Convert nanosecond-epoch timestamp to UTC datetime.

    OTLP protojson encodes ``timeUnixNano`` as a base-10 *string*, not a JSON
    number, so the input may arrive as ``str`` or ``int``. Coerce defensively.
    """
    try:
        ns_int = int(ns)
    except (TypeError, ValueError):
        logger.warning("Invalid OTLP nanosecond timestamp %r, using epoch", ns)
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(ns_int / 1e9, tz=timezone.utc)


def _pick(d: Dict[str, Any], *names: str) -> Any:
    """Return the first present (non-None) value among alternate key names.

    OTLP protojson encodes fields in lowerCamelCase (``traceId``, ``asDouble``)
    while the older snake_case form (``trace_id``, ``as_double``) is also
    accepted by some collectors. We accept both.
    """
    for name in names:
        if name in d and d.get(name) is not None:
            return d[name]
    return None


def _any_value(obj: Any) -> Any:
    """Pull a value from an OTLP ``AnyValue`` (camelCase or snake_case)."""
    if not isinstance(obj, dict):
        return obj
    for scalar in (
        "string_value",
        "stringValue",
        "int_value",
        "intValue",
        "double_value",
        "doubleValue",
        "bool_value",
        "boolValue",
    ):
        if scalar in obj:
            return obj[scalar]
    for collection in ("array_value", "arrayValue", "kvlist_value", "kvlistValue"):
        if collection in obj:
            return obj[collection]
    return None


def _resource_to_dict(resource: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten OTLP ``resource`` ``attributes`` (list of key-value pairs)
    into a plain dict."""
    attrs = resource.get("attributes", []) or []
    out: Dict[str, Any] = {}
    for attr in attrs:
        key = attr.get("key", "")
        value = _any_value(attr.get("value"))
        if key and value is not None:
            out[key] = value
    return out


def _attributes_to_dict(attrs: Any) -> Dict[str, Any]:
    """Convert OTLP attributes (list of key-value or dict) to flat dict."""
    if isinstance(attrs, dict):
        return dict(attrs)
    if isinstance(attrs, list):
        result: Dict[str, Any] = {}
        for attr in attrs:
            key = attr.get("key", "")
            value = _any_value(attr.get("value"))
            if key and value is not None:
                result[key] = value
        return result
    return {}


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


class OTLPAdapter:
    """Convert OTLP/JSON payloads to ``RawObservabilityEvent`` instances.

    Usage::

        adapter = OTLPAdapter()
        raw_events = adapter.convert_spans(otlp_json, source_name="api-gw")
    """

    # ------------------------------------------------------------------
    # Spans → events
    # ------------------------------------------------------------------
    def convert_spans(
        self,
        payload: Dict[str, Any],
        *,
        source_name: str = "otlp",
    ) -> List[RawObservabilityEvent]:
        """Convert OTLP ``ExportTraceServiceRequest`` JSON to raw events."""
        events: List[RawObservabilityEvent] = []
        resource_spans = payload.get("resource_spans", [])

        for rs in resource_spans:
            resource = _resource_to_dict(rs.get("resource", {}))
            service_name = resource.get("service.name", source_name)

            # OTLP protojson nests spans under scope_spans[].spans (or
            # scopeSpans); the flat rs["spans"] form is also accepted for
            # older collectors and the original ARGUS tests.
            spans = list(_pick(rs, "spans") or [])
            if not spans:
                for scope in rs.get("scope_spans", rs.get("scopeSpans", [])) or []:
                    spans.extend(_pick(scope, "spans") or [])

            for span_data in spans:
                span = self._parse_span(span_data, resource)
                raw = RawObservabilityEvent(
                    source_type="trace",
                    source_name=service_name,
                    timestamp=_ns_to_datetime(span.start_time_unix_nano),
                    event_type="TRACE",
                    payload={
                        "trace_id": span.trace_id,
                        "span_id": span.span_id,
                        "parent_span_id": span.parent_span_id,
                        "name": span.name,
                        "kind": span.kind,
                        "start_time": span.start_time_unix_nano,
                        "end_time": span.end_time_unix_nano,
                        "duration_ms": (
                            span.end_time_unix_nano - span.start_time_unix_nano
                        )
                        / 1e6,
                        "status_code": span.status_code,
                        "status_message": span.status_message,
                        "attributes": span.attributes,
                        "events": span.events,
                    },
                    metadata={"source": "otlp", "resource": resource},
                )
                events.append(raw)

        return events

    # ------------------------------------------------------------------
    # Logs → events
    # ------------------------------------------------------------------
    def convert_logs(
        self,
        payload: Dict[str, Any],
        *,
        source_name: str = "otlp",
    ) -> List[RawObservabilityEvent]:
        """Convert OTLP ``ExportLogsServiceRequest`` JSON to raw events."""
        events: List[RawObservabilityEvent] = []
        resource_logs = payload.get("resource_logs", [])

        for rl in resource_logs:
            resource = _resource_to_dict(rl.get("resource", {}))
            service_name = resource.get("service.name", source_name)

            # OTLP protojson nests records under scope_logs[].log_records;
            # flat log_records is also accepted for older collectors.
            records = list(_pick(rl, "log_records", "logRecords") or [])
            if not records:
                for scope in rl.get("scope_logs", rl.get("scopeLogs", [])) or []:
                    records.extend(_pick(scope, "log_records", "logRecords") or [])

            for lr_data in records:
                lr = self._parse_log_record(lr_data, resource)
                severity_raw = _SEVERITY_MAP.get(lr.severity_number, "UNKNOWN")
                severity = _SEV_TO_ARGUS.get(severity_raw, "INFO")

                raw = RawObservabilityEvent(
                    source_type="log",
                    source_name=service_name,
                    timestamp=_ns_to_datetime(lr.time_unix_nano),
                    event_type="LOG",
                    payload={
                        "severity": severity,
                        "message": lr.body,
                        "attributes": lr.attributes,
                        "trace_id": lr.trace_id,
                        "span_id": lr.span_id,
                    },
                    metadata={"source": "otlp", "resource": resource},
                )
                events.append(raw)

        return events

    # ------------------------------------------------------------------
    # Metrics → events
    # ------------------------------------------------------------------
    def convert_metrics(
        self,
        payload: Dict[str, Any],
        *,
        source_name: str = "otlp",
    ) -> List[RawObservabilityEvent]:
        """Convert OTLP ``ExportMetricsServiceRequest`` JSON to raw events."""
        events: List[RawObservabilityEvent] = []
        resource_metrics = payload.get("resource_metrics", [])

        for rm in resource_metrics:
            resource = _resource_to_dict(rm.get("resource", {}))
            service_name = resource.get("service.name", source_name)

            for metric_data in rm.get("metrics", []):
                metric = self._parse_metric(metric_data, resource)

                for dp in (
                    metric.data_points if isinstance(metric.data_points, list) else []
                ):
                    ts_ns = (
                        _pick(
                            dp,
                            "start_time_unix_nano",
                            "startTimeUnixNano",
                            "time_unix_nano",
                            "timeUnixNano",
                        )
                        or 0
                    )
                    value = (
                        _pick(
                            dp,
                            "as_double",
                            "asDouble",
                            "as_int",
                            "asInt",
                            "as_gauge",
                            "asGauge",
                        )
                        or 0
                    )
                    attr_pairs = _pick(dp, "attributes", "attributes") or {}
                    # Histogram and Summary have complex shapes — extract count/sum
                    if metric.metric_type in ("Histogram", "Summary"):
                        value = _pick(dp, "sum", "count") or 0

                    raw = RawObservabilityEvent(
                        source_type="metric",
                        source_name=service_name,
                        timestamp=_ns_to_datetime(ts_ns)
                        if ts_ns
                        else datetime.now(tz=timezone.utc),
                        event_type="METRIC",
                        payload={
                            "metric_name": metric.name,
                            "metric_type": metric.metric_type.upper(),
                            "value": value,
                            "unit": metric.unit,
                            "description": metric.description,
                            "attributes": _attributes_to_dict(attr_pairs),
                            "labels": _attributes_to_dict(attr_pairs),
                        },
                        metadata={"source": "otlp", "resource": resource},
                    )
                    events.append(raw)

        return events

    # ------------------------------------------------------------------
    # Private parsers
    # ------------------------------------------------------------------
    def _parse_span(
        self, span_data: Dict[str, Any], resource: Dict[str, Any]
    ) -> OTLPSpan:
        status = span_data.get("status", {}) or {}
        kind_map = {
            0: "UNSPECIFIED",
            1: "INTERNAL",
            2: "SERVER",
            3: "CLIENT",
            4: "PRODUCER",
            5: "CONSUMER",
        }
        kind_num = span_data.get("kind", 0)

        # protojson encodes timestamps as base-10 strings; coerce to int so
        # downstream arithmetic (duration) stays numeric.
        start_ns = _pick(span_data, "start_time_unix_nano", "startTimeUnixNano") or 0
        end_ns = _pick(span_data, "end_time_unix_nano", "endTimeUnixNano") or 0
        try:
            start_ns = int(start_ns)
            end_ns = int(end_ns)
        except (TypeError, ValueError):
            start_ns = 0
            end_ns = 0

        return OTLPSpan(
            trace_id=_pick(span_data, "trace_id", "traceId") or "",
            span_id=_pick(span_data, "span_id", "spanId") or "",
            parent_span_id=_pick(span_data, "parent_span_id", "parentSpanId") or None,
            name=span_data.get("name", ""),
            kind=kind_map.get(kind_num, "INTERNAL"),
            start_time_unix_nano=start_ns,
            end_time_unix_nano=end_ns,
            attributes=_attributes_to_dict(span_data.get("attributes", [])),
            status_code={
                0: "UNSET",
                1: "OK",
                2: "ERROR",
            }.get(status.get("code", 0), "UNSET"),
            status_message=status.get("message", ""),
            events=span_data.get("events", []),
            resource=resource,
        )

    def _parse_log_record(
        self, lr_data: Dict[str, Any], resource: Dict[str, Any]
    ) -> OTLPLogRecord:
        # Body can be a raw string or an AnyValue ({stringValue: ...}).
        body_value = _any_value(_pick(lr_data, "body"))
        if isinstance(body_value, (str, int, float, bool)):
            body = str(body_value)
        else:
            body = str(body_value or "")

        # Extract trace_id / span_id — top-level fields or attributes.
        attrs = _attributes_to_dict(lr_data.get("attributes", []))
        trace_id = (
            _pick(lr_data, "trace_id", "traceId")
            or attrs.get("trace_id")
            or attrs.get("traceId")
        )
        span_id = (
            _pick(lr_data, "span_id", "spanId")
            or attrs.get("span_id")
            or attrs.get("spanId")
        )

        return OTLPLogRecord(
            time_unix_nano=_pick(lr_data, "time_unix_nano", "timeUnixNano") or 0,
            severity_number=_pick(lr_data, "severity_number", "severityNumber")
            or 9,  # default INFO
            severity_text=_pick(lr_data, "severity_text", "severityText") or "INFO",
            body=body,
            attributes=attrs,
            resource=resource,
            trace_id=trace_id,
            span_id=span_id,
        )

    def _parse_metric(
        self, metric_data: Dict[str, Any], resource: Dict[str, Any]
    ) -> OTLPMetric:
        data_points: List[Dict[str, Any]] = []
        metric_type = "Gauge"

        if "gauge" in metric_data:
            metric_type = "Gauge"
            data_points = _pick(metric_data["gauge"], "data_points", "dataPoints") or []
        elif "sum" in metric_data:
            metric_type = "Sum"
            data_points = _pick(metric_data["sum"], "data_points", "dataPoints") or []
        elif "histogram" in metric_data:
            metric_type = "Histogram"
            data_points = (
                _pick(metric_data["histogram"], "data_points", "dataPoints") or []
            )
        elif "summary" in metric_data:
            metric_type = "Summary"
            data_points = (
                _pick(metric_data["summary"], "data_points", "dataPoints") or []
            )

        return OTLPMetric(
            name=metric_data.get("name", ""),
            description=metric_data.get("description", ""),
            unit=metric_data.get("unit", ""),
            data_points=data_points,
            metric_type=metric_type,
            resource=resource,
        )
