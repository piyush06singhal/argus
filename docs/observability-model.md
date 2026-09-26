# ARGUS Observability Model

How ARGUS represents software behavior: the normalized event model, its entities, and its ingestion and correlation design.

## 1. Principle: a canonical, normalized model

Observability systems (logs, metrics, traces, system events) produce heterogeneous streams. ARGUS ingests them into a **canonical normalized model** so that reliability analysis has one consistent vocabulary.

The core flow:

```text
Source → Adapter → Validation → Redaction → Resolution → Normalization →
Correlation → Deduplication → Persistence (per-event SAVEPOINT isolation)
```

Implemented in:

- `app/core/sources.py` — `ObservabilitySource` / `RawObservabilityEvent` contracts
- `app/services/redaction.py` — `RedactionEngine`: recursive secret redaction before persistence
- `app/services/normalizer.py` — `ObservabilityNormalizer`: validate → map to canonical `ObservabilityEvent` → attach project/environment references → extract correlation IDs → persist
- `app/services/ingestion.py` — `IngestionPipeline`: source polling, single/batch ingestion (per-event savepoints), continuous mode, failure logging, dead-lettering
- `app/services/ingestion_helpers.py` — `ComponentResolver`, `EnvironmentResolver`, `CorrelationEngine`, `EventFingerprint`
- `app/services/queue.py` / `worker_runner.py` — Redis-backed async worker drain; see [docs/ingestion.md](ingestion.md)

## 2. Canonical observability events

Every telemetry signal is stored as one of five entity kinds, each sharing project/environment/component scope and structured `metadata`:

| Kind | Table | Canonical fields |
|------|-------|------------------|
| Event | `observability_events` | `timestamp`, `source`, `event_type` (`LOG`/`METRIC`/`TRACE`/`SYSTEM_EVENT`/…), `severity`, `payload`, `request_id`, `trace_id`, `span_id`, `deployment_id`, `incident_id` |
| Log | `log_records` | `timestamp`, `level`, `message`, `service`, `request_id`, `trace_id`, `span_id`, `raw_payload` |
| Metric | `metric_records` | `timestamp`, `metric_name`, `metric_type` (`COUNTER`/`GAUGE`/`HISTOGRAM`/…), `value`, `unit`, `labels` |
| Trace | `traces` | `trace_id` (W3C-style string), `name`, `start_time`, `end_time`, `duration_ms`, `status` |
| Span | `spans` | `trace_id`, `span_id`, `parent_span_id`, `operation`, `component_id`, `duration_ms`, `status` |

Structured documents (`payload`, `labels`, `raw_payload`, `metadata`) use **JSONB** on PostgreSQL.

## 3. Correlation

ARGUS uses the industry-standard correlation axes from day one:

- **Trace correlation** — `trace_id` / `span_id` on events, logs, and spans; a trace's spans form a tree via `parent_span_id`
- **Request correlation** — `request_id` on events and logs
- **Change correlation** — `deployment_id` on observability events links behavior to deployments
- **Impact correlation** — `incident_id` on observability events links active incidents to events

The normalizer extracts `request_id` / `trace_id` / `span_id` from event payloads automatically, so ingested data arrives pre-correlated.

## 4. Ingestion pipeline

`IngestionPipeline` provides:

- `ingest_one(raw)` — redact → resolve → normalize → correlate → dedup → persist a single event
- `ingest_batch(raw_events)` — normalize a batch with **per-event SAVEPOINT isolation** (`begin_nested`): a DB-level failure rolls back only that event and is dead-lettered, so one bad event can never poison the session for the rest of the batch; returns `accepted` / `duplicates` / `failed` counts
- `poll_and_ingest(lookback_minutes)` — pull new events from an `ObservabilitySource` within a lookback window
- `run_continuous(interval_seconds)` — poll loop (foundation of the background workers)

Deduplication is a first-class stage: a canonical `EventFingerprint` (project, source, event type, timestamp, redacted payload) makes re-ingested events count as duplicates instead of duplicating rows.

**Synchronous and asynchronous paths both run this pipeline.** The HTTP routes (`/observability/*`, `/otlp/v1/*`, `/ingestion/bulk`) run it in the request handler. The async path — `/ingestion/webhook` and `/ingestion/queue` (both 202) — enqueues a job to Redis and a background `IngestionWorker` runs the identical pipeline, with bounded retries, exponential backoff, and dead-lettering on exhaustion. See [docs/ingestion.md](ingestion.md).

