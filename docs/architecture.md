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
| Self-monitoring | Prometheus + Grafana (compose `observability` profile) | 20 alert rules with runbooks + a provisioned dashboard over the `argus_*` exporter |
| Backup | `pg_dump` + a continuously archived WAL, `backup` profile | Scheduled dump with a verification drill, retention pruning; a dump is *not* a WAL archive |
| HA / PITR | `docker-compose.ha.yml` overlay | Streaming replica + archive-based point-in-time recovery; deliberately no automatic failover (see [high-availability.md](high-availability.md)) |
| Orchestration | Docker Compose | web / api / worker / postgres / redis, with opt-in `observability`, `backup` and HA profiles; health checks + ordering |

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

graph (Phase 2 — Software Knowledge Graph)
  GET       /projects/{id}/graph                        (nodes + edges payload)
  GET       /projects/{id}/graph/nodes | edges          (paginated, filterable)
  GET       /projects/{id}/graph/search?q=              (nodes/endpoints/repos/aliases)
  GET       /projects/{id}/graph/paths                  (bounded BFS, both endpoints
                                                         must share the project)
  GET/POST  /projects/{id}/graph/snapshots              (list / create)
  GET       /graph/snapshots/{id}
  GET       /graph/snapshots/{a}/diff/{b}               (set-level diff)
  GET       /projects/{id}/graph/environments/compare   (structural env diff)
  POST      /projects/{id}/graph/reconcile              (mirror canonical state)
  GET       /projects/{id}/graph/health                 (quality aggregation + ok)
  GET       /projects/{id}/graph/discovery              (PENDING suggestions)
  POST      /projects/{id}/graph/discovery/{rid}/register | ignore
  GET       /projects/{id}/graph/data-quality
  GET       /projects/{id}/graph/endpoints
  GET       /components/{id}/graph/dependencies | dependents | neighbors
  GET       /components/{id}/graph/impact               (Dependency Impact)
  GET/POST  /components/{id}/endpoints
  GET/PUT   /components/{id}/owner
  GET/POST  /components/{id}/aliases

anomalies & incidents (Phase 3 — Anomaly & Incident Intelligence)
  GET       /anomalies                          (filter by project/env/component/type/
                                                 severity/status/source/metric/incident/fingerprint)
  GET       /anomalies/{id}                     (detail + observations + explanation §52)
  POST      /anomalies/{id}/acknowledge | resolve
  GET/POST  /anomaly-rules                      (validated rule CRUD)
  GET/PATCH /anomaly-rules/{id}
  GET/POST  /anomaly-suppressions               (auditable suppression rules)
  GET/POST  /maintenance-windows                 (§43)
  POST      /projects/{id}/anomalies/detect      (one bounded detect + correlate pass)
  GET       /projects/{id}/reliability-metrics   (§44, MTTA/MTTR with definitions)
  GET       /projects/{id}/incident-dashboard    (§36 aggregates + series)
  GET       /incidents/{id}/timeline | anomalies | components | graph
  GET       /incidents/{id}/deployments | configuration-changes | summary
  POST      /incidents/{id}/acknowledge | investigate | mitigate | resolve | reopen
  POST      /incidents/{id}/timeline             (NOTE only — facts are derived)

prometheus (Phase 1, §40; Phase 3 series added in §44)
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

The Software Knowledge Graph (Phase 2) is a **materialized overlay** over the canonical tables: `graph_nodes`/`graph_edges` reference canonical entities via `entity_kind` + `entity_id` (unique per project) — no duplicate component/dependency representations, no separate graph database. See [docs/software-knowledge-graph.md](software-knowledge-graph.md).

See [data-model.md](data-model.md) for the full entity-relationship model. Highlights:

- UUID primary keys on all entities
- `TotalConst` style — `created_at`/`updated_at` on every row via `TimestampMixin`
- `JSONType` maps to PostgreSQL `JSONB` and SQLite `JSON`
- Foreign keys enforce project → environment/component/incident/deployment integrity
- Indexes on query paths (project/environment/component/timestamp) for pagination and filtering

## 6. Observability model

See [observability-model.md](observability-model.md).

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

These remain separate services, not one AI monolith. `AnomalyDetector` and
`IncidentCorrelator` are now implemented by real, deterministic engines —
`AnomalyDetectionService` and `IncidentCorrelationEngine` — which is why the
correlation layer can be unit-tested without a model in the loop. The remaining
interfaces (causal engine, reproduction, debugger, fix generator, verification,
remediation) stay abstract: they raise `NotImplementedError`, so Phase 4+ cannot
start by accident.

### 7.1 Phase 3 detection & correlation pipeline

