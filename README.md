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
[![Tests](https://img.shields.io/badge/tests-866%20backend%20%2B%2083%20frontend-brightgreen)](#verification)
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
5. **Reproduce** a hypothesis: build an isolated sandbox for the relevant system
   state, replay sanitized inputs, inject controlled faults, capture what the
   sandbox actually did, and compare it against the original incident.

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
| **5** | Failure Reproduction Engine — isolated sandbox, sanitized replay, controlled faults, comparison, hypothesis validation | ✅ shipped | [docs/phase-5.md](docs/phase-5.md) |
| **6** | AI Debugger — reason over the full evidence model, data treated strictly as data | planned | [docs/roadmap.md](docs/roadmap.md) |

Phases 4 and 5 do **not** patch, deploy or remediate. Phase 4 explains from
stored evidence; Phase 5 runs a bounded experiment in a disposable sandbox and
reports what it observed — nothing is changed in your systems, and no result is
presented as proof.

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
                     Failure Reproduction
        plan ─► safety validation ─► disposable sandbox ─► sanitized replay
        ─► controlled faults ─► captured telemetry ─► comparison ─► verdict
                                 ▼
                     Next.js investigation UI
        system map · anomaly center · incidents · root cause analysis
        · reproduction workspace
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

<details>
<summary><b>Failure Reproduction (Phase 5)</b></summary>

- **A plan you can read before anything runs.** Strategy, target component,
  services, expected behaviour derived from the incident's own signals, resource
  limits, timeout and repetitions — and planning **never executes**: an
  experiment starts only on an explicit `confirm_sandbox=true`
- **Isolated sandboxes.** Per-service processes behind POSIX limits (default) or
  per-service containers on an internal network with `--cap-drop ALL` and a
  read-only root filesystem; loopback-only sockets, sanitized environment,
  bounded CPU / memory / disk / processes / telemetry
- **Sanitization that fails safe.** Secret-named subtrees are tainted wholesale,
  PII becomes deterministic pseudonyms (structure preserved, identity
  unrecoverable), and the replay engine re-checks immediately before sending —
  a failed check sends nothing
- **Sanitized replay.** Synthetic and recorded requests, events, messages and
  trace inputs; sequential by default; relative timing preserved because a
  failure is a property of ordering, not of the clock face
- **Controlled faults.** Latency, timeout, HTTP 4xx/5xx, connection failure,
  response corruption, resource pressure, dependency unavailable — applied
  inside one sandbox, with a telemetry-derived audit of how many requests they
  actually affected
- **Explainable comparison.** Eight independence dimensions (component, error,
  latency, trace topology, log patterns, failure sequence, temporal, recovery)
  each with its stored formula; an unavailable dimension is excluded, never
  scored as dissimilarity
- **Honest verdicts.** `SUPPORTED` / `PARTIALLY_SUPPORTED` / `NOT_SUPPORTED` /
  `INCONCLUSIVE` — with environment differences, missing inputs, repeatability as
  an observation over the runs that happened, and stated limitations. A failed
  reproduction is never reported as a refutation
- **Auditable artifacts.** Content-addressed (SHA-256) and immutable after the
  experiment: plan, manifest, environment snapshots, telemetry, comparison and
  validation
- **Cleanup you can check.** Every sandbox is destroyed on every path — success,
  failure, cancellation, timeout, or a dead worker — and
  `GET /reproductions/metrics` reports orphaned sandboxes and cleanup failures
  rather than hiding them
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
| Web UI | http://localhost:3000 | System map, incidents, root cause analysis, reproduction workspace |
| API | http://localhost:8000 | OpenAPI docs at `/docs` |
| Metrics | http://localhost:8000/metrics | Prometheus format |
| PostgreSQL | `localhost:5432` | Set `DATABASE_PORT` in `.env` if 5432 is taken |

The stack seeds a deterministic demo — **ARGUS Demo Commerce** (checkout →
inventory → datastore) — with a working dependency graph, scripted telemetry,
and the incidents that Phases 3, 4 and 5 analyse and reproduce. Everything the
demo shows is derived by the real engines from that telemetry; no answer is
seeded, and no reproduction result is hard-coded.

Reproduction runs sandboxes inside the API container by default — no extra
service and no Docker-in-Docker required. To run experiments against containers
instead of processes, set `REPRO_SANDBOX_BACKEND=docker` and mount the Docker
socket into the API service; the backend refuses clearly if the daemon is
unreachable rather than silently falling back.

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
| Backend test suite | `cd apps/api && pytest -q` | **866 passed** |
| Lint / format / types | `ruff check`, `ruff format --check`, `mypy app` | clean (116 modules / 161 files) |
| Frontend tests | `cd apps/web && npm test` | **83 passed** |
| Frontend type check | `cd apps/web && npx tsc --noEmit` | clean |
| Frontend production build | `cd apps/web && npm run build` | succeeds, 24 routes |
| Phase 0/1 live gate | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 live gate | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Phase 3 live gate | `bash infrastructure/e2e-smoke-phase3.sh` | **103/103** |
| Phase 4 live gate | `bash infrastructure/e2e-smoke-phase4.sh` | **70/70** |
| Phase 5 live gate | `bash infrastructure/e2e-smoke-phase5.sh` | **104/104** |
| Migration under a live pool | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase4.sh` | **73/73** |
| Fresh-database bootstrap | empty DB → `alembic upgrade head` → `seed_data.py` | 9 migrations apply from zero; demo incident, its analysis and its reproduction are derived correctly |
| Migrations reversible | `alembic upgrade head` / `downgrade -1` on PostgreSQL 16 | verified both directions |

The Phase 4 gate also exercises the browser views and proves that several
projects can be analysed side by side without contaminating each other: a second
system's evidence-free incident stays `UNKNOWN` while another project holds a
`HIGH`-confidence analysis, and every candidate resolves to a component of the
project being analysed.

The Phase 5 gate drives the whole reproduction engine through the real API —
plan → confirm → sandbox → replay → fault → capture → compare → validate →
artifacts → destroy — and additionally asserts what the engine *refuses*: a
start without confirmation, a start without a project scope, another project's
experiment, a shell-command fault target, an execution-shaped fault parameter and
a URL as a replay target. It then checks the security invariants that matter for
this phase: no credential value in an environment snapshot, no reproduction
signal in production telemetry, no sandbox left on disk, and no orphaned sandbox
or cleanup failure in the engine metrics.

## Project layout

```
apps/
  api/                    FastAPI backend (async SQLAlchemy 2.0, Pydantic v2)
    app/api/v1/routes/    REST endpoints (124 paths, 156 operations)
    app/core/             config, database, logging, dependencies
    app/models/           SQLAlchemy ORM models
    app/schemas/          request/response schemas
    app/services/         ingestion, detection, correlation, causal analysis, reproduction
    reproduction/         sandbox harness: runners, templates, environments, fixtures
    alembic/              9 reversible migrations
    tests/                unit, integration and scenario suites
  web/                    Next.js 14 + Tailwind UI
    app/system-map/       knowledge-graph explorer + impact/snapshot/quality panels
    app/incidents/        incident investigation, root cause analysis, reproduction history
    app/anomalies/        anomaly center and rules
    app/reproductions/    reproduction workspace (safety gate, live run, comparison, verdict)
    lib/                  typed API client, presentation rules, pure helpers
infrastructure/
  e2e-smoke-phase{1,2,3,4,5}.sh live end-to-end gates
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
- **A reproduction is an experiment, not a claim.** The result (what the sandbox
  observed) and the verdict (what that means for the hypothesis) are separate
  fields, and a failed reproduction is never presented as a refutation.
- **Nothing runs implicitly.** A plan executes nothing; an experiment starts only
  on explicit confirmation, with a project scope, inside a disposable sandbox.
- **Absence of evidence is not evidence of difference.** A comparison dimension
  that could not be measured is excluded from the score, not counted against the
  hypothesis.
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

Phase 5 is the one place ARGUS *executes* something, so its boundary is stated
and tested explicitly:

- **Nothing executable crosses the API.** A client names a logical sandbox
  service, an HTTP method/path, and a *typed* fault — never a command, URL,
  image, mount or arbitrary parameter. Fault parameters reject execution-shaped
  keys, and nested structures are refused outright.
- **Never against production.** A sandbox gets no production credentials, no
  production network, no host filesystem, and no egress by default; the plan
  records `production_access: BLOCKED` and `arbitrary_commands: REJECTED`.
- **Sanitized or it does not run.** A secret-named subtree is tainted wholesale,
  PII becomes deterministic pseudonyms, and the replay engine re-derives "is
  anything sensitive still here?" immediately before sending. A failed check
  sends nothing.
- **Bounded and disposable.** CPU, memory, disk, process, wall-clock and
  telemetry ceilings; loopback-only sockets; one sandbox per repetition; every
  sandbox destroyed on every exit path, with failures reported through the
  engine metrics rather than hidden.
- **Ownership is checked, not assumed.** Starting, cancelling or retrying an
  experiment requires a matching project scope; knowing an id is not authority to
  run it.

Remediation, when it arrives, will follow:

```
Proposal → Verification → Policy Check → Approval → Execution
```

See [docs/architecture.md](docs/architecture.md) for the full boundary and trust
model, and [docs/phase-5.md](docs/phase-5.md) §6 for the reproduction boundary
specifically.

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
| Reproduction | `/incidents/{id}/reproductions`, `/reproductions`, `/reproductions/metrics`, `/reproductions/{id}/{plan,safety,status,inputs,telemetry,artifacts,comparison,validation,environment,faults,manifest,start,cancel,retry}` |
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
- **A reproduction is an experiment under stated conditions.** It runs from
  synthetic fixtures, not from production state, and it cannot reproduce what was
  never instrumented or what depended on an external service. `SUPPORTED` means
  "consistent with this experiment's evidence", not "proven".
- **No authentication yet.** Isolation is server-side ownership validation on
  every request. Token auth and per-project authorization are on the roadmap.
- **Later phases are not started.** Automatic debugging, patch generation,
  remediation, autonomous deployment and predictive forecasting are deliberately
  absent — and Phase 5 does none of them: it reproduces, replays, compares and
  validates, nothing more.

## Roadmap

| Phase | Scope |
| :--- | :--- |
| 5 | ✅ Failure Reproduction Engine — sandboxed replay, controlled faults, comparison, hypothesis validation |
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
| [Phase 5 — Failure Reproduction Engine](docs/phase-5.md) | Architecture, sandbox design, security model, lifecycle, replay, faults, capture, comparison, validation, artifacts, cleanup, limitations |
| [Phase 2 Report](docs/phase2-implementation-report.md) · [Phase 3 Report](docs/phase3-implementation-report.md) · [Phase 4 Report](docs/phase4-implementation-report.md) · [Phase 5 Report](docs/phase5-implementation-report.md) | Delivery summaries, gate evidence, bugs found by live validation |
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
