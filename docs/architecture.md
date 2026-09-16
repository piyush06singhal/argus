# ARGUS Architecture

## 1. Overview

ARGUS is a software reliability & engineering intelligence platform. It continuously builds an understanding of software behavior by connecting normalized observability evidence to software architecture, incidents, code changes, and deployments.

This document describes the Phase 0 (Foundation) and Phase 1 (Observability & Ingestion) architecture and the boundaries that constrain future phases.

## 2. System diagram

```text
┌──────────────────────────┐      ┌──────────────────────────┐
│        Web (Next.js)     │      │   External Telemetry/     │
│    software reliability  │      │   Integration Clients     │
│        command center    │      │   (sources, OTEL, hooks)  │
└────────────┬─────────────┘      └────────────┬─────────────┘
             │                                 │
             │  HTTP (REST /api/v1)            │  HTTP ingestion
             ▼                                 ▼
┌────────────────────────────────────────────────────────────┐
│                    FastAPI (async)                         │
│                                                            │
│  Route (v1) ───────────────────► validation → redaction →  │
│   sync ingestion (bulk/log/┐    │ resolution → normalize →  │
│   metric/trace/OTLP)       │    │ correlate → dedup →      │
│   async webhook/queue      │    │ persist (per-event       │
│                            │    │ SAVEPOINT isolation)     │
│  /health/*   /metrics(Prom)│    │                          │
│  ingestion operators       │    └────► Redis queue            │
│  (stats/sources/dead-      │              │  argus:ingest:*  │
│   letter/retention)        │              ▼                  │
└───────────────┬────────────┴────────── IngestionWorker       │
                │ SQLAlchemy 2.0 async      (retry/backoff/    │
                ▼                            dead-letter)       │
┌──────────────────────────┐      ┌──────────────────────────┐
│      PostgreSQL 16       │      │        Redis 7            │
│   (primary data store,   │      │  (queue + dead-letter     │
│    JSONB documents)      │      │   accounting for async    │
│                          │      │   background ingestion)   │
└──────────────────────────┘      └──────────────────────────┘
```

## 3. Technology stack

| Layer       | Technology | Rationale |
|-------------|------------|-----------|
| API         | FastAPI, Python 3.14 | Async-first, typed with Pydantic, OpenAPI docs for free |
| ORM         | SQLAlchemy 2.0 (async) | AsyncSession + asyncpg; DB-agnostic JSON type (JSONB/JSON) |
| Validation  | Pydantic v2 | Strict (`extra="forbid"`), aliased `metadata` field, enum-driven |
| Database    | PostgreSQL 16 | JSONB documents, UUID PKs, relational integrity |
| Tests       | SQLite (file-backed) | Fast, isolated; JSONType degrades to JSON |
| Cache/Queue | Redis 7 | Async ingestion queue (`argus:ingest:*`) + dead-letter accounting |
| Web         | Next.js 14 (App Router) | Server components + typed fetch to `/api/v1` |
| Migrations  | Alembic (async) | Versioned schema evolution |
| Orchestration | Docker Compose | web / api / postgres / redis with health checks + ordering |

## 4. API layer

### 4.1 Versioning

All endpoints live under `/api/v1`, included via `APIRouter(prefix="/api/v1")`. The version prefix is a hard boundary: breaking changes go to `/api/v2`, never into v1 in-place.

### 4.2 Routes

```text
api/v1
  POST/GET  /projects
  GET/PUT/DELETE /projects/{id}
  POST/GET  /projects/{id}/environments
  POST/GET  /projects/{id}/components
  POST/GET  /projects/{id}/dependencies
  POST/GET  /observability/events            (+ GET /events/{id})
  POST/GET  /observability/logs
  POST/GET  /observability/metrics
  POST/GET  /observability/traces            (?trace_id filter)
  POST      /observability/traces/spans
  GET       /observability/traces/{trace_id} (returns trace + spans)
  POST/GET  /incidents
  GET/PUT   /incidents/{id}
  POST/GET  /incidents/{id}/evidence
  POST/GET  /deployments
  GET       /projects/{id}/deployments
  PUT       /deployments/{id}

ingestion (Phase 1)
  POST/GET  /ingestion/sources               (source registry §5/45)
  GET       /ingestion/sources/{id}
  POST/GET  /ingestion/sources-health        (§45 health summary)
  POST/GET  /ingestion/health-checks
  POST/GET  /ingestion/config-changes
  POST      /ingestion/webhook               (event webhook §47)
  POST      /ingestion/webhooks/{source_id}  (source-scoped webhook)
  POST      /ingestion/queue                 (batch async queue §37)
  POST/GET  /ingestion/bulk                  (sync bulk)
  GET       /ingestion/stats                 (§39)
  GET       /ingestion/dead-letter           (§44)
  GET       /ingestion/trace-validation/{trace_id}
  GET       /ingestion/trace-validation/{project_id}/project
  GET       /ingestion/retention/policy | preview
  POST      /ingestion/retention/sweep

otlp (Phase 1, protojson camelCase + snake_case §17)
  POST      /otlp/v1/traces | logs | metrics

prometheus (Phase 1, §40)
  GET       /metrics                       (argus_* exporter)

health
  GET       /health/live
  GET       /health/ready
  GET       /health/dependencies
```

### 4.3 Pagination contract

Event-heavy list endpoints share one contract:

```json
{
  "items": [ { ... } ],
  "total": 204,
  "page": 3,
  "page_size": 20,
  "total_pages": 11
}
```

