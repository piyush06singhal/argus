# ARGUS Telemetry Ingestion

How ARGUS ingests OpenTelemetry (OTLP) telemetry, what wire formats it accepts, and the Prometheus-compatible `/metrics` export surface.

## 1. OTLP: OpenTelemetry Protocol (JSON transport)

ARGUS accepts OTLP/JSON (`ExportTraceServiceRequest`, `ExportLogsServiceRequest`, `ExportMetricsServiceRequest`) at:

```text
POST /api/v1/otlp/v1/traces
POST /api/v1/otlp/v1/logs
POST /api/v1/otlp/v1/metrics
```

Only the JSON transport is supported (not gRPC/protobuf). The adapter (`app/services/otlp_adapter.py`) is stateless and deterministic; all side-effects happen in the shared ingestion pipeline.

### 1.1 protojson wire format — camelCase **and** snake_case accepted

OTLP protojson emits lowerCamelCase structural keys — `resourceSpans`, `resourceLogs`, `resourceMetrics`, `scopeSpans` / `scopeLogs`. The original ARGUS-native form used snake_case (`resource_spans`, `scope_spans`). ARGUS accepts **both** at every level:

| Level | camelCase (protojson) | snake_case (legacy) |
|-------|------------------------|----------------------|
| Envelope | `resourceSpans` | `resource_spans` |
| Scope container | `scopeSpans[].spans` | `scope_spans[].spans` |
| Span fields | `traceId`, `spanId`, `parentSpanId`, `startTimeUnixNano`, `endTimeUnixNano`, `status.code`, `status.message` | `trace_id`, `span_id`, `parent_span_id`, `start_time_unix_nano`, … |
| Log fields | `logRecords`, `timeUnixNano`, `severityNumber`, `severityText`, `body.stringValue` | `log_records`, `time_unix_nano`, `severity_number`, … |
| Metric fields | `dataPoints`, `asDouble`, `asInt`, `timeUnixNano` | `data_points`, `as_double`, `as_int`, … |
| Attribute key | `service.name` | `service.name` (unchanged) |

The route-level schemas use a Pydantic alias generator (`to_camel`, `populate_by_name`) so a real collector's camelCase top-level keys are accepted while snake_case still works. The adapter's `_pick` helper resolves both spellings for nested fields — e.g. both `scope_spans` and `scopeSpans` nesting are traversed, and logs accept `log_records` / `logRecords` at both the resource and scope level.

### 1.2 String-encoded nanos coerced

protojson encodes nanosecond timestamps (`timeUnixNano`, `startTimeUnixNano`, `endTimeUnixNano`) as base-10 **strings**, not JSON numbers. The adapter coerces defensively (`_ns_to_datetime`):

```text
"startTimeUnixNano": "1726100000123000000" → datetime (UTC)
```

A `start_time_unix_nano` of `"0"` falls back to epoch; genuinely unparseable values are warned about and fall back to epoch rather than aborting the batch. Downstream duration arithmetic stays numeric because the adapter converts nanos to `int` before computing `duration_ms`.

### 1.3 Body and value shapes

- **Span `status`**: `status.code` `0/1/2` → `UNSET/OK/ERROR`; `status.message` → `status_message`.
- **Span `kind`**: numeric OTLP kind `0–5` → `UNSPECIFIED/INTERNAL/SERVER/CLIENT/PRODUCER/CONSUMER`.
- **Log `severityNumber`**: 1–24 → mapped to ARGUS severity via the OTLP severity ladder, then folded into the ARGUS vocabulary (`TRACE/DEBUG → DEBUG`, 9–13 → `INFO`, 14–16 → `WARN`, 17–19 → `ERROR`, 20–24 → `FATAL`).
- **Log `body`**: a raw string, or an `AnyValue` object `{stringValue: "…"}` — both accepted; anything else stringifies.
- **Attributes**: OTLP `AnyValue` (`stringValue`/`intValue`/`doubleValue`/`boolValue`/`arrayValue`/`kvlistValue`) is unwrapped to a flat dict; `resource.attributes[].value` flattens into the event's `metadata.resource`.

