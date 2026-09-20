<div align="center">

# ARGUS

**Autonomous Software Reliability & Engineering Intelligence**

A software reliability platform that builds a continuously evolving model of how a
system behaves and how it is built — then connects telemetry to architecture,
incidents, and change, so a failure can be explained instead of guessed at.

[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-20-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-761%20backend%20%2B%2055%20frontend-brightgreen)](#verification)
[![Migrations](https://img.shields.io/badge/migrations-reversible-informational)](docs/development.md)

</div>

---

## What ARGUS is

Most observability stacks tell you *that* something is wrong. ARGUS is built to
answer *why* — and to be honest about when it cannot.

It does this in layers, each one resting on the last:

1. **Ingest** telemetry from any OpenTelemetry source — logs, metrics, traces,
   events — through a durable queue, with retention and validation.
2. **Model** the software that produced it: a knowledge graph of components,
   environments, endpoints, dependencies and their owners, with provenance on
   every relationship.
3. **Detect** abnormal behaviour deterministically, with baselines that say what
   "normal" meant, and group related signals into incidents.
4. **Explain** failures: evidence-supported hypotheses, the causal chain that
   connects the evidence, how confident the system is, and what contradicts the
   conclusion.

The design constraint that shapes everything: **ARGUS never claims more than its
evidence supports.** A root-cause candidate ships with its evidence counts, its
score components, the facts that argue against it, and the reason its confidence
is what it is. `"Insufficient evidence to determine a root cause"` is a
first-class, tested outcome — not a failure mode.

## Status

| Phase | Scope | State | Design |
| :--- | :--- | :--- | :--- |
| **0** | Foundation — data model, REST API, seed data, Docker, web app | ✅ shipped | [docs/architecture.md](docs/architecture.md) |
| **1** | Observability & Ingestion — OTLP, Redis queue + worker, retention, Prometheus | ✅ shipped | [docs/ingestion.md](docs/ingestion.md) |
| **2** | Software Knowledge Graph — typed nodes/edges, provenance, reconciliation, snapshots, impact | ✅ shipped | [docs/software-knowledge-graph.md](docs/software-knowledge-graph.md) |
| **3** | Anomaly & Incident Intelligence — detectors, baselines, correlation, lifecycle, evidence | ✅ shipped | [docs/phase-3.md](docs/phase-3.md) |
| **4** | Root Cause & Causal Analysis — temporal/trace/dependency/change analysis, causal graph, scoring | ✅ shipped | [docs/phase-4.md](docs/phase-4.md) |
| **5** | Failure Reproduction Engine | planned | [docs/roadmap.md](docs/roadmap.md) |

Phase 4 does **not** reproduce, patch, or remediate. It analyzes, explains,
hypothesizes, validates and traces causality — nothing is done to your systems.

## How it works

```
                    OpenTelemetry / webhooks / API
                                 │
                                 ▼
                    Ingestion ─► Redis queue ─► worker
                                 │
                                 ▼
        ┌────────────────────────────────────────────────┐
        │  PostgreSQL — the single source of truth       │
        │  telemetry · incidents · evidence · graph      │
        └────────────────────────────────────────────────┘
               │                │                 │
               ▼                ▼                 ▼
        Knowledge Graph   Anomaly Detection   Causal Analysis
        (structure)       + Correlation       (explanation)
               │                │                 │
               └────────────────┴─────────────────┘
                                 ▼
                     Next.js investigation UI
        system map · anomaly center · incidents · root cause analysis
```

## Features

<details open>
<summary><b>Observability &amp; ingestion (Phase 1)</b></summary>

- OTLP/JSON ingestion for traces, logs and metrics (protojson camelCase and
  snake_case), plus native REST ingestion endpoints
- Durable async pipeline: `202` → Redis → worker, with dead-letter capture and
  per-source health
- Trace cross-reference validation, span-tree storage, retention policies with
  preview and sweep, Prometheus `/metrics`
</details>

<details>
<summary><b>Software Knowledge Graph (Phase 2)</b></summary>

- Typed nodes and relationships mirrored from canonical entities — a graph
  *overlay*, never a duplicate representation
- Provenance (`Configured` / `Observed` / `Inferred`) and confidence semantics on
  every edge
- Component identity with aliases and ownership; service endpoints with
  normalized path templates; trace-driven relationship discovery
- Reconciliation that marks relationships `STALE` instead of deleting them,
  versioned snapshots with set-level diffs, environment comparison, dependency
  impact analysis, data-quality checks, and an interactive SVG graph explorer
</details>

<details>
<summary><b>Anomaly &amp; Incident Intelligence (Phase 3)</b></summary>

- Deterministic baselines (static and rolling: mean/median/stddev/p50/p95/p99)
  with explicit `INSUFFICIENT_DATA` — missing data is never treated as failure
- Nine detectors — threshold, baseline deviation, z-score, rate change, error
  rate, latency ratio, log-pattern spike, trace-failure rate, health transition —
  each a pure function that stores why it fired
- Fingerprint deduplication and cooldown so one condition is one evolving
  anomaly, not one per sample
- Graph-aware incident correlation with false-merge protection and a stored
  rationale for every grouping; validated lifecycle state machine; timelines;
  structured evidence with provenance and relevance reasons; blast-radius
  classification
- Auditable suppression and maintenance windows: anomalies are recorded, never
  silently dropped; suppressions are deactivated via `PATCH`, not deleted
- Reliability metrics (MTTA/MTTR with stated definitions) and Prometheus series
</details>

<details>
<summary><b>Root Cause &amp; Causal Analysis (Phase 4)</b></summary>

- **Evidence-driven candidates**, not a detector that guesses: bounded
  generation from anomalies, failing span trees, the dependency graph and change
  events — one hypothesis per component, no type assumed before the evidence is
  read
- **Direction from stored spans.** A failing child span inside a parent span is a
  record of the call that failed; propagation order is derived from when failures
  concluded, so nested failures cannot be inverted
- **Temporal analysis** with `before ≠ caused` enforced as code: precedence,
  gaps, simultaneity, persistence, recovery ordering
- **Change analysis that refuses a late deployment.** A change after onset is a
  `TEMPORAL_CONTRADICTION`: penalised, and never given a causal edge
- **A causal graph where every edge names the facts that justify it**, and a
  validator that rejects impossible arrows
- **Deterministic scoring** (temporal, trace, dependency, propagation, change,
  recovery, resource — minus a contradiction penalty) with **confidence kept
  separate from score**: `HIGH` / `MEDIUM` / `LOW` / `INSUFFICIENT`. No probability
  is ever emitted
- **Alternative hypotheses** shown with supporting *and* contradicting evidence,
  and why each one's confidence differs
- **Versioned re-analysis.** Identical evidence returns the stored analysis; new
  evidence appends a version and preserves what changed
- **An investigation workspace** — causal-graph explorer, evidence inspector
  ("why does ARGUS believe this edge exists?"), timeline synchronization, history
</details>

## Quick start

**Requirements:** Docker with Compose. Nothing else — no local Python or Node
needed for the containerised path.

```bash
git clone https://github.com/piyush06singhal/argus.git
cd argus
cp .env.example .env          # defaults work locally
docker compose up --build -d  # migrations + idempotent seed run on boot
```

| Service | URL | Notes |
| :--- | :--- | :--- |
| Web UI | http://localhost:3000 | System map, incidents, root cause analysis |
| API | http://localhost:8000 | OpenAPI docs at `/docs` |
| Metrics | http://localhost:8000/metrics | Prometheus format |
| PostgreSQL | `localhost:5432` | Set `DATABASE_PORT` in `.env` if 5432 is taken |

The stack seeds a deterministic demo — **ARGUS Demo Commerce** — with a working
dependency graph, scripted telemetry, and the incidents that Phases 3 and 4
analyse. Everything the demo shows is derived by the real engines from that
telemetry; no answer is seeded.

<details>
<summary><b>Running the API and web app outside Docker</b></summary>

```bash
# API
cd apps/api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head && python seed_data.py
uvicorn app.main:app --reload

# Web
cd apps/web
npm install
npm run dev
```
</details>

## Verification

Every number below is reproducible from a clean checkout. The live gates run
against the Docker Compose stack and assert **invariants**, not fixtures — so
they pass on repeat runs, not only on a pristine database.

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend test suite | `cd apps/api && pytest -q` | **761 passed** |
| Lint / format / types | `ruff check`, `ruff format --check`, `mypy app` | clean (98 modules / 137 files) |
| Frontend tests | `cd apps/web && npm test` | **55 passed** |
| Frontend type check | `cd apps/web && npx tsc --noEmit` | clean |
| Frontend production build | `cd apps/web && npm run build` | succeeds, 22 routes |
| Phase 0/1 live gate | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 live gate | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Phase 3 live gate | `bash infrastructure/e2e-smoke-phase3.sh` | **103/103** |
| Phase 4 live gate | `bash infrastructure/e2e-smoke-phase4.sh` | **70/70** |
| Migration under a live pool | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase4.sh` | **73/73** |
| Fresh-database bootstrap | empty DB → `alembic upgrade head` → `seed_data.py` | 8 migrations apply from zero; demo incident and its analysis are derived correctly |
| Migrations reversible | `alembic upgrade head` / `downgrade -1` on PostgreSQL 16 | verified both directions |

The Phase 4 gate also exercises the browser views and proves that several
projects can be analysed side by side without contaminating each other: a second
system's evidence-free incident stays `UNKNOWN` while another project holds a
`HIGH`-confidence analysis, and every candidate resolves to a component of the
project being analysed.

## Project layout

```
apps/
  api/                    FastAPI backend (async SQLAlchemy 2.0, Pydantic v2)
    app/api/v1/routes/    REST endpoints (106 paths, 137 operations)
    app/core/             config, database, logging, dependencies
    app/models/           SQLAlchemy ORM models
    app/schemas/          request/response schemas
    app/services/         ingestion, detection, correlation, causal analysis
    alembic/              8 reversible migrations
    tests/                unit, integration and scenario suites
  web/                    Next.js 14 + Tailwind UI
    app/system-map/       knowledge-graph explorer + impact/snapshot/quality panels
    app/incidents/        incident investigation + root cause analysis
    app/anomalies/        anomaly center and rules
    lib/                  typed API client, presentation rules, pure helpers
infrastructure/
  e2e-smoke-phase{1,2,3,4}.sh   live end-to-end gates
  graph-benchmark.py            graph performance benchmark
  anomaly-benchmark.py          detection/correlation benchmark
docs/                     architecture, data model, per-phase design + reports
docker-compose.yml        web + api + postgres + redis
```

## Design principles

- **Evidence, not conclusions.** A root-cause candidate is an evidence-supported
  hypothesis with a stated confidence and a visible list of what contradicts it.
- **Score is not confidence.** A deterministic score with published components
  sits beside a coarse confidence bucket; no probability is ever emitted.
- **Contradictions are surfaced, not smoothed.** A change that happened after
  onset reduces confidence and never becomes a causal edge.
- **Every conclusion is auditable.** Each causal edge names its stored facts, and
  a candidate's displayed counts always match the evidence shown beneath them.
- **Provenance everywhere.** Configured / Observed / Inferred is always visible;
  confidence means evidence strength, never a causality probability.
- **Reconciliation never deletes.** Disappeared relationships become `STALE`;
  detection that went quiet remains on record.
- **Exact scope where it decides data.** `environment_id=None` means
  *environment-less*, never "all environments" — one environment's telemetry can
  never fire another's anomaly or join its incident.
- **Bounded by construction.** Every traversal, window, candidate set and list is
  clamped server-side; no query scans the telemetry database for one incident.
- **Deterministic where it can be.** Same input, same output, same explanation;
  AI is an optional interpretation layer that may only re-word what the engine
  established — it cannot invent evidence, alter scores, or override a
  contradiction.

## Security &amp; trust model

ARGUS is designed to have no unrestricted capabilities: no shell or code
execution, no production database access, no credential extraction, no
infrastructure modification. External data — logs, traces, payloads — is treated
as **data, never as instructions**. Project and environment isolation is enforced
in SQL on every query; an out-of-scope identifier is a `404`, never data.

Remediation, when it arrives, will follow:

```
Proposal → Verification → Policy Check → Approval → Execution
```

See [docs/architecture.md](docs/architecture.md) for the full boundary and trust
model.

## API overview

All endpoints are versioned under `/api/v1`; interactive documentation is at
`/docs` when `API_ENVIRONMENT=development`.

| Area | Endpoints |
| :--- | :--- |
| Projects & environments | `/projects`, `/projects/{id}/environments`, `/projects/{id}/components` |
| Observability | `/observability/{events,logs,metrics,traces}`, `/observability/traces/spans` |
| Ingestion | `/ingestion/{sources,sources-health,webhook,queue,stats,dead-letter}`, `/ingestion/retention/*` |
| OTLP | `/otlp/v1/{traces,logs,metrics}` |
| Knowledge graph | `/projects/{id}/graph/*` (nodes, edges, search, paths, snapshots, diff, environments/compare, reconcile, health, discovery, data-quality, endpoints) |
| Components | `/components/{id}/{graph/*,endpoints,owner,aliases}` |
| Anomalies | `/anomalies`, `/anomaly-rules`, `/anomaly-suppressions`, `/maintenance-windows`, `/projects/{id}/anomalies/detect` |
| Incidents | `/incidents`, `/incidents/{id}/{timeline,anomalies,evidence,components,graph,summary}` and lifecycle actions |
| Reliability | `/projects/{id}/{reliability-metrics,incident-dashboard}` |
| Causal analysis | `/incidents/{id}/{analyze,causal-analysis,causal-graph,causal-chain,root-causes,hypotheses,evidence-analysis}` |
| Ops | `/health/{live,ready,dependencies}`, `/metrics` |

List endpoints share one pagination contract:

```json
{ "items": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 0 }
```

## Limitations

Stated plainly, because a reliability tool that overstates itself is worse than
useless:

- **No causal instrumentation.** ARGUS infers from stored telemetry. Even a
  `HIGH` confidence candidate is evidence-supported, not instrumented end to end.
- **Traces decide direction.** Without span trees, direction is a hypothesis and
  the confidence ceiling reflects it.
- **Causality does not cross project boundaries.** Each project is its own causal
  universe; many systems are analysed side by side, but not reasoned about
  together.
- **Re-analysis is idempotent, not incremental.** Unchanged evidence is
  returned as stored; new evidence produces a full deterministic pass as a new
  version.
- **Detection and inference are bounded by configuration.** A window that is too
  short hides evidence — and the analysis says so through `missing_evidence`.
- **No authentication yet.** Isolation is server-side ownership validation on
  every request. Token auth and per-project authorization are on the roadmap.
- **Later phases are not started.** Reproduction, automatic debugging, patch
  generation, remediation and predictive forecasting are deliberately absent.

## Roadmap

| Phase | Scope |
| :--- | :--- |
| 5 | Failure Reproduction Engine — sandboxed replay, no production impact |
| 6 | AI Debugger — reason over the full evidence model, data treated strictly as data |
| 7 | Automated Fix Generation & Verification — candidates verified in isolation, never auto-applied |

See [docs/roadmap.md](docs/roadmap.md) for detail.

## Documentation

| Document | Contents |
| :--- | :--- |
| [Architecture](docs/architecture.md) | System design, boundaries, trust model |
| [Software Knowledge Graph](docs/software-knowledge-graph.md) | Overlay design, node/edge semantics, provenance, reconciliation |
| [Phase 3 — Anomaly & Incident Intelligence](docs/phase-3.md) | Detectors, baselines, correlation, lifecycle, evidence model |
| [Phase 4 — Root Cause & Causal Analysis](docs/phase-4.md) | Causal model, evidence model, analyzers, scoring, confidence, graph, frontend |
| [Phase 2 Report](docs/phase2-implementation-report.md) · [Phase 3 Report](docs/phase3-implementation-report.md) · [Phase 4 Report](docs/phase4-implementation-report.md) | Delivery summaries, gate evidence, bugs found by live validation |
| [Data model](docs/data-model.md) | Tables, relationships, enum domains, indexes |
| [Observability model](docs/observability-model.md) | Signals, normalization, retention |
| [Development](docs/development.md) | Local setup, migrations, testing conventions |

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. `cd apps/api && pytest -q` — the backend suite must stay green.
2. `cd apps/api && ruff check app tests && ruff format --check app tests && mypy app`
3. `cd apps/web && npx tsc --noEmit && npm test && npm run lint`
4. If you touch a phase's behaviour, run its live gate
   (`bash infrastructure/e2e-smoke-phase<N>.sh`) against the compose stack.

Conventions worth preserving: exact environment scoping, reversible migrations,
bounded queries, and the rule that new user-visible conclusions ship with their
evidence.

## License

No license file is present yet, so all rights are reserved by the author. If you
intend to use, fork or redistribute ARGUS, please open an issue to ask for a
license to be added first.