```
Ingested telemetry (Phase 1)
      │  POST /observability/*  → enqueue anomaly_detect
      │  scheduled sweep (ANOMALY_SWEEP_INTERVAL_SECONDS)
      ▼
Baseline engine  ──►  Deterministic detectors  ──►  Anomalies
(STATIC | ROLLING)     (pure functions, stored        (fingerprinted,
                        reasons, no AI scores)         deduplicated,
                                                       suppression-aware)
      ▼
Correlation engine  ──►  Incident manager  ──►  Timeline / Evidence /
(shared evidence,        (lifecycle state      Blast radius / Context /
 graph adjacency,         machine, dedup        Deterministic summary
 window + span cap)       by fingerprint)
      ▼
Phase 2 knowledge graph context  ──►  API  ──►  Incident Intelligence UI
```

Ingestion hooks and the periodic sweep both call the same idempotent service, so
they cannot double-count: fingerprints and the `anomaly_fingerprints` registry
make a second evaluation an update rather than a new record.

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
- `GET /metrics` — the `argus_*` Prometheus exporter, including self-observability
  series (rate-limit backend and fallbacks, backup age/duration/size)

Internal failures that ARGUS logs (ingestion, processing, API, database, queue) are tracked via the structured logging layer (`app/core/logging.py`). No secrets are exposed in health responses.

**Shipped, not aspirational.** Bringing the stack up with the `observability`
profile provisions Prometheus, Grafana and 20 alert rules — each with a runbook
link into [operations.md](operations.md) — plus a pre-built dashboard. The
`backup` profile adds a scheduled `pg_dump` with a periodic restore drill and
retention pruning; the `ha` overlay adds a streaming replica and WAL archiving
for point-in-time recovery. These are proved by
`infrastructure/e2e-smoke-observability.sh`, `e2e-smoke-backup.sh`,
`e2e-smoke-ha.sh` and `e2e-smoke-sso.sh`; the honest boundaries (no automatic failover, no read
routing, a single Redis node) are stated in
[high-availability.md](high-availability.md) and
[production-readiness.md](production-readiness.md). Authentication federates to
an OIDC provider when `OIDC_*` is configured, with `argus_rate_limit_backend`
showing whether shared rate limiting is active.

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

This section listed the gaps of the *Phase 1* system and went stale as the later
phases closed them. What follows is the state of the shipped platform:
**established** (with the evidence), then **genuinely open**.

**No longer limitations** (each is implemented and proved by a live gate):

- Authentication and authorization — bearer tokens with roles and per-project
  grants, per-source ingest tokens, `401`/`404` refusals over real HTTP
  (`infrastructure/e2e-smoke-hardening.sh`); SSO/OIDC sign-in when configured,
  with sessions retained under their own policy
  (`infrastructure/e2e-smoke-sso.sh`).
- Operational alerting, dashboards, scheduled backups and Postgres HA/PITR —
  alert rules with runbooks, a provisioned dashboard, a dump-plus-drill
  scheduler and a streaming replica with WAL archiving
  (`e2e-smoke-observability.sh`, `e2e-smoke-backup.sh`, `e2e-smoke-ha.sh`,
  `e2e-smoke-sso.sh`).
- Anomaly detection, incident correlation, causal/root-cause analysis,
  reproduction, fix generation, predictive reliability, policy-controlled
  remediation and cross-incident learning — Phases 3–10, each with its own gate.
- The AI trust model is **enforced at runtime**, not merely documented: every fact
  handed to the model carries an evidence id, every reference it returns is
  resolved against that index, and an unresolvable claim is reported as refused
  rather than displayed (`app/services/ai_debugger.py`).
- Worker scaling — the queue consumer is replica-safe (Redis `BLPOP` hands each
  job to exactly one consumer) and sweep work is arbitrated by a per-sweep
  PostgreSQL advisory lock, so replicas do not duplicate passes
  (`app/services/sweep_leader.py`, proved in `tests/test_sweep_leader_postgres.py`).

**Genuinely open** (documented at the same detail in
[production-readiness.md](production-readiness.md) §7):

- Retention is per table and does not cascade from a parent row to its children;
  the windows are deliberately different lengths (traces 30 days, events 90).
- No OTLP/gRPC listener on 4317 — OTLP/HTTP is served in both encodings.
- Reads are not routed to the HA replica, and failover is manual: the overlay
  removes single-node Postgres durability risk, not availability orchestration.
- No multi-region replication, and Redis is a single node.
- Write-heavy scenarios above 8 concurrent clients are unmeasured (reads and
  metrics are characterised to 64).
- Reproduction needs a runnable sandbox image, and code intelligence needs a
  registered repository — without them those surfaces degrade honestly
  (`ENVIRONMENT_UNAVAILABLE`, "no readable repository") rather than pretending.