The response is `{ "accepted": N, "duplicates": N, "failed": N }` — the same pipeline bookkeeping as every other ingest path.

### 1.4 Multi-tenancy

`project_id` is required on every OTLP request (a UUID); `environment_id` is optional. These are ARGUS extensions layered onto the OTLP envelope so telemetry lands scoped to the right project/environment immediately.

## 2. Structured log ingestion

Structured logs are ingested via `POST /observability/logs` (level, message, service, timestamp, project/environment/component scope, plus JSON `metadata`). Log records are additionally materialized into the `log_records` table for severity/time-series querying, alongside their canonical `observability_events` row.

## 3. Prometheus-compatible `/metrics`

`GET /metrics` (`app/api/v1/routes/metrics_export.py`) serves the Prometheus text exposition format (`version=0.0.4`). Content-type is the standard `text/plain; version=0.0.4; charset=utf-8` so a Prometheus scrape target accepts it. Pull-based and read-only; accepts an optional `?project_id=` filter.

Metrics emitted (all prefixed `argus_`):

| Metric | Type | Meaning |
|--------|------|---------|
| `argus_api_up` | gauge | API availability probe (1 when the endpoint answers) |
| `argus_sources_total` | gauge | Registered observability sources |
| `argus_sources_by_status{status}` | gauge | Sources by health status (`HEALTHY`/`DEGRADED`/`FAILING`/…) |
| `argus_source_events_total` | counter | Events received across all sources (registry `event_count`) |
| `argus_dead_letter_total` | gauge | Rows in the dead-letter store |
| `argus_events_24h_total` | counter | Canonical events ingested in the last 24 h |
| `argus_events_24h_by_type{event_type}` | counter | Events by type, last 24 h |
| `argus_logs_24h_by_severity{severity}` | counter | Log records by severity, last 24 h |
| `argus_traces_24h_total` | counter | Trace records created in the last 24 h |

`?project_id=` scopes every query except the API uptime probe.

## 4. OTLP → pipeline → canonical model

OTLP spans → `RawObservabilityEvent` (`source_type="trace"`, `event_type="TRACE"`) with `trace_id`/`span_id`/`parent_span_id`/`kind`/`duration_ms`/`status_code` in the payload. OTLP logs → `source_type="log"`, `event_type="LOG"`. OTLP metrics → `source_type="metric"`, `event_type="METRIC"`. From there they flow through the shared `IngestionPipeline` stages (Redaction → Resolution → Normalization → Correlation → Deduplication → per-event SAVEPOINT persist) exactly like any other source — see [docs/ingestion.md](ingestion.md).

## 5. The secret-rejection boundary (§46)

Credentials are **never ingested nor persisted**, at every boundary:

1. **Schema-level rejection**: Pydantic schemas reject request bodies whose `metadata` / `payload` / `configuration` contains known secret keys (`password`, `passwd`, `pwd`, `secret`, `api_key`/`apikey`, `access_token`, `auth_token`, `bearer`, `private_key`, `token`, substring-matched case-insensitively) with `422`. Verified by the end-to-end smoke against the live API for events, logs, metrics, webhooks, and source registration.
2. **Redaction (defense in depth)**: the pipeline re-scans every payload and rewrites sensitive values to `[REDACTED]` — including value-shape detection (JWTs, 40+ char base64, 32+ char hex) even under innocent key names. It returns a *new* dict and never mutates the caller's data.
3. **Worker re-checks**: `process_event_job` re-runs the same key check on async jobs (`/ingestion/webhook`, `/ingestion/queue`) so a job enqueued by any producer still cannot leak secrets into the domain model.
4. **Dead-letter redaction**: dead-lettered events store `payload_summary` (key names + type markers only, `[REDACTED]` for sensitive keys) — never the values.

Timestamps, payloads, and attributes carrying secret values are rejected or redacted before persistence; nothing derived from them is ever stored.