Failures are handled safely: each normalization/ingestion error is logged (feeding ARGUS's self-observability), dead-lettered, and surfaced in `GET /ingestion/dead-letter`, rather than crashing the pipeline.

## 5. Failure semantics

| Input type | Behavior |
|------------|----------|
| Unknown source type | `NormalizationError` (`UNKNOWN_SOURCE_TYPE`) |
| Unknown event type enum | `NormalizationError` (`UNKNOWN_EVENT_TYPE`) |
| Malformed timestamp / value | rejected at API boundary with 422 |
| Unknown component/project on write | FK integrity enforcement / 404 where appropriate |
| Orphan span (trace not yet present) | accepted at ingest; isolated by trace cross-reference validation (§20) — `GET /ingestion/trace-validation/{trace_id}` reports `is_valid` / orphan spans; `GET /ingestion/orphan-spans/{project_id}` lists them |
| Batch event DB failure | rolls back just that event, dead-letters it, commits the rest (per-event SAVEPOINT) |
| Queue job failure | bounded retry with exponential backoff, then dead-letter (≥ MAX_RETRIES) |

None of these crash the API — every failure path is caught and represented as a validation error, a logged ingestion failure, or a dead-lettered job.

## 5a. Retention (Phase 1)

Retention policies layer on the timestamps and lifecycle metadata present from Phase 0:

- `GET /ingestion/retention/policy` — configured `retention_days` per entity kind
- `GET /ingestion/retention/preview` — dry run: which rows a sweep *would* remove
- `POST /ingestion/retention/sweep` — delete-by-timestamp sweep (`RetentionService`), returning a `RetentionSummary`

Sweeps never use unbounded queries — they are bounded by policy on the same indexed timestamp columns the list endpoints use.

## 5b. Where ingestion meets the graph (Phase 2)

Persisting spans/trace events enqueues a Redis `graph_extract` job. The worker
extracts typed relationships (CALLS, READS_FROM, …) from recent span trees and
trace-carrying events and runs graph reconciliation — the Software Knowledge
Graph stays current with observed runtime behavior without blocking ingestion.
Extraction is deterministic (parent/child span walks + service-name resolution
through the component registry); edges carry `source=TRACE`/`LOG` provenance
and never overwrite configured relationships. See
[software-knowledge-graph.md](software-knowledge-graph.md).

## 6. Querying

List endpoints support filters aligned to the normalization model:

- **Events**: `project_id`, `environment_id`, `event_type`, `severity`, `trace_id`, `start_time`, `end_time`
- **Logs**: `project_id`, `environment_id`, `level`, `service`, `trace_id`, `start_time`, `end_time`
- **Metrics**: `project_id`, `environment_id`, `metric_name`, `start_time`, `end_time`
- **Traces**: `project_id`, `environment_id`, `trace_id`, `start_time`, `end_time`
- **Incidents**: `project_id`, `environment_id`, `severity`, `status`, `start_time`, `end_time`

The `trace_id` filter lets operators pull every canonical event, log, or trace bearing a given trace ID — the raw material the trace detail view and trace cross-reference validation build on.

All list endpoints paginate (`page`, `page_size`, `total_pages`).

Trace detail: `GET /observability/traces/{trace_id}` returns the trace plus all spans, preserving parent/child order — the primitive the Phase 1 trace view builds on.

## 7. Self-observability

The API exposes its own health via `/health/live`, `/health/ready`, and `/health/dependencies`. Ingestion, processing, and API failures are captured in the structured `logging` module (`app/core/logging.py`). No secrets are exposed in health output.

## 8. Why this model

Everything later depends on these primitives:

- **Incident linking** reuses `incident_id` on events and `IncidentEvidence` rows with `source_id`
- **The knowledge graph** consumes the `system_components`/`component_dependencies` model described in [data-model.md](data-model.md)
- **Root-cause reasoning** in future phases will correlate across events, logs, metrics, traces, deployments, and incidents through these stable IDs
- **Retention policies** layer onto the existing timestamps and lifecycle metadata without schema changes — implemented in Phase 1 (policy + preview + sweep, §5a)
