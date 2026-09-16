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

## Phase 2 — Software Knowledge Graph

- Materialize the system map: components, dependencies, ownership, blast radius
- Versioned architecture snapshots
- Component health rollup across environments
- Impact graph for changes (which components does this deployment reach?)

## Phase 3 — Anomaly & Incident Intelligence

- Baseline learning and anomaly detection on normalized metrics
- Incident detection heuristics and grouping
- Evidence ranking (which log/metric/trace is most relevant)
- Alerting and notification wiring

## Phase 4 — Root Cause & Causal Analysis

- Causal reasoning over evidence correlations (events ↔ logs ↔ metrics ↔ traces ↔ deployments)
- Hypothesis generation and ranking
- Present **possible causes**, never certainty
- Bounded: no actions, diagnostics only

## Phase 5 — Failure Reproduction Engine

- Safe reconstruction of failures from captured behavior
- Sandboxed replay against recorded sequences
- No production impact — reproduction only in isolated environments

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
