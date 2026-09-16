# ARGUS Intelligence

**Software Reliability & Engineering Intelligence**

ARGUS is a platform that constructs a continuously evolving understanding of software behavior and connects observability evidence to software architecture, incidents, code changes, and deployments. It is being built to eventually diagnose failures, propose causes, and — with human approval — recommend and verify fixes.

This repository contains **Phase 0 — Foundation** and **Phase 1 — Observability & Ingestion**: the data model, REST API, deterministic seed data, test suite, Docker infrastructure, observability foundation, initial web application, and the Phase 1 ingestion surface (OpenTelemetry OTLP/JSON ingestion, background Redis queue + worker, source registry, retention policies, trace cross-reference validation, Prometheus `/metrics`, worker-based async ingestion). It establishes the architecture boundaries that later phases will build on; it does **not** yet perform anomaly detection, root cause analysis, or automated remediation.

---

## What ARGUS is

A software reliability command center, not a generic AI dashboard. ARGUS ties together:

- **Observability** — normalized logs, metrics, traces, and events
- **System Map** — components and their dependencies
- **Incidents** — grouped impacts with evidence, not guesses
- **Deployments** — change events correlatable to behavior changes
- **Projects & Environments** — the entities that contain all of the above

## What ARGUS does NOT do (yet)

The following are future phases, deliberately not implemented now:

- Anomaly detection
- AI evidence-ranking / root cause analysis
- Failure reproduction
- Automated fix generation / verification
- Safe autonomous remediation
- Repository cloning or code execution

Advanced diagnosis belongs to later phases. In Phases 0–1, ARGUS displays **evidence**, not conclusions.

## Repository layout

```text
apps/
  api/                  FastAPI backend (Python 3.14, async SQLAlchemy 2.0)
    app/
      api/v1/routes/    REST endpoints under /api/v1
      core/             config, database, logging, dependencies
      domain/           domain abstractions (future engine boundaries)
      models/           SQLAlchemy ORM models
      schemas/          Pydantic v2 request/response schemas
      services/         normalizer, ingestion pipeline, engine interfaces
    tests/              168 unit/integration tests across 11 modules + e2e smoke (46 checks)
    seed_data.py        deterministic demo data (ARGUS Demo Commerce)
    alembic/            database migrations
  web/                  Next.js 14 web application
infrastructure/
  docker/               auxiliary Docker assets
  scripts/              helper scripts
docs/                   architecture, development, data model, roadmap
docker-compose.yml      web + api + postgres + redis
```

## The stack

| Layer       | Technology                                        |
|-------------|---------------------------------------------------|
| API         | FastAPI + async SQLAlchemy 2.0 + Pydantic v2      |
| Database    | PostgreSQL 16 (JSONB), SQLite file-backed for tests |
| Cache/Queue | Redis 7                                           |
| Web         | Next.js 14 (App Router) + Tailwind CSS            |
| Migrations  | Alembic                                           |
| Tests       | pytest + pytest-asyncio + FastAPI TestClient      |
| Orchestration | Docker Compose                                  |

## Quick start

### 1. Configure environment

```bash
cp .env.example .env
```

### 2. Start the stack with Docker Compose

```bash
docker-compose up --build
```

This starts:

| Service  | Port | Notes                          |
|----------|------|--------------------------------|
| web      | 3000 | Next.js frontend               |
| api      | 8000 | FastAPI, docs at `/docs`       |
| postgres | 5432 | Persistent volume              |
| redis    | 6379 | Persistent volume              |

Health checks and `depends_on` ordering ensure services start in the correct order.

### 3. (Alternatively) Run the API locally

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### 4. Load seed data (optional)

```bash
cd apps/api
python seed_data.py
```

Loads the deterministic **ARGUS Demo Commerce** scenario — 7 components, dependency graph, deployments, traces, logs, metrics, and one incident with four pieces of evidence.

### 5. Run tests

```bash
cd apps/api
pytest
```

### 6. Run the frontend locally

```bash
cd apps/web
npm install
npm run dev
```

## Environment variables

See [`.env.example`](.env.example). Includes database, Redis, API, frontend, AI provider, authentication, logging, ingestion, and retention settings. Never commit real credentials.

## API overview

All endpoints are versioned under `/api/v1`. Interactive docs are available at `/docs` when `API_ENVIRONMENT=development`.

| Resource        | Endpoints (prefix `/api/v1`)                                  |
|-----------------|---------------------------------------------------------------|
| Projects        | `POST/GET /projects`, `GET/PUT/DELETE /projects/{id}`         |
| Environments    | `POST/GET /projects/{id}/environments`                        |
| Components      | `POST/GET /projects/{id}/components`                          |
| Dependencies    | `POST/GET /projects/{id}/dependencies`                        |
| Observability   | `POST/GET /observability/events`, `/logs`, `/metrics`, `/traces` |
| Traces & Spans  | `POST /observability/traces/spans`, `GET /observability/traces/{trace_id}` |
| Incidents       | `POST/GET /incidents`, `GET/PUT /incidents/{id}`              |
| Incident evidence | `POST/GET /incidents/{id}/evidence`                         |
| Deployments     | `POST/GET /deployments`, `GET /projects/{id}/deployments`, `PUT /deployments/{id}` |
| OTLP ingestion  | `POST /otlp/v1/traces`, `/otlp/v1/logs`, `/otlp/v1/metrics` (protojson camelCase + snake_case) |
| Source registry | `POST/GET /ingestion/sources`, `/ingestion/sources-health`, `POST/GET /ingestion/health-checks`, `/ingestion/config-changes` |
| Async ingestion | `POST /ingestion/webhook`, `/ingestion/queue` (202 → Redis → worker) |
| Operators       | `/ingestion/stats`, `/ingestion/dead-letter`, `/ingestion/trace-validation/{trace_id}`, `/ingestion/retention/{policy,preview,sweep}` |
| Metrics         | `GET /metrics` (Prometheus, `argus_*`)                        |
| Health          | `/health/live`, `/health/ready`, `/health/dependencies`        |

List endpoints share a consistent pagination contract:

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 20,
  "total_pages": 0
}
```

## Key principles

- **Versioned API from day one** — `/api/v1`
- **Strict request validation** — Pydantic `extra="forbid"` rejects unexpected fields
- **Consistent pagination** — event-heavy endpoints never return unbounded lists
- **Trust boundary** — external data (logs, traces, repos) is *data*, not instructions
- **Evidence, not conclusions** — Phase 0 never declares root cause
- **Bounded AI interfaces** — engine interfaces exist as contracts, with a mock provider for tests

## Security boundaries

ARGUS must never have unrestricted capabilities: no unrestricted shell/code execution, no production database access, no credential extraction, no infrastructure modification. Future remediation flows will follow:

```text
Proposal → Verification → Policy Check → Approval → Execution
```

See [docs/architecture.md](docs/architecture.md) for the full boundary and trust model.

## Documentation

- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Data model](docs/data-model.md)
- [Observability model](docs/observability-model.md)
- [Ingestion](docs/ingestion.md) — pipeline, Redis queue + worker, retries/backoff/dead-letter, SAVEPOINT isolation
- [Telemetry](docs/telemetry.md) — OTLP wire format, Prometheus `/metrics`, secret-rejection boundary
- [Roadmap](docs/roadmap.md)

## License

Proprietary. Internal use.
