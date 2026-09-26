# ARGUS Phase 4 — Completion Report

> **Snapshot, not current state.** This is the report written when the phase
> shipped, and its numbers are from that run. The authoritative, current
> verification matrix lives in the [README](../README.md#verification);
> nothing here is kept in sync with later work.


**Root Cause & Causal Analysis Engine**

Status: **complete — every increment delivered, all gates green, live end-to-end
validated, frontend included.**

Phase 4 changes what ARGUS can say. Before it: *"something abnormal happened"*.
After it: *"here are the most evidence-supported explanations for why it
happened, the chain that connects the evidence, and how confident we are"* — and,
when the evidence does not support an answer, *"there is not enough evidence to
determine the root cause"*.

---

## 1. Implemented

| Area | Delivered |
| --- | --- |
| Domain model | `CausalAnalysis`, `RootCauseCandidate`, `CausalRelationship`, `CausalEvidence` + 7 Postgres enums |
| Migration | `f1a2b3c4d5e6_phase4_causal_analysis`, reversible, verified up/down on live PostgreSQL 16 |
| Analyzers | `TemporalAnalyzer`, `DependencyAnalyzer`, `TraceAnalyzer`, `ChangeAnalyzer` |
| Engine | `CausalCandidateGenerator`, `CausalGraphBuilder`, `CausalValidator`, `RootCauseScorer` |
| Orchestration | `CausalAnalysisService` — versioned, idempotent, audited, bounded |
| Explanation | `CausalExplanationService` + `RootCauseExplanationProvider` seam (deterministic default) |
| API | 11 routes incl. `POST /analyze`, analysis/graph/chain/hypotheses/evidence-analysis/history/edge explanations |
| Frontend | RCA summary page, causal-graph explorer, evidence inspector, timeline synchronization, analysis history, RCA index |
| Demos | checkout chain (§48), counterexample (§49), unknown root cause (§50) |
| Live gate | `infrastructure/e2e-smoke-phase4.sh` — 70 checks across API, browser, and multi-project isolation |
| Tests | 74 new backend tests, 30 new frontend tests, plus the live gate |
| Docs | `docs/phase-4.md`, `docs/phase4-implementation-report.md`, README, roadmap, data model, `.env.example` |
| Docs | `docs/phase-4.md`, this report, README, roadmap, data model |

---

## 2. Architecture

```text
                       INCIDENT (Phase 3)
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   anomalies            changes             span trees
   (Phase 3)        (deployments /        (Phase 1 traces)
                     config changes)
        │                    │                    │
        └────────┬───────────┴──────────┬─────────┘
                 ▼                      ▼
        DependencyAnalyzer        TemporalAnalyzer
      (Phase 2 graph + edges)   (ordering, gaps, recovery)
                 │                      │
                 └──────────┬───────────┘
                            ▼
                CausalCandidateGenerator        ← bounded, evidence-originated
                            ▼
                    CausalGraphBuilder          ← edges only where facts justify them
                            ▼
                     CausalValidator            ← rejects impossible arrows
                            ▼
                     RootCauseScorer            ← score ≠ confidence
                            ▼
              primary candidate  or  UNKNOWN
                            ▼
                 CausalExplanationService       ← structured reasons + limitations
```

No graph database, no new infrastructure: Phase 4 reads Phase 0–3 tables and owns
four of its own.

---

## 3. Database

New tables (only new tables — nothing existing was altered):

| Table | Purpose |
| --- | --- |
| `causal_analyses` | one versioned analysis run per incident (`(incident_id, analysis_version)` unique) |
| `root_cause_candidates` | hypotheses; score and confidence kept separate |
| `causal_evidence` | facts bound to a candidate, optionally narrowed to an edge |
| `causal_relationships` | directed hypothesis edges of the per-incident causal graph |

Enum types created and dropped with the migration: `analysisstatus`,
`candidatetype`, `candidatestatus`, `causalrelationshiptype`,
`causalevidencecategory`, `evidencepolarity`, `confidencelevel`.

Reversibility verified on live PostgreSQL: `alembic upgrade head` →
`alembic downgrade -1` → `upgrade head`, back at head.

---

## 4. Backend

### Services and analyzers

`apps/api/app/services/`: `causal_analysis_service.py`,
`temporal_analyzer.py`, `dependency_analyzer.py`, `trace_analyzer.py`,
`change_analyzer.py`, `candidate_generator.py`, `causal_graph.py`,
`root_cause_scorer.py`, `causal_explanation.py`, plus the demo scenarios
`demo_incident.py` / `demo_causal.py`.

### APIs

```text
POST /api/v1/incidents/{id}/analyze                              run or reuse (idempotent)
GET  /api/v1/incidents/{id}/causal-analysis                      latest / specific version
GET  /api/v1/incidents/{id}/causal-analysis/history              versioned audit trail
GET  /api/v1/incidents/{id}/causal-analysis/{aid}/explanation    structured reasons
GET  /api/v1/incidents/{id}/root-causes                          ranked candidates
GET  /api/v1/incidents/{id}/causal-graph                         nodes + edges
GET  /api/v1/incidents/{id}/causal-chain                         validated chain
GET  /api/v1/incidents/{id}/hypotheses                           alternatives + evidence
GET  /api/v1/incidents/{id}/evidence-analysis                    supporting vs contradicting
GET  /api/v1/incidents/{id}/relationships/{rid}/explanation      why this edge exists
```

Isolation is enforced in SQL on every query: the incident is resolved through
the caller's scope, and analyses/evidence are filtered by `project_id` **and**
`incident_id`. Unknown or out-of-scope ids are 404s, never data.

### Workers and caching

Analysis runs on demand through the API (and is callable from a worker); there
is no new queue. Idempotency replaces caching: an unchanged evidence set
returns the stored version instead of recomputing it.

---

## 5. Causal analysis

**Candidate generation.** Candidates originate from evidence only: recent
deployments and configuration changes, observed anomaly components, trace
propagation origins, datastore/external/resource signals. Bounded by
`CAUSAL_MAX_CANDIDATES`; one hypothesis per component, not one per discovery
path. No candidate type is assumed before the evidence is read.

**Temporal analysis.** First/last occurrence, duration, ordering, gaps,
simultaneity, persistence and recovery ordering — all UTC, all aware. `before ≠
caused` is enforced as code: precedence produces a supporting fact, never a
conclusion.

**Dependency analysis.** A bounded structural neighbourhood built from the
Phase 2 knowledge graph plus `component_dependencies`, capped by
`CAUSAL_MAX_DEPENDENCY_HOPS`. "A calls B" establishes a channel, not a cause.

**Trace analysis.** Stored span trees yield the strongest directional evidence:
a failing child span inside a parent span is a stored record of the call that
failed. Propagation order is derived from failure *conclusion* times, because
ordering by span start inverts nested failures (a defect found and fixed here).

**Evidence scoring.** Documented additive components — temporal alignment,
trace support, dependency support, propagation, change relevance, recovery,
resource signal — minus a contradiction penalty, normalised deterministically
inside one analysis.

**Contradiction handling.** A change after onset yields `TEMPORAL_CONTRADICTION`,
reduces confidence, and never produces a causal edge. A dependent that failed
before its dependency is recorded as contradicting evidence on the dependency.

**Confidence.** Separate from score, bucketed `HIGH` / `MEDIUM` / `LOW` /
`INSUFFICIENT` with documented criteria and a contradiction-cap. No probability
is ever emitted.

**Alternative hypotheses.** All candidates are returned ranked with their
supporting, contradicting and neutral evidence and an explicit reason their
confidence differs. There is no "winner" label.

**Declining to answer.** `primary_candidate = UNKNOWN` at `INSUFFICIENT`
confidence is a first-class outcome with `missing_evidence` enumerating what the
engine looked for and did not find.

---

## 6. Frontend

* **RCA page** `/incidents/{id}/causal-analysis` — overall confidence and its
  meaning, the primary candidate with its score components, what evidence is
  missing, the validated causal chain, alternatives with supporting and
  contradicting evidence, and the version history.
* **Causal graph** — zoom, pan, node selection, edge selection; a deterministic
  layered layout; directional hypotheses drawn distinctly from
  `CORRELATES_WITH`, which is labelled co-occurrence and dashed.
* **Evidence inspector** — selecting an edge fetches *why ARGUS believes this
  relationship exists*: the stored quotes plus caveats, including contradictions.
* **Synchronized timeline** — event → node highlighting and node/edge → event
  highlighting, so the graph is an investigation surface, not a picture.
* **History** — every version with what changed: primary, confidence bucket, new
  contradictions.
* **RCA index** `/incidents/rca` — makes the feature reachable per incident.

---

## 7. Testing

| Gate | Command | Result |
| --- | --- | --- |
| Backend suite | `pytest -q` | **761 passed** |
| Frontend unit | `npm test` (vitest) | **55 passed** (30 new) |
| Type check | `npx tsc --noEmit` | clean |
| Lint / format / types (backend) | `ruff check`, `ruff format --check`, `mypy app` | clean (98 modules) |
| Frontend lint | `next lint` | clean (one pre-existing warning) |
| Frontend build | `next build` | succeeds, 21 routes |
| Phase 1 regression | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 regression | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Phase 3 regression | `bash infrastructure/e2e-smoke-phase3.sh` | **103/103** (one check strengthened: the incident page must both decline a root cause and hand off to the RCA view) |
| Phase 4 live gate | `bash infrastructure/e2e-smoke-phase4.sh` | **70/70** (re-runnable; identical counts on repeat runs) |
| Phase 4 DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase4.sh` | **73/73** (migration under a live pool, then an immediate request) |
| Migrations | `alembic upgrade head` / `downgrade -1` on live PostgreSQL 16 | reversible, back at head |

New suites: `test_phase4_engines.py` (50), `test_phase4_api.py` (13),
`test_phase4_demo.py` (11), `lib/__tests__/causal.test.ts` (30).

The gate covers the browser views as well as the API, and creates real state
each run, so it is written to be re-run: it reuses a scratch project for
incidents it must create, never depends on incident list order, and asserts
invariants rather than fixtures.

### Performance

Bounded by configuration on every axis: evidence window
(`CAUSAL_EVIDENCE_WINDOW_SECONDS`), candidate budget, edge budget, trace cap
(`CAUSAL_MAX_TRACES`), dependency hops, evidence cap. A live analysis of the
demo incident completes within the gate's request budget; repeated analyses of
an unchanged evidence set are O(1) — they return the stored version.

---

## 8. Demo

1. **Checkout chain (§48).** From the demo's own stored telemetry — datastore
   latency → service timeouts → caller failures, with span trees carrying the
   direction — the engine derives **PostgreSQL** as the primary candidate at
   `HIGH` confidence and builds `PostgreSQL → Inventory Service → Checkout
   Service`. The root cause is never written anywhere; it is derived.
2. **Counterexample (§49).** A deployment after onset is refused: recorded as a
   temporal contradiction, given no causal edges, and excluded from the primary
   position.
3. **Unknown root cause (§50).** Correlated anomalies without directional
   evidence produce `UNKNOWN` at `INSUFFICIENT`, with the missing evidence named.

---

## 9. Bugs found by live validation

Live validation is what found these; each has a regression test.

1. **A candidate displayed more evidence than it was scored on.** Edge-bound
   evidence rows were returned as the target candidate's own facts: Inventory
   Service reported 20 supporting facts while its confidence had been computed
   from 7. Candidate reads now exclude edge-bound rows, counts are derived from
   the evidence actually persisted, and the live gate asserts the reconciliation.
2. **Propagation order was inverted.** Ordering failures by span *start* made a
   nested child look later than its parent. Ordering now uses failure conclusion
   times.
3. **Change edges fanned out to every candidate** instead of the components the
   change actually touched, inflating graph support.
4. **Anomaly ordering came from the sweep instant, not the telemetry.**
   `detected_at` is when the sweep ran; the true ordering lives in
   `anomaly_observations.observed_at`. Using the sweep instant collapsed
   precedence evidence.
5. **Change candidates received temporal precedence evidence** even though they
   are not components.
6. **The model and the migration disagreed on column names** (and one enum name
   was mistyped), which the first live run surfaced immediately.
7. **A gate that only passed once** — corrected to be self-scoping and
   re-runnable, and confirmed over repeated runs.
8. **The first request after a migration returned 500.** Running the migration
   under a *live* connection pool invalidated asyncpg's cached prepared-statement
   plans, and `InvalidCachedStatementError` surfaced to the caller; the request
   after it succeeded, which is exactly the kind of "works on the second try"
   defect that hides in production. The engine now disables that cache
   (`connect_args={"statement_cache_size": 0}` in `app/core/database.py`) — a
   re-plan per statement in exchange for a correct answer on the first attempt.
   Reproduced deliberately and pinned by the opt-in `DDL_PROBE=1` gate step
   (`== 17`).

---

## 10. Limitations

* **No causal instrumentation.** ARGUS infers from stored telemetry. Even `HIGH`
  is evidence-supported, not instrumented end to end.
* **Traces decide direction; without them direction is a hypothesis**, and the
  confidence ceiling reflects it.
* **Correlation remains heuristic.** `CORRELATES_WITH` is an honest label, never
  a causal claim.
* **Boundaries are configuration.** A window that is too short hides evidence,
  and the analysis reports that through `missing_evidence`.
* **Recovery evidence depends on anomalies resolving.** Where they never
  resolved, recovery contributes nothing and is not invented.
* **Analysis is bounded by design** — candidates, edges and evidence are capped.
* **Causality does not cross project boundaries.** Each project is its own
  causal universe: a dependency on a component that lives in another project is
  outside the scope the engine reads, and the analysis stops at the edge of its
  own system rather than guessing. Many systems are analysed side by side
  without contaminating each other — proven by the gate (`== 16. Several systems
  at once`) — but they are not reasoned about *together*.
* **Re-analysis is idempotent, not incremental.** An unchanged evidence set is
  returned as stored; new evidence produces a full deterministic pass as a new
  version. There is no partial recomputation of only the affected candidates —
  a deliberate trade that keeps versions comparable and auditable.
* **No authentication layer yet** (Phase 0–3 parity): isolation is server-side
  ownership validation on every request.
* **The UI never recomputes causality.** It renders stored rows and does no
  causal arithmetic of its own.

---

## 11. Next phase

```text
Phase 5 — Failure Reproduction Engine
```

Reproduction, automatic debugging, patch generation and application, autonomous
remediation, self-healing and predictive forecasting are **not** implemented.
Phase 4 is *analyze, explain, hypothesize, validate, trace causality* — not
*reproduce, fix, deploy*.
