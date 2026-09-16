# ARGUS Ingestion

How data gets into ARGUS — the synchronous HTTP path and the asynchronous Redis → worker path, the unified pipeline both run through, and the failure/retry/dead-letter guarantees around them.

## 1. Two entry paths, one pipeline

Every signal — event, log, metric, trace, span — flows through the same processing pipeline (`app/services/ingestion.py`):

```text
Source → Adapter → Validation → Redaction → Resolution → Normalization →
Correlation → Deduplication → Persistence (per-event SAVEPOINT)
```

The two differences from a consumer's point of view are *when* the client gets a response and *who* runs the pipeline:

| Path                     | Endpoints                                                        | Response      | Pipeline runs in      |
|--------------------------|------------------------------------------------------------------|---------------|-----------------------|
| Synchronous              | `/observability/*`, `/otlp/v1/*`, `/ingestion/bulk`              | 201/200/422   | HTTP request handler  |
| Asynchronous (queue)     | `/ingestion/webhook`, `/ingestion/queue`                         | 202 Accepted  | background worker     |

The 202-accepted endpoints enqueue jobs to Redis and return immediately; an `IngestionWorker` drains them and runs the identical pipeline. This is the case that proves **both** persistence paths end-to-end.

## 2. The pipeline stages

`IngestionPipeline.ingest_batch` walks each raw event through:

1. **Redaction** (`RedactionEngine`): copies the payload and rewrites sensitive values (`password`, `token`, `api_key`, JWTs, 32+ char hex/base64 blobs, …) to `[REDACTED]`. Defense in depth — the API schemas already reject known secret keys with 422.
   See [docs/telemetry.md](telemetry.md) §5 for the rejection boundary.
2. **Resolution**: `ComponentResolver` and `EnvironmentResolver` map knowledge-graph references (component name/type, environment) in the payload onto `component_id` / `environment_id`.
3. **Normalization** (`ObservabilityNormalizer`): maps the raw event to the canonical `ObservabilityEvent`, attaching project/environment scope, extracting `request_id` / `trace_id` / `span_id`, and deriving severity.
4. **Correlation** (`CorrelationEngine`): anchors the event to a `correlation_id` from shared request/trace/deployment anchors.
5. **Deduplication**: a canonical `EventFingerprint` (project, source, event type, timestamp, redacted payload) is checked against existing events; matches are counted as duplicates, not persisted twice.
6. **Persistence** with per-event isolation (below).

Failure bookkeeping is explicit — `IngestionResult` carries `accepted`, `duplicates`, `failed`, and per-event `failures`. Nothing is silently dropped.

## 3. Per-event SAVEPOINT isolation (the poison-session guard)

`ingest_batch` wraps each event in its own savepoint:

```python
async with self._db.begin_nested():      # SQLAlchemy SAVEPOINT
    ok = await self.ingest_one(raw, source_id=source_id)
```

A DB-level failure (e.g. a foreign-key violation from a bad `component_id`) rolls back **only that event**. The rest of the batch commits intact at the end, and the failed event is pushed to the dead-letter store (see §5). One misbehaving event can no longer poison the session for its siblings — a batch that previously committed nothing now keeps the good events.

## 4. The asynchronous path: Redis queue → worker

**Queue layout** — logical Redis lists, one per workload class (`app/services/queue.py`):

```text
argus:ingest:events   – normalized event ingest jobs
argus:ingest:traces   – trace (with spans) ingest jobs
```

**Job envelope** (`make_job`):

```json
{
  "kind": "event",
  "payload": {
    "project_id": "…", "environment_id": "…" | null, "source_id": "…" | null,
    "events": [ { "source_type": "WEBHOOK", "source_name": "…",
                  "timestamp": "<iso8601>", "event_type": "SYSTEM_EVENT",
                  "payload": {…}, "metadata": {…} } ]
  },
  "_retries": 0
}
```

`_retries` is worker bookkeeping on the payload — it is never persisted to the domain model.

**`IngestionQueue`** provides exactly three primitives: `push`, `pop` (non-blocking `blpop`), and the lazy Redis client. Corrupt (unparseable) payloads are consumed and dropped with a warning so they cannot stall the drain for valid jobs behind them — `pop() == None` always means *empty*, never *corrupt*.

**`IngestionWorker`** (`run_once` / `run_forever`):
- Drains up to `MAX_JOBS_PER_BATCH` (50) jobs across `ALL_QUEUES` per pass, then sleeps `poll_interval` (0.5 s).
- `run_forever` keeps the loop alive through `QueueUnavailable` and unexpected errors (logged, then re-polled) — the queue outage cannot kill the worker process.

**Job processing** (`process_event_job` in `app/services/worker_runner.py`) re-applies the secret-rejection guardrail (`_reject_secrets` on payload and metadata — mirrors the HTTP boundary, §46), parses UUIDs / ISO-8601 timestamps strictly so malformed jobs fail loudly, and runs the pipeline with `ingest_batch(..., source_id=source_id)`.

## 5. Retries, backoff, and dead-lettering

On a processing failure the worker does not drop the job:

```text
Job fails
  ├─ _retries < MAX_RETRIES (3)  → sleep BACKOFF_BASE × 2^retries → re-enqueue with _retries+1
  └─ _retries ≥ MAX_RETRIES      → write ingestion_failures row (redacted)   [dead-letter]
```

- **Exponential backoff**: `BACKOFF_SECONDS` (1.0 s) doubled per retry — 1 s, 2 s, 4 s.
- **Dead-letter store**: the `ingestion_failures` table receives the failing event only after its retry budget is exhausted. Entries are **redacted** — `payload_summary` stores key names and type markers, never values — and carry `error_type`, `error_message`, `retry_count`, and a deterministic `fingerprint`.
- **Poison-session safety**: the pipeline's `_dead_letter` rolls back a killed transaction before writing, so dead-lettering a failed event can never itself fail on a poisoned session. Idempotent — a re-failed event updates its existing failure row rather than duplicating it.
- **Unknown job kinds** are raised (not silently dropped) so they flow through the bounded retry and land in dead-letter with an explicit message.
- The dead-letter list is inspectable at `GET /ingestion/dead-letter` (operator surface, DoD §44).

## 6. Ingestion operators

- `GET /ingestion/stats` — pipeline counts (accepted / duplicates / failed) per project.
- `GET /ingestion/sources-health` — per-source health rollup from the source registry.
- `GET /ingestion/dead-letter` — redacted failed-event list.
- `GET /ingestion/retention/policy`, `GET /ingestion/retention/preview`, `POST /ingestion/retention/sweep` — retention policy lifecycle (see `docs/observability-model.md` §5 and `app/services/retention.py`).

## 7. Load characteristics

Measured against the live compose stack with the `infrastructure/load-benchmark.sh` script:

- 500 events enqueued to Redis in ~0.38 s (~1300 events/s) via the batch queue endpoint.
- Worker drained all 500 through the pipeline to persisted rows in ~2 s.
- Sync `POST /ingestion/bulk` persisted 50 events in ~87 ms.

Numbers are indicative of a single worker on a development laptop, not a capacity ceiling.
