"""ARGUS OTLP/Protobuf decoding (hardening — interop with stock collectors).

Why this exists: a vanilla OpenTelemetry Collector's ``otlphttp`` exporter
defaults to **Protobuf**, not JSON. Before this module, the documented
"point your collector here" path required every user to discover and set
``encoding: json``, and a collector left on its default received a ``422`` that
named a JSON field it had never sent. The workaround was documented honestly, but
a reliability platform that cannot accept the protocol's default encoding is not
interoperable — it is JSON-only with an asterisk.

How the bytes become rows: there is exactly one ingestion path. This module does
**not** grow a second one. It decodes an OTLP/Protobuf export into the *same
dictionary shape* the JSON transport already produces (``MessageToDict`` with
camelCase keys, which is what protojson emits and what
:class:`~app.services.otlp_adapter.OTLPAdapter` already reads), and the caller —
:class:`~app.core.edge.OtlpProtobufMiddleware` — hands that to the untouched
route. One adapter, one pipeline, one set of scope checks.

Decoding uses the official ``opentelemetry-proto`` descriptors rather than a
hand-rolled wire reader: the OTLP schema is large and versioned, and a
subtly-wrong hand parser would silently drop spans.

Errors are typed so the HTTP layer can answer precisely — a malformed body is a
client error (``400``), never a stack trace, and never a silent empty batch.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = [
    "OtlpDecodeError",
    "SUPPORTED_SIGNALS",
    "decode_otlp_protobuf",
    "is_protobuf_content_type",
]


class OtlpDecodeError(ValueError):
    """The body was not a decodable OTLP/Protobuf export request.

    Carries a message written for the *sender*: it names what was expected, so a
    collector's operator can act on it without reading ARGUS source.
    """


#: Route suffix → the protobuf message that path accepts, plus the JSON key the
#: adapter reads. Keyed by the *signal*, not the full path, so a future v2 path
#: needs no new entry.
SUPPORTED_SIGNALS: dict[str, tuple[str, str]] = {
    "traces": (
        "opentelemetry.proto.collector.trace.v1.trace_service_pb2:ExportTraceServiceRequest",
        "resourceSpans",
    ),
    "logs": (
        "opentelemetry.proto.collector.logs.v1.logs_service_pb2:ExportLogsServiceRequest",
        "resourceLogs",
    ),
    "metrics": (
        "opentelemetry.proto.collector.metrics.v1.metrics_service_pb2:ExportMetricsServiceRequest",
        "resourceMetrics",
    ),
}


def is_protobuf_content_type(content_type: str | None) -> bool:
    """True for the content types a real OTLP exporter sends.

    ``application/x-protobuf`` is what the Collector and every OpenTelemetry SDK
    use; ``application/protobuf`` and ``application/vnd.google.protobuf`` appear
    from other senders. Anything with the JSON media type (or no content type at
    all, which is how the existing tests and curl-based probes send) is left to
    the JSON route untouched.
    """
    if not content_type:
        return False
    value = content_type.split(";", 1)[0].strip().lower()
    return value in {
        "application/x-protobuf",
        "application/protobuf",
        "application/vnd.google.protobuf",
        "application/octet-stream+protobuf",
    }


def _resolve(dotted: str) -> Any:
    """Import ``module:attribute`` lazily.

    Lazy on purpose: the import pulls in the protobuf runtime, and a deployment
    that never receives a protobuf body should not pay for it (nor fail on a
    missing optional import at boot). The dependency is pinned in requirements;
    this keeps the failure at request time and turns it into a clear 501-ish
    message rather than an import crash at startup.
    """
    module_path, _, attribute = dotted.partition(":")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, attribute)


def decode_otlp_protobuf(path: str, body: bytes) -> dict[str, Any]:
    """Decode a binary OTLP export into the adapter's JSON dictionary shape.

    ``path`` is the request path; the trailing segment selects the message type
    (``/v1/traces`` → ``ExportTraceServiceRequest``).

    Raises :class:`OtlpDecodeError` with a sender-readable reason when the path is
    not an OTLP export, the protobuf runtime is unavailable, or the bytes do not
    parse. An empty body is a valid, empty export — OTLP defines it that way, and
    collectors do send it — so it decodes to an empty request rather than an
    error.
    """
    signal = path.rstrip("/").rsplit("/", 1)[-1]
    entry = SUPPORTED_SIGNALS.get(signal)
    if entry is None:
        raise OtlpDecodeError(
            f"{path} is not an OTLP export path; expected one of "
            + ", ".join(f"/v1/{name}" for name in SUPPORTED_SIGNALS)
        )
    dotted, json_key = entry

    try:
        message_class = _resolve(dotted)
    except Exception as exc:  # pragma: no cover - depends on deployment
        raise OtlpDecodeError(
            "this deployment cannot decode OTLP/Protobuf: the "
            f"'opentelemetry-proto' package is not installed ({exc}). Send "
            "Content-Type: application/json instead, or install the pinned "
            "dependency."
        ) from exc

    message = message_class()
    try:
        # ``ParseFromString`` raises DecodeError on malformed bytes.
        message.ParseFromString(body)
    except Exception as exc:
        raise OtlpDecodeError(
            f"the request body is not a valid {message_class.__name__}: {exc}"
        ) from exc

    #: ``google.protobuf`` ships no type information for this module; the import
    #: is inside the decoder because the dependency is optional (a deployment can
    #: run JSON-only without ``opentelemetry-proto`` installed).
    from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

    # camelCase (protojson's default), preserving_proto_field_name=False, is
    # exactly what the JSON transport receives, so both encodings converge before
    # the adapter sees them.
    decoded: dict[str, Any] = MessageToDict(
        message,
        preserving_proto_field_name=False,
        always_print_fields_with_no_presence=False,
    )
    if json_key not in decoded:
        # An empty export carries no repeated field at all; the routes default it
        # to an empty list, so materialise it here to keep one shape.
        decoded[json_key] = []
    return decoded


def describe_expected_body(path: str) -> str:
    """A one-line description of what the path accepts, for error messages."""
    signal = path.rstrip("/").rsplit("/", 1)[-1]
    entry = SUPPORTED_SIGNALS.get(signal)
    if entry is None:
        return "an OTLP export path (/v1/traces, /v1/logs or /v1/metrics)"
    return f"{entry[0].split(':')[-1]} as OTLP/Protobuf or OTLP/JSON"


#: Re-exported for type checkers that read the module's public surface.
DecodeFn = Callable[[str, bytes], dict[str, Any]]
