# ARGUS Roadmap

ARGUS is built in phases. This document records the planned evolution. **Phases 0 and 1 are implemented. Do not implement later phases now** — the roadmap is a contract for architecture boundaries, not a to-do list.

## Phase 0 — Foundation ✅

- ✅ Data model: projects, environments, components, dependencies, observability (events/logs/metrics/traces/spans), incidents + evidence, deployments, code repositories
- ✅ REST API (`/api/v1`), validation, filtering, consistent pagination
- ✅ Deterministic seed data — ARGUS Demo Commerce (7 components, dependency graph, deployments, traces, logs, metrics, incident #1001)
- ✅ 68 tests (CRUD, failure paths, ingestion, isolation, health, duplicate/conflict handling, seed idempotency)
- ✅ Docker Compose: web + api + postgres + redis with health checks
- ✅ Web application: dashboard, projects, project detail, system map, incidents, observability (logs/metrics/traces), deployments, settings
- ✅ Alembic migrations, `.env.example`, documentation
- ✅ Architecture boundaries and AI trust model documented

**Boundaries set in Phase 0 (implemented as interfaces, not services):** anomaly detection, root-cause analysis, incident impact ranking, failure reproduction — all present only as contracts in `app/services/engines.py`; `MockAIProvider` for tests.

## Phase 1 — Observability & Ingestion ✅

- ✅ Real telemetry ingestion: OpenTelemetry protocol (OTLP) JSON for traces/logs/metrics (protojson camelCase
  and snake_case accepted, string-encoded nanos coerced), structured log ingestion, Prometheus-compatible scrape (`/metrics`)
- ✅ Background ingestion workers (Redis queue), batching, bounded retries with exponential backoff, dead-lettering
- ✅ Health/readiness of external telemetry sources (source registry, health-check + config-change events)
- ✅ Data retention policies on existing timestamp/lifecycle metadata (policy + sweep endpoints)
- ✅ Trace cross-reference validation (orphan span handling)

Verified end-to-end against the live compose stack: 46/46 smoke checks pass (Prometheus `/metrics`,
OTLP traces/logs/metrics, retention policy & sweep, trace validation, source registry, config-change &
health-check events, webhook, batch ingest, dead-letter inspection, ingestion stats, OTLP camelCase + snake_case, secret rejection at every boundary, event retrieval by id, pagination, web UI pages); 168 unit/integration
tests green.

## Phase 2 — Software Knowledge Graph ✅

- ✅ Materialized graph overlay over PostgreSQL (`graph_nodes`/`graph_edges` mirror canonical entities via `entity_kind`/`entity_id` — no duplicate representations, no graph database)
- ✅ Typed nodes (§7 categories projected from `ComponentCategory`), typed edges (CONTAINS/DEPENDS_ON/CALLS/READS_FROM/WRITES_TO/PUBLISHES_TO/CONSUMES_FROM/DEPLOYED_AS/IMPLEMENTS/…), provenance (`source`), and confidence (evidence strength, never causality)
- ✅ Component registry: identity resolution, explicit aliases, ownership (team/contact/repository owner), criticality (explicit only)
- ✅ Service endpoints with deterministic path-template normalization (`/api/checkout/{id}`)
- ✅ Discovery engine: weak evidence → PENDING suggestion records; explicit registration required to create nodes
- ✅ Trace→graph extraction (CALLS/READS_FROM from span trees + trace-carrying events), deployment edges, repository IMPLEMENTS edges
- ✅ Reconciliation that mirrors canonical state, appends evidence to `metadata.sources[]`, and **never deletes** (stale policy instead)
- ✅ Graph snapshots with set-level diffs; temporal foundation (`first_seen_at`/`last_seen_at`/status)
- ✅ Bounded query service: dependencies/dependents (direct + transitive), neighbors, shortest path (depth ≤ 25, nodes ≤ 2000), environment comparison
- ✅ Dependency Impact analyzer (downstream reachability, labeled "Dependency Impact" — not failure prediction)
- ✅ Data-quality checks + `/graph/health` aggregation (orphans, duplicates, unresolved deps, conflicts)
- ✅ Async ingestion hook: trace/span ingestion enqueues `graph_extract`; worker extracts + reconciles
- ✅ System Map rewritten as a real graph explorer (SVG, filters/search/selection/provenance legend) + Impact, Environments, Snapshots, Quality panels
- ✅ Security: project-scoped endpoints server-side, clamped traversal bounds, redacted metadata
- ✅ 356 unit/integration tests (188 new Phase 2 tests; Phase 0/1 suites unchanged) + 13 vitest frontend tests; benchmark at 100/500 and 1000/5000 scale (`infrastructure/graph-benchmark.py`)

See [docs/software-knowledge-graph.md](software-knowledge-graph.md) for the full design and [docs/phase2-implementation-report.md](phase2-implementation-report.md) for the delivery report.

## Phase 3 — Anomaly & Incident Intelligence ✅

- ✅ Deterministic baselines (STATIC + ROLLING: mean/median/stddev/min/max/p50/p95/p99) with explicit `INSUFFICIENT_DATA` handling — missing data is never treated as failure
- ✅ Detectors: threshold, baseline deviation, z-score, rate change, error rate, latency ratio, log-pattern spike, trace-failure rate, health transition — all pure, explainable functions with stored reasons
- ✅ Anomaly fingerprinting + deduplication registry + cooldown/persistence gating (one evolving anomaly, not one per sample)
- ✅ Explainable severity engine (magnitude, criticality, duration, blast radius) — every level records its inputs
- ✅ Graph-aware incident correlation with false-merge protection, a capped cluster span, and a stored rationale for every grouping
- ✅ Incident lifecycle with a validated state machine (409 on illegal transitions), auto-resolve, and reopen-on-recurrence; timeline, structured evidence with relevance reasons, and observed blast-radius classification
- ✅ Temporal context for deployments and configuration changes (`is_context_only`, explicit non-causality wording) and deterministic summaries generated from stored evidence only
- ✅ Auditable suppression rules and maintenance windows — anomalies are recorded, never silently dropped; deactivated via PATCH rather than deleted, so the record that detection was quiet survives
- ✅ Reliability metrics + Prometheus series (MTTA/MTTR with stated definitions), incident dashboard, anomaly center, incident investigation UI, Phase 2 graph-context overlay
- ✅ Project/environment ownership validation on every route (out-of-scope ids are 404, never data), bounded queries and pagination throughout
- ✅ Deterministic "ARGUS Checkout Latency Incident" demo that runs the real detection + correlation pipeline
- ✅ Exact scope everywhere it matters: `environment_id=None` means environment-less, never "all environments", so one environment's telemetry can never fire another's anomaly or join its incident
- ✅ 687 unit/integration tests + 25 vitest frontend tests; live Phase 3 smoke gate (103 checks, re-runnable); detection benchmark (`infrastructure/anomaly-benchmark.py`)

See [docs/phase-3.md](phase-3.md) for the full design and [docs/phase3-implementation-report.md](phase3-implementation-report.md) for the delivery report.

**Explicitly not included:** root-cause causal inference, automated diagnosis, reproduction, or remediation — those are Phase 4 and beyond.

## Phase 4 — Root Cause & Causal Analysis ✅

- ✅ Causal reasoning over stored evidence (events ↔ logs ↔ metrics ↔ traces ↔ deployments ↔ the Phase 2 knowledge graph) — no graph database, no new infrastructure
- ✅ Hypothesis generation and ranking from evidence only: bounded candidate set, one hypothesis per component, no candidate type assumed before the evidence is read
- ✅ Temporal analysis (ordering, gaps, simultaneity, persistence, recovery ordering) with `before ≠ caused` enforced as code
- ✅ **Directional** evidence from stored span trees; propagation order derived from when failures concluded, so nested failures cannot be inverted
- ✅ A causal graph whose every edge names the facts that justify it, and a validator that rejects impossible arrows
- ✅ Change analysis that refuses a deployment which happened *after* onset — recorded as `TEMPORAL_CONTRADICTION`, penalised, and given no causal edge
- ✅ Documented deterministic scoring (temporal, trace, dependency, propagation, change, recovery, resource, minus contradiction penalty) with **confidence kept separate from score**: `HIGH`/`MEDIUM`/`LOW`/`INSUFFICIENT`, never a probability
- ✅ Alternative hypotheses with supporting *and* contradicting evidence, and an explicit reason each one's confidence differs
- ✅ `UNKNOWN` at `INSUFFICIENT` is a first-class answer, with the missing evidence enumerated — ARGUS declines rather than guesses
- ✅ Versioned re-analysis (`POST /analyze`, idempotent; `force=true` appends) with history preserving what changed between versions
- ✅ Frontend investigation workspace: causal-graph explorer, evidence inspector (*why does ARGUS believe this edge exists?*), timeline synchronization, alternatives, history
- ✅ Project/environment isolation on every route; every traversal, window, candidate, edge and evidence list is bounded by configuration
- ✅ 761 unit/integration tests + 55 vitest frontend tests; live Phase 4 smoke gate (70 checks, re-runnable; covers the browser views and proves several systems can be analysed side by side without contaminating each other)

See [docs/phase-4.md](phase-4.md) for the full design and [docs/phase4-implementation-report.md](phase4-implementation-report.md) for the delivery report.

**Explicitly not included:** reproduction, automatic debugging, patch generation or application, autonomous remediation, self-healing, predictive forecasting. Phase 4 is *analyze, explain, hypothesize, validate, trace causality* — not *reproduce, fix, deploy*. Its output is an **evidence-supported hypothesis**, never proof.

## Phase 5 — Failure Reproduction Engine ✅

- ✅ Plan-reviewable experiments: strategy, target component, expected behaviour derived from the incident's own signals, resource limits, timeout and repetitions — and planning executes **nothing**
- ✅ Disposable isolation: per-service processes behind POSIX limits (default) or per-service containers on an internal Docker network (`--cap-drop ALL`, `no-new-privileges`, read-only root fs); loopback-only sockets, sanitized environment, bounded CPU/memory/disk/processes/telemetry
- ✅ Sanitization that fails safe: secret-named subtrees tainted wholesale, PII replaced by deterministic pseudonyms, and an independent pre-flight check inside the replay engine — a failed check sends nothing
- ✅ Sanitized replay of synthetic requests, events, messages and trace inputs; sequential by default; relative timing preserved, original wall-clock kept as provenance only
- ✅ Controlled fault injection (latency, timeout, HTTP 4xx/5xx, connection failure, response corruption, resource pressure, dependency unavailable) with a telemetry-derived audit of how many requests each fault actually affected
- ✅ Captured telemetry in its own `repro:<experiment-id>` namespace, labelled against expectations, with explicit `MISSING` observations so a clean run is distinguishable from one that never exercised the failure
- ✅ Explainable comparison over eight independence dimensions with stored formulas; an unavailable dimension is excluded, never scored as dissimilarity
- ✅ Four verdicts — `SUPPORTED`, `PARTIALLY_SUPPORTED`, `NOT_SUPPORTED`, `INCONCLUSIVE` — with environment differences, missing inputs, repeatability framed as an observation, and stated limitations. A failed reproduction is never a refutation
- ✅ Content-addressed (SHA-256), immutable artifacts; validated lifecycle with cooperative cancellation, deadlines and a reaper; unconditional cleanup with orphan/cleanup-failure metrics
- ✅ 866 backend tests (103 Phase 5) + 82 vitest tests; live Phase 5 gate (104 checks) covering the full engine **and** its refusals: no confirmation, no project scope, cross-project, shell-command fault target, execution-shaped fault parameter, URL replay target, credential leak, namespace bleed, sandbox leak

See [docs/phase-5.md](phase-5.md) for the full design and [docs/phase5-implementation-report.md](phase5-implementation-report.md) for the delivery report.

**Explicitly not included:** source-code modification, patch generation or application, autonomous deployment, production remediation, rollback and self-healing. Phase 5 is *reproduce, replay, experiment, compare, validate* — not *fix, deploy, remediate*.

## Phase 6 — AI Debugger

- Reason over evidence and propose debugging hypotheses
- Signals separated from noise across the full ARGUS data model
- Treat all external data as *data*, never instructions (see trust model)

## Phase 7 — Automated Fix Generation & Verification

- Generate candidate code changes from identified root causes
- Verify candidates against tests in isolation
- Produce proposed fixes requiring approval — never auto-applied

## Phase 8 — Predictive Reliability

- Trend, capacity, and reliability prediction from historical behavior
- Pre-incident signal detection

## Phase 9 — Safe Autonomous Remediation

- Proposal → Verification → Policy Check → Approval → Execution
- Strict policy enforcement; no unrestricted capabilities

## Phase 10 — Reliability Intelligence Platform

- Full product surface: reliability command center across projects, orgs, and teams
- Enhanced UI (system map health states, causal paths), reporting, insights

---

## Cross-cutting principles that survive every phase

1. **Evidence, not conclusions** — ARGUS can display correlation; declaring root cause arrives much later and always as a hypothesis.
2. **Trust boundary** — external data is data, not instructions.
3. **No unrestricted capabilities** — every future autonomous path requires verification, policy check, and approval.
4. **Boundary separation** — observability, knowledge graph, incident engine, causal engine, reproduction, debugger, fixer, verifier and remediator stay separate subsystems.
5. **Versioned, paginated API** — `/api/v1` contract persists; events never unbounded.
6. **Foundation-first** — retention, auditability, and observability-of-self are designed in from Phase 0, not bolted on.