- `page` ≥ 1, `page_size` 1–100
- Described in `Schema.pagination` docs
- Guarantees large event volumes are never returned unbounded

### 4.4 Validation & error handling

- Pydantic schemas run `extra="forbid"` — unknown request fields are rejected (422)
- Enum types enforce domain vocabularies (`ProjectStatus`, `IncidentSeverity`, `MetricType`, etc.)
- Filter parameters (e.g. `severity=CRITICAL`) persist across pagination
- A global handler returns `{ "detail": "Internal server error", "error_code": "INTERNAL_ERROR" }` (500) with no internals leaked
- 404s are explicit (`"Incident not found"`)

## 5. Data layer

See [docs/data-model.md](docs/data-model.md) for the full entity-relationship model. Highlights:

- UUID primary keys on all entities
- `TotalConst` style — `created_at`/`updated_at` on every row via `TimestampMixin`
- `JSONType` maps to PostgreSQL `JSONB` and SQLite `JSON`
- Foreign keys enforce project → environment/component/incident/deployment integrity
- Indexes on query paths (project/environment/component/timestamp) for pagination and filtering

## 6. Observability model

See [docs/observability-model.md](docs/observability-model.md).

Core design: a **normalized** representation shared by all sources — events, logs, metrics, traces, and spans — each with stable identity, explicit timestamps, and structured `metadata`.

Implemented pipeline stages (Phase 1):

```
Source → Adapter (OTLP/webhook/bulk/mock) → Validation → Redaction →
Resolution (component/environment) → Normalization → Correlation →
Deduplication (canonical fingerprint) → Persistence (per-event SAVEPOINT)
```

- Redaction (`services/redaction.py`) and schema-level secret rejection keep credentials out of the store (see §46).
- Correlation (`CorrelationEngine`) anchors shared `request_id`/`trace_id`/`deployment_id`.
- Trace cross-reference validation rejects/isolates orphaned spans (§20).

Both synchronous (HTTP) and asynchronous (Redis queue → `IngestionWorker`) paths run through this same pipeline — see [docs/ingestion.md](ingestion.md).

## 7. Domain boundaries (future engines)

The `app/services/engines.py` module defines abstract engine interfaces that later phases implement — they must remain **separate services**, not merged into one AI monolith:

| Boundary          | Responsibility |
|-------------------|----------------|
| Observability     | Collects and normalizes system behavior |
| Knowledge Graph   | Represents architecture and dependencies |
| Incident Engine   | Groups and manages incidents |
| Causal Engine     | Evaluates relationships between evidence |
| Reproduction Engine | Reconstructs failures safely |
| AI Debugger       | Reasons over evidence, proposes hypotheses |
| Fix Generator     | Generates candidate code changes |
| Verification      | Tests whether proposed changes fix the issue |
| Remediation       | Executes approved changes |

In Phase 0 these are interfaces (`NotImplementedError`) so the architecture cannot quietly collapse into a single monolith later. A `MockAIProvider` stands in for tests.

## 8. Security boundaries

### 8.1 Unrestricted capability prohibition

ARGUS must never have:

- Unrestricted shell execution
- Unrestricted code execution
- Production database access
- Production deployment
- Credential extraction
- Browser automation
- Infrastructure modification

### 8.2 Future remediation flow

Any future remediation uses:

```text
Proposal
   ↓
Verification
   ↓
Policy Check
   ↓
Approval
   ↓
Execution
```

Every step is a separate, auditable stage.

### 8.3 AI trust model (data vs instructions)

External system data — logs, traces, repository files, commit messages, and external responses — is **untrusted** and may contain malicious or misleading instructions.

Future AI components must treat it as:

```text
DATA              NOT        INSTRUCTIONS
```

This principle is established here, in Phase 0, so later phases inherit it. Nothing in Phase 0 executes content derived from observability data.

## 9. Observability of ARGUS itself

ARGUS monitors its own internal health:

- `GET /health/live` — is the process up?
- `GET /health/ready` — can it serve traffic (DB reachable)?
- `GET /health/dependencies` — per-dependency status + latency

Internal failures that ARGUS logs (ingestion, processing, API, database, queue) are tracked via the structured logging layer (`app/core/logging.py`). No secrets are exposed in health responses.

## 10. Pagination & performance foundation

- Indexes on all filter/query paths
- Consistent pagination on every list endpoint
- Bounded `page_size` (max 100)
- No endpoint returns unlimited observability data
- Background processing is live: Redis-backed queue (`argus:ingest:*`) with a draining `IngestionWorker` (bounded retries, exponential backoff, dead-lettering) — see [docs/ingestion.md](ingestion.md)

## 11. Auditability foundation

`app/domain/` and the core layer reserve an audit-event abstraction for future tracking of:

- configuration changes
- project changes
- incident state changes
- integration changes

Phase 1 materialized **configuration changes** and **health checks** as first-class ingestion entities (`/ingestion/config-changes`, `/ingestion/health-checks`), with ingestion **stats** and source **health/status** rollups surfaced to operators (§14/15/39/45).

## 12. Current limitations

- No authentication/authorization yet (readiness stubs present)
- No anomaly detection, no root-cause analysis, no remediation
- The AI trust model is documented policy, not yet enforced at runtime (no AI runs in the data path)
- Retention policy is configuration-only; sweep removes events by timestamp but does not cascade to related spans/logs
- Ingestion backpressure and worker scaling beyond single-process are not yet implemented
