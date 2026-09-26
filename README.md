<div align="center">

# ARGUS

**Autonomous Software Reliability & Engineering Intelligence**

Observability tells you *that* something is wrong. ARGUS is built to answer *why* —
and to say so when it cannot.

[![CI](https://github.com/piyush06singhal/argus/actions/workflows/ci.yml/badge.svg)](https://github.com/piyush06singhal/argus/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-2093%20backend%20%2B%20248%20frontend-brightgreen)](#verification)
[![Python](https://img.shields.io/badge/python-3.12-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-20-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

**Quick start** · **[Console guide](docs/ui-guide.md)** · **[API](docs/api.md)** · **[Operations](docs/operations.md)** · **[Security](docs/security-architecture.md)** · **[Troubleshooting](docs/troubleshooting.md)**

</div>

---

ARGUS ingests telemetry from any OpenTelemetry source, builds a knowledge graph of
the software that produced it, detects and correlates anomalies into incidents, then
explains failures with evidence — reproduction, causal chains, code-level debugging,
verified patches, reliability forecasts and policy-controlled remediation.

The constraint that shapes every layer:

> **ARGUS never claims more than its evidence supports.** A root-cause candidate ships
> with its evidence counts, its score components, the facts that argue against it, and
> the reason its confidence is what it is. *"Insufficient evidence to determine a root
> cause"* is a first-class, tested outcome — not a failure mode.

---

## Contents

- [Why ARGUS](#why-argus)
- [Architecture](#architecture)
- [Capabilities](#capabilities)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Verification](#verification)
- [Project layout](#project-layout)
- [Security model](#security-model)
- [Design principles](#design-principles)
- [Limitations](#limitations)
- [Further upgrades](#further-upgrades)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## Why ARGUS

| Question an engineer asks | What answers it in ARGUS |
| :--- | :--- |
| What is abnormal, and against which baseline? | Deterministic detectors with the baseline they compared against |
| Which of these signals are one incident? | Fingerprint-based correlation with per-project scoping |
| What is the most likely cause? | Evidence-supported candidates, each with what contradicts it |
| Can it be reproduced? | A disposable sandbox, sanitized replay, controlled faults, comparison against the original |
| Where in the code? | A knowledge graph plus trace→code mapping, with every claim validated against indexed source |
| What is the fix, and does it work? | Smallest defensible patch, verified in isolation by your own checks and a two-sided regression test |
| What is at risk next? | Deterministic baseline forecasts with confidence, calibration and stated limits |
| What may be changed automatically? | A policy engine that denies by default, in a closed action registry — inert until a policy exists |
| What did we learn? | Validated, versioned patterns mined from completed outcomes, reviewed by a human |

Most of the work in a reliability platform is not detection. It is being correct about
the *unknown*, and refusing to look more certain than the evidence allows.

---

## Architecture

ARGUS is a **modular monolith**: one API, one web console, one PostgreSQL, one Redis.
Every phase shares the same incident, component and evidence vocabulary, so splitting
those into services would have replaced in-process joins with distributed ones for no
reliability gain.

### System overview

```mermaid
flowchart TB
    subgraph SRC["Signal sources"]
        OTEL["OpenTelemetry collectors and SDKs"]
        HOOK["Webhooks and batch API"]
        REPO["Git repositories"]
    end

    subgraph EDGE["API edge — FastAPI"]
        AUTH["Token auth, roles, project grants"]
        GUARD["Rate limits, body limits"]
        API["/api/v1 routes"]
    end

    QUEUE["Redis queues"]
    WORKER["Worker: ingestion, detection, sweeps<br/>leader-elected per pass"]
    DB[("PostgreSQL — the single source of truth")]
    WEB["Next.js console"]

    OTEL --> AUTH
    HOOK --> AUTH
    AUTH --> GUARD
    GUARD --> API
    API --> QUEUE
    QUEUE --> WORKER
    WORKER --> DB
    API --> DB
    REPO --> WORKER
    WEB --> API
    DB --> WEB
```

**Nothing is stored twice.** There is no second table with an opinion about whether an
incident is resolved; derived state is derived, and the rows that own a fact are the
ones that answer questions about it.

### The telemetry path

```mermaid
sequenceDiagram
    autonumber
    participant C as Collector
    participant A as ARGUS API
    participant Q as Redis
    participant W as Worker
    participant P as PostgreSQL

    C->>A: POST /api/v1/otlp/v1/traces
    Note over C,A: Protobuf or JSON, per-source ingest token
    A->>A: resolve project from the credential, validate scope
    A->>Q: enqueue normalized batch
    A-->>C: 202 Accepted
    Q->>W: BLPOP — each job goes to exactly one consumer
    W->>P: persist events, spans, logs, metrics
    W->>P: detect anomalies, correlate into incidents
    A->>P: read, always scoped to a project
    P-->>A: incidents, evidence, graph
```

If Redis is unavailable the API degrades honestly: health reports the dead broker with a
reason, ingestion falls back to synchronous processing so no telemetry is dropped, and
the queues drain when it returns. That path is a live gate, not an aspiration.

### The investigation pipeline

Each stage is a separate, independently gated engine. The arrows into **Human review**
are the ones that matter: nothing acts on your systems without a decision.

```mermaid
flowchart LR
    INC["Incident"] --> CA["Causal analysis"]
    CA --> REP["Reproduction"]
    REP --> DBG["Code-level debugging"]
    DBG --> FIX["Fix generation"]
    FIX --> VER["Patch verification"]
    CA --> PRED["Reliability forecast"]
    VER --> HUMAN{"Human review"}
    PRED --> HUMAN
    HUMAN --> REM["Remediation<br/>policy-gated, reversible"]
    REM --> LEARN["Learning from the outcome"]
    LEARN --> CA
```

What holds the pipeline together is the evidence model. A conclusion is never a scalar:

```mermaid
flowchart LR
    CLAIM["Candidate cause"] --> EV["Stored evidence"]
    EV --> SUP["Supporting facts"]
    EV --> CON["Contradicting facts"]
    CLAIM --> SCORE["Score components<br/>published"]
    CLAIM --> CONF["Confidence bucket<br/>evidence strength, never a probability"]
    CLAIM --> MISS["Missing evidence<br/>what would change the answer"]
```

### Deployment topologies

```mermaid
flowchart TB
    subgraph T1["Default — single api container"]
        direction LR
        A1["api: HTTP + worker + sweeps"] --- P1[("PostgreSQL")]
        A1 --- R1[("Redis")]
        W1["web"] --> A1
    end

    subgraph T2["Split — sustained write load"]
        direction LR
        A2["api: HTTP only"] --- P2[("PostgreSQL")]
        W2["worker replicas<br/>leader-elected sweeps"] --- P2
        A2 --- R2[("Redis")]
        W2 --- R2
    end
```

In the split topology the HTTP processes are taken out of the sweep business, and worker
replicas coordinate through a per-sweep PostgreSQL advisory lock — so adding replicas does
not duplicate passes, and a crashed worker costs one interval rather than the sweep.
See [operations.md](docs/operations.md) §6.

---

## Capabilities

| Phase | What it does | Its boundary | Design |
| :--- | :--- | :--- | :--- |
| **0** | Foundation: data model, versioned REST API, seed data, Docker, web console | — | [architecture](docs/architecture.md) |
| **1** | Ingestion: OTLP traces/logs/metrics in Protobuf **and** JSON, Redis queue + worker, retention, Prometheus | Per-source tokens; HMAC webhooks; dead-letter instead of silent loss | [ingestion](docs/ingestion.md) |
| **2** | Software knowledge graph: typed nodes/edges, provenance, reconciliation, snapshots, impact | Reconciliation marks `STALE`; it never deletes | [knowledge graph](docs/software-knowledge-graph.md) |
| **3** | Anomaly & incident intelligence: detectors, baselines, correlation, lifecycle, evidence | Exact environment scoping — one environment's telemetry can never fire another's anomaly | [phase 3](docs/phase-3.md) |
| **4** | Root cause & causal analysis: temporal, trace, dependency and change analysis, scored candidates, causal graph | Confidence is evidence strength, never a causality probability | [phase 4](docs/phase-4.md) |
| **5** | Failure reproduction: isolated sandboxes, sanitized replay, typed faults, telemetry comparison, hypothesis validation | No arbitrary command crosses the API; no production credentials, network or filesystem | [phase 5](docs/phase-5.md) |
| **6** | AI debugger: code intelligence, trace→code mapping, evidence-grounded analysis, validated code claims | Every reference is resolved against an evidence index; unresolvable claims are refused | [phase 6](docs/phase-6.md) |
| **7** | Fix generation & verification: fix hypotheses, smallest defensible patch, safety validation, disposable workspace, your own checks, two-sided regression test | Stops at human review. No merge, no deploy, no push | [phase 7](docs/phase-7.md) |
| **8** | Predictive reliability: feature engineering, deterministic baselines, risk policy, walk-forward backtesting, calibration, drift, early warnings | A forecast is never a fact, never opens an incident, never says a component "will fail" | [predictive reliability](docs/predictive-reliability.md) |
| **9** | Safe autonomous remediation: action registry, safety and policy gates, approval or policy-scoped autonomy, controlled execution, verification, rollback, hash-chained audit | Default deny, closed registry, inert until a policy exists, one-call emergency stop | [remediation](docs/safe-autonomous-remediation.md) |
| **10** | Reliability intelligence: normalized experiences, nine deterministic miners, validation, versioned and human-reviewed knowledge, component profiles, learned relationships | Learning never crosses a project boundary and cannot change policy, execute, or raise a limit | [intelligence](docs/reliability-intelligence.md) · [governance](docs/learning-governance.md) |
| **11** | Unified platform: derived system state, Reliability Cases with one timeline, evidence-gated workflow, service catalog, SLOs and error budgets, change intelligence, search, governance, platform health, reports | Adds orchestration and observability, not authority — Phase 9 stays the only thing that can act | [platform](docs/unified-reliability-platform.md) |

Full behaviour, guarantees and limitations per phase are in the documents above; the
[documentation index](#documentation) lists all 41 files.

---

## Quick start

**Requirements:** Docker with Compose (~4 GB free RAM, 10 GB disk). No local Python or
Node needed for the containerised path.

```bash
git clone https://github.com/piyush06singhal/argus.git
cd argus
cp .env.example .env            # the defaults work locally
docker compose up --build -d    # migrations and the idempotent seed run on boot
```

| Service | URL | Notes |
| :--- | :--- | :--- |
| Web console | http://localhost:3000 | System map, incidents, causal analysis, reproduction, fixes, platform |
| API | http://localhost:8000 | Interactive schema at `/docs` |
| Metrics | http://localhost:8000/metrics | Prometheus format |
| PostgreSQL | `localhost:5433` → container `5432` | Host port is **5433** on purpose: 5432 is usually taken by another project's Postgres. Override with `DATABASE_PORT` |
| Redis | `localhost:6379` | Queues and the in-memory rate-limit buckets |

**Sign in.** On first boot ARGUS mints one root `ADMIN` token and prints it once. It is
stored hashed and can never be shown again:

```bash
docker compose logs api | grep "bootstrap admin token"
# ARGUS bootstrap admin token (shown ONCE — store it now): argus_xxxx…
```

Open http://localhost:3000, go to **Connect**, paste the token. If it is lost, mint a
replacement from inside the container — this also prints a recovery notice on stderr:

```bash
docker compose exec api python -m app.cli bootstrap-token
docker compose exec api python -m app.cli create-token --name ci --role OPERATOR --project <uuid>
```

Every `/api/v1` route requires a token except `/`, the health probes and `/metrics`. Roles
are `VIEWER` (read), `OPERATOR` (read and write) and `ADMIN`, and a token can be scoped to
specific projects. `AUTH_DISABLED=true` is a local-development escape hatch and is
**refused at boot** when `API_ENVIRONMENT=production`.

**What you get on first boot.** A deterministic demo — *ARGUS Demo Commerce*
(checkout → inventory → datastore) — with a dependency graph, telemetry, and the
incidents that the detection, causal and reproduction engines analyse. Everything the
demo shows is derived by the real engines from that telemetry: no answer is seeded and no
reproduction result is hard-coded. Set `SEED_DEMO=false` to start empty, and read
[docs/demo.md](docs/demo.md) for exactly what it contains.

**Bring your own system instead.** [docs/quickstart.md](docs/quickstart.md) walks a
non-demo project from an empty database to a detected anomaly in about ten minutes using
only documented API calls, and [docs/onboarding.md](docs/onboarding.md) covers pointing a
real collector or SDK at ARGUS.

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

<details>
<summary><b>Sandbox backend for reproduction (Phase 5)</b></summary>

Reproduction runs sandboxes as processes inside the API container by default — no extra
service and no Docker-in-Docker. To run experiments against containers instead, set
`REPRO_SANDBOX_BACKEND=docker` and mount the Docker socket into the API service. The
backend refuses clearly when the daemon is unreachable rather than silently falling back.
</details>

---

## Configuration

`.env.example` is the complete, commented reference; the settings an operator changes
most often:

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `API_ENVIRONMENT` | `development` | Production semantics, refuses `AUTH_DISABLED`; disables `/docs` |
| `AUTH_DISABLED` | unset | Unset = auth enforced everywhere except tests. Local bypass only |
| `DATABASE_PORT` | `5433` | Host port for Postgres |
| `BACKGROUND_JOBS_ENABLED` | `true` | `false` on HTTP processes when running the split topology |
| `RATE_LIMIT_ENABLED` / `_PER_MINUTE` / `_BURST` | `true` / `600` / `120` | Edge token bucket, **per API process** |
| `MAX_REQUEST_BODY_BYTES` | see `.env.example` | Declared-oversize bodies are rejected with `413` before being read |
| `SEED_DEMO` | unset | Unset = seeded outside production; `false` = never |
| `CODE_INTELLIGENCE_ENABLED` / `CODE_ALLOWED_ROOTS` | `true` / `[]` | **Empty allowlist means nothing is readable** — the list is the boundary |
| `REMEDIATION_DEFAULT_MODE` | `OBSERVE_ONLY` | A project with **no policy row** resolves to observe-only: proposals are recorded, never authorized. This is what keeps remediation inert out of the box |
| `REMEDIATION_EXECUTION_ENABLED` | `true` | Master kill switch, independent of every policy: set `false` and no action may apply a live effect anywhere. Actions still run only through the closed registry |
| `RETENTION_*` | per table | Retention days per signal and per phase table |

---

## Verification

Every number below is reproducible from a clean checkout. The live gates run against the
Docker Compose stack and assert **invariants**, not fixtures, so they keep passing on a
busy database rather than only on a pristine one.

```bash
bash infrastructure/verify-all.sh            # everything, with a pass/fail summary
bash infrastructure/verify-all.sh --fast     # skip the production build and browser tier
GATES="phase8 hardening" bash infrastructure/verify-all.sh --live-only
```

| Layer | Command | Result |
| :--- | :--- | :--- |
| Backend suite (PostgreSQL, as CI runs it) | `cd apps/api && ARGUS_TEST_DB=postgresql+asyncpg://argus:argus_password@localhost:5433/argus_db pytest -q` | **2,185 passed, 1 skipped** |
| Backend suite (developer default, SQLite) | `cd apps/api && pytest -q` | **2,171 passed, 15 skipped** |
| PostgreSQL-gated proofs | `pytest tests/test_{migrations,project_lock,sweep_leader}_postgres.py` | **16 passed** — migration chain, per-project write mutex, sweep lease |
| Lint, format, types | `cd apps/api && ruff check app tests && ruff format --check app tests && mypy app` | clean (239 modules, 352 files formatted) |
| Frontend tests | `cd apps/web && npm test` | **263 passed** (14 files) |
| Frontend types, lint, build | `cd apps/web && npx tsc --noEmit && npm run lint && npm run build` | clean; build succeeds (50 static pages generated) |
| Live gates | `bash infrastructure/verify-all.sh --live-only` | **19 gates, 1,157 assertions, 0 failures** |
| Browser tier | `cd apps/web && npm run test:e2e` | **5 tests, 24 assertions** (Chromium) |
| Capacity | `python infrastructure/load-soak.py --scenario read --duration 15 --concurrency 8` | ~180–240 req/s plateau, **0 errors to 64 concurrent clients** |

<details>
<summary><b>The 19 live gates and what each one proves</b></summary>

| Gate | Assertions | Proves |
| :--- | :--- | :--- |
| `e2e-smoke-phase1.sh` | 46 | Foundation and ingestion: sync and both async drain paths, OTLP protojson **and** Protobuf, `/metrics`, secret rejection, retention |
| `e2e-smoke-phase2.sh` | 28 | Knowledge graph: reconcile, dependencies, paths, impact, environment comparison, snapshots, provenance |
| `e2e-smoke-phase3.sh` | 103 | Detection and correlation end to end |
| `e2e-smoke-phase4.sh` | 70 | Causal analysis, plus several projects analysed side by side without contaminating each other |
| `e2e-smoke-phase5.sh` | 104 | Reproduction: planning, safety refusals, sandbox lifecycle, comparison, verdict |
| `e2e-smoke-phase6.sh` | 159 | Code intelligence and the debugger, including refused code claims |
| `e2e-smoke-phase7.sh` | 90 | Patch generation, safety validation, verification ladder, refusal paths |
| `e2e-smoke-phase8.sh` | 42 | Forecasts, evaluation, backtesting, drift, early warnings |
| `e2e-smoke-phase9.sh` | 69 | Policy refusals first, then the allowed path, rollback and audit |
| `e2e-smoke-phase10.sh` | 87 | Experience building, miners, validation, knowledge lifecycle, recommendations |
| `e2e-smoke-phase11.sh` | 90 | Derived state, cases, workflow, catalog, objectives, governance, reports |
| `e2e-smoke-onboarding.sh` | 45 | The new-user path on a non-demo project, including accepted counts rather than status codes alone |
| `e2e-ui-smoke.sh` | 73 | Every route renders against live data |
| `e2e-smoke-hardening.sh` | 15 | Authentication and tenant isolation: refusals over real HTTP, no cross-project read or write |
| `e2e-smoke-faults.sh` | 10 | Redis killed mid-ingest: degraded honestly, still ingest, recover, drain, lose nothing |
| `e2e-smoke-backup.sh` | 15 | A real dump restores; a truncated one is refused; live drift does not break the drill |
| `e2e-smoke-observability.sh` | 22 | Prometheus + Grafana come up, and **every series the alert rules and the dashboard reference exists in the live scrape** — an anti-fiction check, because a rule that can never fire reads as coverage |
| `e2e-smoke-ha.sh` | 19 | A streaming, read-only replica catches up, and a **rehearsed point-in-time recovery** to a chosen moment succeeds (with a no-target control run proving the archive really replays) |
| `e2e-smoke-sso.sh` | 65 | Single sign-on over a real socket against a strict stub IdP: discovery, JWKS, PKCE verified at the provider, a session the edge then **accepts**, and every refusal (replayed state, forged state, unverified email, disabled identity, provider-side revocation) |
| `playwright test` | 5 tests | The UI as a browser experiences it, including the token boundary |

</details>

---

## Project layout

```
apps/
  api/                     FastAPI backend — async SQLAlchemy 2.0, Pydantic v2
    app/api/v1/routes/     REST surface (301 paths, 345 operations, 22 route modules)
    app/core/              config, database, security, edge middleware, logging
    app/models/            SQLAlchemy ORM models (19 modules)
    app/schemas/           request/response schemas
    app/services/          the engines: ingestion, detection, correlation, causal
                           analysis, reproduction, code intelligence, fixes,
                           forecasting, remediation, learning, platform (157 modules)
    reproduction/          sandbox harness: runners, templates, environments, fixtures
    alembic/               reversible migrations (23 revisions, 127 tables from zero)
    tests/                 108 test modules
  web/                     Next.js 14 + Tailwind console (68 pages)
    app/system-map/        knowledge-graph explorer, impact, snapshots, data quality
    app/incidents/         investigation, causal analysis, reproduction history
    app/reproductions/     reproduction workspace: safety gate, live run, comparison
    app/debugger/          AI debugger: sessions, analysis audit, hypotheses
    app/fixes/             fix and verification workspace
    app/reliability/       forecasts, heatmap, backtests, accuracy
    app/intelligence/      pattern explorer, learned relationships, recommendations
    app/platform/          cases, catalog, objectives, changes, search, governance,
                           reports, data quality, health, activity
    lib/                   typed API client, presentation rules, pure helpers
infrastructure/
  e2e-smoke.sh             Phase 0 end-to-end lifecycle across every API surface
  e2e-smoke-*.sh           the 18 phase, onboarding, UI, security, faults, backup,
                           observability and HA shell gates
  verify-all.sh            one command for the whole matrix
  backup.sh                dump, verify (full decompression), restore, drill
  load-soak.py             load and soak harness
  graph-benchmark.py       graph traversal benchmarks
  anomaly-benchmark.py     detection and correlation benchmarks
docs/                      42 documents — see the index below
docker-compose.yml         web + api + postgres + redis (optional worker profile)
```

---

## Security model

ARGUS is designed to have no unrestricted capabilities: no shell or code execution, no
production database access, no credential extraction, no infrastructure modification.
External data — logs, traces, payloads — is treated as **data, never as instructions**.

- **Every route is authenticated and scoped.** Bearer tokens with roles and per-project
  grants; an out-of-scope identifier is a `404`, never data.
- **Ingestion has its own boundary.** Per-source ingest tokens (rotatable, shown once,
  stored hashed), HMAC-verified webhooks with replay refusal, size and depth limits, and
  secret redaction before anything is stored.
- **The one place that executes is bounded by construction.** Phase 5 names logical
  services and *typed* faults; it never accepts a command, URL, image or mount. No
  production credentials, no production network, no host filesystem, no egress by
  default. Reproduction does not fabricate certainty.
- **Remediation is default-deny.** A closed registry, a safety assessment that cannot be
  overridden, a policy decision, an explicit scope, a risk ceiling, verification, rollback
  where the action allows, and a hash-chained audit.

Out of the box **nothing executes**: the master switch (`REMEDIATION_EXECUTION_ENABLED`)
is on, but a project with no policy row resolves to `OBSERVE_ONLY`, which records
proposals and never authorizes them — and the shipped action set only touches ARGUS's
own control plane. Setting the master switch to `false` stops every action everywhere,
whatever any policy says.
- **Isolation is enforced in SQL**, not in a handler that remembered to check.

Full threat model, credential types and fail-closed design:
[security-architecture.md](docs/security-architecture.md).

---

## Design principles

- **Evidence, not conclusions.** Every candidate carries its supporting and
  contradicting facts, and its displayed counts match the evidence shown beneath them.
- **Absence of evidence is not evidence of difference.** A dimension that could not be
  measured is excluded from scoring, never counted against a hypothesis.
- **Exact scope where it decides data.** `environment_id=None` means *environment-less*,
  never "all environments".
- **Bounded by construction.** Every traversal, window, candidate set and list is clamped
  server-side; no query scans the telemetry database for one incident.
- **Deterministic where it can be.** Same input, same output, same explanation. AI is an
  optional interpretation layer that may re-word what the engine established — it cannot
  invent evidence, alter a score, or override a contradiction.
- **Reconciliation never deletes.** Disappeared relationships become `STALE`; detection
  that went quiet stays on record.
- **Nothing runs implicitly.** A plan executes nothing; an experiment starts only on
  explicit confirmation, inside a disposable sandbox with a project scope.
- **Migrations are reversible and verified** against a real database in CI, not only
  against the developer's SQLite file.

---

## Operating ARGUS

The operational gaps found in the first audit are closed, and each one is gated — a
capability without a live proof is a claim, and this document does not make those.

| Capability | What ships | Proof |
| :--- | :--- | :--- |
| **Single sign-on (OIDC)** | Authorization-code + PKCE, JWKS ID-token verification, claim→role/grant mapping, database-backed single-use state, and every attempt written to the auth audit trail | `infrastructure/e2e-smoke-sso.sh` (65) — two real processes over TCP, with a stub IdP that verifies PKCE and client auth, plus `apps/api/tests/test_hardening_oidc.py` (49 tests, in-process fake IdP) |
| **Alert rules & dashboards** | 20 Prometheus alert rules and a 22-panel Grafana overview, provisioned under the `observability` profile | `infrastructure/e2e-smoke-observability.sh` (22) — every referenced series must exist in a **live scrape** |
| **Scheduled backup** | A scheduler that dumps, rehearses a restore and prunes on an interval, recording freshness in Postgres | `infrastructure/e2e-smoke-backup.sh` (15) |
| **PostgreSQL HA & PITR** | A streaming replica plus continuous WAL archiving under `docker-compose.ha.yml` | `infrastructure/e2e-smoke-ha.sh` (19) — streams a real write, then recovers to a chosen moment |
| **Shared rate limiting** | A Redis-backed bucket so the ceiling is global across replicas, with an honest per-process fallback that is alerted on | `apps/api/tests/test_hardening_rate_limit.py` (15) |
| **Docs-consistency guard** | Documentation that cannot silently drift: runbook links, alert names and gate coverage are asserted in CI | `apps/api/tests/test_docs_consistency.py` |

Read [operations.md](docs/operations.md) for the alert runbook, rate limiting, backups and
disaster recovery, and [high-availability.md](docs/high-availability.md) for the replica,
point-in-time recovery and the honest scope of multi-region.

---

## Limitations

Stated plainly: a reliability tool that overstates itself is worse than useless. Most of
these are the *price of the model* rather than unfinished work — they are what honesty
costs, and they will not change.

**Inherent to the design**

- **No causal instrumentation.** ARGUS infers from stored telemetry. Even a `HIGH`
  confidence candidate is evidence-supported, not instrumented end to end.
- **Traces decide direction.** Without span trees, direction is a hypothesis and the
  confidence ceiling reflects that.
- **A reproduction is an experiment under stated conditions.** `SUPPORTED` means
  "consistent with this experiment's evidence", not "proven".
- **Predictions are expectations, not facts.** Deterministic baselines are strong on trends
  and weak on interactions, and stay `UNKNOWN` until there is enough history behind them.
- **Fix verification inherits your test coverage.** A two-sided regression test is derived
  from the patch; ARGUS cannot know what your suite does not cover.
- **Learned patterns are correlations with a sample count.** Three observations is the
  default floor before a pattern may leave `CANDIDATE`, and there is no
  false-discovery-rate control at the level of a journal.
- **Causality and learning do not cross project boundaries.** Projects are separate causal
  universes: analysed side by side, never reasoned about together.
- **Derived state is as good as its inputs.** Where nothing was instrumented the state is
  `UNKNOWN` — a quiet system and an unobserved one look alike unless you read the evidence
  coverage.
- **Autonomous execution carries residual risk.** Bounded, scoped, reversible where the
  action allows, breaker-limited, audited and revocable — and inert until someone writes a
  policy, because a missing policy resolves to `OBSERVE_ONLY`.

**Gaps we have not closed** — each one is listed with an approach and rough size under
[Further upgrades](#further-upgrades):

- **Remediation reaches ARGUS's own runtime.** Anything touching your infrastructure is
  refused with `ADAPTER_UNAVAILABLE` until an operator configures an adapter.
- **Reads are not routed to replicas.** A streaming replica exists for durability and
  failover (see [high-availability.md](docs/high-availability.md)), but the API uses one
  DSN — read-your-writes routing is not implemented, so do not point it at a replica.
- **Redis is not replicated.** A region that loses its Redis falls back to per-process
  rate limiting until it returns (surfaced by `argus_rate_limit_backend`), and a region
  that loses its *host* is a failover decision, not an automatic one.
- **The frontend runtime carries advisories we triaged rather than fixed.** Next.js
  14.2.35 is flagged by `npm audit` (12 high/critical). None is reachable in ARGUS's
  shape — no `next/image`, no server actions, no `middleware.ts`, Linux containers only —
  and the only real fix is the breaking Next 16 upgrade, which is **deferred and tracked**
  in [supply-chain-triage.md](docs/supply-chain-triage.md) with its migration surface and
  exit criteria. Re-reviewed on every `next` bump.
- **Capacity is measured on one host.** Roughly 180–240 req/s per token at 8 concurrent
  clients, zero errors to 64; run `infrastructure/load-soak.py` on your hardware before
  sizing anything.

The full per-phase list, including the phase-specific ones, is in
[production-readiness.md](docs/production-readiness.md) §7.

---

## Further upgrades

Everything through Phase 11 is shipped, and none of the items below is required for the
platform to work — that is the point of listing them separately. Each is a real, scoped
piece of work with the machinery already in place around it.

| Upgrade | Why it matters | Rough size |
| :--- | :--- | :--- |
| **Replicated Redis and read routing** | The HA overlay gives durability and failover; it does not split reads or survive a whole-region Redis loss | weeks |
| **OTLP/gRPC listener on 4317** | Some collectors are configured gRPC-only. OTLP/HTTP with Protobuf already covers the rest | days |
| **Reference remediation adapters (Kubernetes, cloud APIs)** | The registry, policy, safety, verification and rollback machinery is complete and waiting; only adapters are missing | weeks |
| **Measured multi-worker throughput and write-path load at 64 clients** | The sweep lease makes replicas *safe*; their throughput is unmeasured, and the write path is characterised only at 8 | days |
| **Opt-in cross-project knowledge pooling** | Teams running the same software in several projects currently learn nothing across them | weeks |
| **Next.js 16 upgrade** | The only fix npm offers for the triaged Next 14 advisories; deliberately deferred, with the migration surface and exit criteria recorded in [supply-chain-triage.md](docs/supply-chain-triage.md) | days |

**Deliberately not on this list:** hosted SaaS, injecting an agent into your workloads,
merging or deploying changes, and any path where a model decides what to execute. Those
are out of scope for the design, not backlog.

See [roadmap.md](docs/roadmap.md) for the per-phase history of how the platform got here.

---

## Documentation

**Getting started** — [Quickstart](docs/quickstart.md) · [Onboarding a real service](docs/onboarding.md) · [Demo dataset](docs/demo.md) · [Console guide](docs/ui-guide.md) · [Troubleshooting](docs/troubleshooting.md)

**Platform reference** — [API](docs/api.md) · [Architecture](docs/architecture.md) · [Data model](docs/data-model.md) · [Observability model](docs/observability-model.md) · [Telemetry contract](docs/telemetry.md) · [Ingestion](docs/ingestion.md) · [Knowledge graph](docs/software-knowledge-graph.md) · [Development](docs/development.md)

**Operating** — [Operations](docs/operations.md) · [High availability & PITR](docs/high-availability.md) · [Security architecture](docs/security-architecture.md) · [Production readiness](docs/production-readiness.md) · [Benchmark report](docs/argus-benchmark-report.md) · [Final architecture audit](docs/final-architecture-audit.md)

**Phase design** — [Phase 3: anomaly & incident intelligence](docs/phase-3.md) · [Phase 4: causal analysis](docs/phase-4.md) · [Phase 5: reproduction](docs/phase-5.md) · [Phase 6: AI debugger](docs/phase-6.md) · [Phase 7: fix generation](docs/phase-7.md) · [Phase 8: predictive reliability](docs/predictive-reliability.md) · [Phase 9: safe remediation](docs/safe-autonomous-remediation.md) · [Phase 10: reliability intelligence](docs/reliability-intelligence.md) · [Learning governance](docs/learning-governance.md) · [Phase 11: unified platform](docs/unified-reliability-platform.md)

**Assurance and history** — [Production hardening plan](docs/PRODUCTION-HARDENING-PLAN.md) · [Supply-chain triage](docs/supply-chain-triage.md) · [Roadmap](docs/roadmap.md) · Delivery reports: [Phase 2](docs/phase2-implementation-report.md) · [Phase 3](docs/phase3-implementation-report.md) · [Phase 4](docs/phase4-implementation-report.md) · [Phase 5](docs/phase5-implementation-report.md) · [Phase 6](docs/phase6-implementation-report.md) · [Phase 7](docs/phase7-implementation-report.md) · [Phase 8](docs/phase-8-report.md) · [Phase 9](docs/phase-9-report.md) · [Phase 10](docs/phase-10-report.md) · [Phase 11](docs/phase-11-report.md)

Every delivery report is a snapshot taken when that phase shipped; the matrix above is
the current state.

---

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. `cd apps/api && pytest -q` — the backend suite must stay green.
2. `cd apps/api && ruff check app tests && ruff format --check app tests && mypy app`
3. `cd apps/web && npx tsc --noEmit && npm test && npm run lint`
4. If you touch a phase's behaviour, run its live gate
   (`bash infrastructure/e2e-smoke-phase<N>.sh`) against the compose stack.
5. `bash infrastructure/verify-all.sh` before you ask for a review.

Conventions worth preserving: exact environment scoping, reversible migrations, bounded
queries, no conclusion without its evidence, and documentation that states limits as
clearly as capabilities. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).

---

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Piyush Singhal.
