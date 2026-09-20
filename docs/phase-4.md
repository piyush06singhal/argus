# Phase 4 — Root Cause & Causal Analysis

Phase 4 turns ARGUS from *"something abnormal happened"* into *"here are the
most evidence-supported explanations for why it happened, the chain that
connects the evidence, and how confident we are"* — without ever pretending
that correlation proves causation.

> **Central claim.** Causal analysis in ARGUS produces **evidence-supported
> hypotheses**, ranked and explained. It does not produce proof. Every sentence
> the system emits is traceable to stored rows, and *"insufficient evidence"*
> is a first-class, tested answer.

---

## 1. Architecture

```
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

Modules (all under `apps/api/app/services/`):

| Module | Responsibility |
| --- | --- |
| `causal_analysis_service.py` | orchestrator: loads evidence, runs the pipeline, persists a versioned analysis |
| `temporal_analyzer.py` | deterministic ordering: precedence, overlap, gaps, simultaneity |
| `dependency_analyzer.py` | bounded structural neighbourhood from Phase 2's graph + `component_dependencies` |
| `trace_analyzer.py` | directional evidence from stored span trees; failure propagation order |
| `change_analyzer.py` | deployments/config changes vs onset; `TEMPORAL_CONTRADICTION` |
| `candidate_generator.py` | bounded candidate set; one hypothesis per component, not per discovery path |
| `causal_graph.py` | `CausalGraphBuilder` + `CausalValidator` |
| `root_cause_scorer.py` | documented scoring components and confidence buckets |
| `causal_explanation.py` | structured explanations, edge justifications, chain extraction, provider seam |
| `demo_incident.py` | the checkout scenario (Phase 3 + Phase 4 telemetry) |
| `demo_causal.py` | the counterexample and unknown-root-cause scenarios |

---

## 2. What the engine deliberately keeps apart

ARGUS never collapses these into one number (§2 of the phase spec):

| Concept | Where it lives |
| --- | --- |
| Observed fact | evidence rows bound to a stored record (`source_table` + `source_id`) |
| Correlation | `CausalRelationshipType.CORRELATES_WITH` — distinct, and *refused* when direction evidence exists |
| Temporal association | `CausalEvidenceCategory.TEMPORAL`, labelled "supporting context only" |
| Structural relationship | `dependency_analyzer` output; `structural_support` on an edge — never an edge on its own |
| Causal evidence | `TRACE`/`DEPENDENCY`-backed edges with a direction kind |
| Hypothesis | `RootCauseCandidate` |
| Conclusion | never — ARGUS does not conclude |

Two invariants are enforced in code rather than by convention:

1. **No edge without evidence.** `CausalValidator.validate_edge` rejects any
   relationship with zero supporting facts, and rejects `CORRELATES_WITH`
   when direction evidence exists (and a causal type when it does not).
2. **Exact scope.** `environment_id=None` means *environment-less*, never
   "all environments", in every Phase 4 read (Phase 3 parity).

---

## 3. Temporal analysis

`TemporalAnalyzer` is pure: no database, no clock, no randomness.

* **Facts** are normalized to UTC; naive timestamps are interpreted as UTC
  (storage is UTC), missing timestamps are skipped rather than guessed.
* **Relations** are `PRECEDES` / `FOLLOWS` / `SIMULTANEOUS` / `OVERLAPS`, with
  a signed `gap_seconds`. Simultaneity has an explicit tolerance.
* **Ordering is total and deterministic** — ties break by key, then label, so
  two runs over the same rows produce the same sequence.

Two timestamps matter, and conflating them was a real bug found in live
validation:

* `anomalies.detected_at` is when the *sweep* noticed — every anomaly in one
  sweep shares it;
* `anomaly_observations.observed_at` is when the *telemetry* showed it.

Phase 4 orders by the observation time. Using `detected_at` made every anomaly
look simultaneous and silently destroyed the temporal analysis.

### `before ≠ caused`

Precedence is recorded as precedence. A candidate that degraded first gains a
`TEMPORAL` fact whose quote says exactly that, and whose explanation says
"occurring before does not by itself establish causing". A candidate that
degraded *after* something that depends on it gains a `CONTRADICTING` fact
instead.

### Recovery ordering (§31)

Where anomalies resolved, recovery order is analysed: a supposed cause that
recovered *after* its effects is flagged as a caveat on that candidate rather
than quietly ranked first.

---

## 4. Directional evidence

Structural adjacency is the weakest useful signal — "A calls B" says only that
failure *can* travel along the call. Phase 4 therefore treats these as
genuinely different:

| Source | What it licenses |
| --- | --- |
| **Span tree** (Phase 1) | a failing child span inside a parent span: the parent's call to that component failed → `LIKELY_CAUSE`/`POSSIBLE_CAUSE` child → parent |
| **Propagation order** | failures within one request ordered by when each span *failed* (not when it started) → `DOWNSTREAM_EFFECT` |
| **Dependency precedence** | a provider that failed before its caller → `POSSIBLE_CAUSE` |
| **Change timing** | a change before onset that touched the component or its providers → `POSSIBLE_CAUSE` |
| **Co-occurrence only** | `CORRELATES_WITH`, and nothing stronger |

The propagation rule carries a subtle but decisive detail: ordering by span
**start** time inverts every cascading failure, because a caller's span starts
before the dependency it waits on. Ordering by failure *completion* puts the
innermost failure first — the true propagation direction. This is pinned by a
regression test.

---

## 5. Candidate generation

Candidates originate from stored facts only: trace failures, propagation
origins, observed anomalies, temporally relevant changes.

Two design decisions do the heavy lifting:

* **Identity is the accused, not the discovery path.** A datastore reached
  through a failing span and the same datastore reached through its own anomaly
  are one hypothesis with pooled evidence — not two competitors diluting each
  other. Change events stay separate, because a deployment and the component it
  touched are genuinely different explanations.
* **Contradictions always survive the budget.** When the candidate cap bites,
  temporally contradicting changes are kept first — they are the evidence that
  a tempting explanation is wrong, and hiding them would be lying by omission.

---

## 6. Scoring and confidence

Score and confidence are **separate concepts** (§26). A candidate can rank first
among weak candidates while confidence stays `LOW`.

```
score = W_trace·trace + W_temporal·temporal + W_dependency·dependency
      + W_change·change + W_propagation·propagation + W_recovery·recovery
      + W_resource·resource − contradiction_penalty        (clamped to 0..1)
```

Every component is stored on the candidate (`score_breakdown`) and rendered in
the UI, so no number is a black box.

Confidence buckets, with criteria enforced by the scorer:

| Bucket | Criteria |
| --- | --- |
| `HIGH` | ≥3 *independent* evidence categories align, including trace direction, and contradiction is not material |
| `MEDIUM` | ≥2 categories align, but direct causal evidence is incomplete |
| `LOW` | only temporal/structural evidence exists |
| `INSUFFICIENT` | evidence is contradictory, too sparse, or unusable |

Two calibration rules exist because a naive scorer was wrong:

* One strong temporal fact is exactly the spec's definition of `LOW`, so the
  `LOW` floor sits *below* that weight (0.10) rather than above it.
* **Contradictions cap the bucket.** A candidate whose contradicting weight is
  ≥50 % of its total is capped at `LOW`, ≥25 % at `MEDIUM` — corroboration on
  one axis does not license ignoring refutation on another. The cap is stated
  in the confidence reason.

### Choosing a primary — or declining

The primary candidate is selected only when its score clears
`CAUSAL_PRIMARY_MIN_SCORE` **and** its confidence is `MEDIUM` or `HIGH`.
Otherwise `primary_candidate_id` stays `NULL` and the analysis says:

> Insufficient evidence to determine a root cause. …

`missing_evidence` enumerates what the engine looked for and did not find
(spans, a change before onset, timestamps, any evidence-backed relationship).

---

## 7. Causal graph, chains and validation

`CausalGraphBuilder` produces the incident's graph; `CausalValidator` guards it:

* every edge names its evidence (`supporting_evidence_count`, and the evidence
  rows themselves via `relationship_id`);
* an effect that precedes its cause by more than the tolerance is rejected as a
  `TEMPORAL_CONTRADICTION`;
* duplicate candidate edges are merged **before** the edge budget is applied,
  so the cap bounds the stored graph without dropping relationships other
  lenses found;
* change edges connect only to the component the change touched and that
  component's callers — a deployment of checkout is a hypothesis about checkout,
  not about every component in the incident.

The chain endpoint walks directional edges out of the primary hypothesis and
validates every link. `CORRELATES_WITH` never appears in a chain (a chain
threaded through co-occurrence would imply direction nobody observed). A chain
containing a negative temporal alignment is returned as `valid: false` with its
reasons.

---

## 8. Explanation and the optional AI layer

`CausalExplanationService` builds structured explanations from stored rows:
reasons per candidate, supporting/contradicting quotes, score breakdown,
uncertainty, alternatives and explicit limitations.

`RootCauseExplanationProvider` is the seam for an optional interpretation layer.
The default is `DeterministicExplanationProvider`; `NullExplanationProvider`
exists so a deployment can *say* no interpretation layer is configured.
Whatever provider is used, the deterministic engine stays authoritative: a
provider may only re-word what is already established, and may not invent
facts, add edges, change scores, or soften a contradiction (§38).

---

## 9. API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/incidents/{id}/analyze` | run (or reuse) an analysis — idempotent |
| `GET` | `/api/v1/incidents/{id}/causal-analysis` | latest (or a specific version) analysis |
| `GET` | `/api/v1/incidents/{id}/causal-analysis/history` | versioned audit trail with diffs |
| `GET` | `/api/v1/incidents/{id}/causal-analysis/{aid}/explanation` | structured reasons + limitations |
| `GET` | `/api/v1/incidents/{id}/root-causes` | ranked hypotheses |
| `GET` | `/api/v1/incidents/{id}/causal-graph` | nodes + edges for rendering |
| `GET` | `/api/v1/incidents/{id}/causal-chain` | the validated causal chain |
| `GET` | `/api/v1/incidents/{id}/hypotheses` | alternatives with evidence splits |
| `GET` | `/api/v1/incidents/{id}/evidence-analysis` | supporting vs contradicting, per candidate |
| `GET` | `/api/v1/incidents/{id}/relationships/{rid}/explanation` | *why does ARGUS believe this edge?* |

**Idempotency (§34).** `analyze` hashes the bounded input set (incident scope,
engine version, every usable anomaly fingerprint and observation time). If the
newest completed analysis has the same fingerprint, it is returned with
`reused: true`. New evidence appends a new version; history is never
overwritten (`force=true` forces a re-run).

**Isolation (§45).** Every query filters by `project_id` *and* `incident_id`;
analyses are additionally checked against the incident's project. An
out-of-scope or unknown id is a 404 — never data, and never a hint that the row
exists elsewhere.

---

## 10. Frontend — the investigation workspace (§39–§43)

An engineer can go from incident to defensible hypothesis without leaving ARGUS.

The workspace is deliberately two-layered:

* a **server-rendered summary page** at `/incidents/{id}/causal-analysis` — the
  primary candidate, its score components, the causal chain, alternatives, and
  the version history. It renders from the same API the gate exercises, so what
  a person reads is what the engine stored;
* a **client investigation panel** where the graph, the evidence inspector and
  the timeline share one selection.

| File | Role |
| --- | --- |
| `app/incidents/[id]/causal-analysis/page.tsx` | primary candidate, score breakdown, chain, alternatives, history, missing evidence |
| `app/incidents/[id]/causal-analysis/CausalInvestigation.tsx` | causal graph + evidence inspector + synchronized timeline |
| `app/incidents/[id]/causal-analysis/AnalyzeButton.tsx` | run / re-run the analysis (idempotent by default) |
| `app/incidents/rca/page.tsx` | index: incidents, linking into each analysis |
| `lib/causal.ts` | presentation rules, deterministic graph layout, chain rendering |

### The causal graph (§40)

Nodes are candidates, labelled by component name and carrying their confidence
and evidence counts. Columns follow causal depth: a node sits one column right
of whatever it explains. Rows are ordered by score. The layout is a pure
function, so the same analysis always draws the same picture.

Edges are **not visually identical**, because they do not mean the same thing:

* a directional hypothesis is drawn as a solid arrow, width by confidence;
* `CORRELATES_WITH` is drawn dashed and muted, and is labelled co-occurrence;
* a wide transparent hit area sits over each line, because a 2px line is not a
  click target.

### The evidence inspector (§41)

Selecting an edge asks the API *why this relationship exists* and shows the
stored quotes that justify it plus any caveats (a negative time offset is
labelled a temporal contradiction, a correlation says outright that no
direction was observed). Selecting a node shows the candidate's own facts and
its score components.

Edge-bound evidence rows are excluded from a candidate's own evidence list —
the same rule the backend enforces — so the counts displayed next to a
candidate always describe exactly the rows shown underneath them.

### Timeline synchronization (§42)

Selecting a timeline event highlights the graph node for that component;
selecting a node or an edge highlights the timeline entries behind it. This is
an investigation workflow rather than a static picture.

### History (§43)

The history table shows every stored version, newest first, with what changed:
a changed primary, a changed confidence bucket, new contradictions. A re-run
appends a version; nothing is overwritten.

---

## 11. Deterministic scenarios

### 11.1 The checkout chain (§48)

`seed_checkout_incident` (extended in Phase 4) ingests, anchored to the
detection instant `T`:

```
T-4m ..    baseline telemetry for checkout, inventory, datastore
T-120s     deployment published (context)
T-100s     datastore query p95 18ms → 640ms      ← first degradation
T-95s      datastore query timeouts logged
T-90s      configuration change recorded
T-80s      inventory p95 150ms → 620ms
T-75s      inventory read timeouts logged
T-50s      checkout p95 220ms → 890ms
T-30s      checkout error rate 0.8% → 7.2%
T-25s      checkout health HEALTHY → DEGRADED
T-20s..    six failing requests, each with a 3-span tree:
             checkout (root) → inventory → datastore
```

The engine is never told which component is the root. Live result on the
seeded stack:

| Candidate | Type | Confidence | Score |
| --- | --- | --- | --- |
| **PostgreSQL** | `DATABASE` | **HIGH** | 0.580 |
| Checkout Service | `APPLICATION_COMPONENT` | HIGH | 0.290 |
| Inventory Service | `DEPENDENCY_FAILURE` | LOW | 0.266 |
| Deployment (checkout) | `DEPLOYMENT` | LOW | 0.143 |
| Configuration change | `CONFIGURATION_CHANGE` | INSUFFICIENT | 0.000 |

with the chain `PostgreSQL → Inventory Service → Checkout Service`, `valid: true`,
and every edge carrying TRACE + DEPENDENCY evidence at HIGH confidence.

### 11.2 The counterexample (§49)

`seed_counterexample_scenario`: checkout errors begin at `T-4m`; the deployment
lands at `T-30s` — after onset, close enough to tempt a "recent change" heuristic.

Result: the deployment is **not** primary. It carries a `CONTRADICTING` fact —

> Deployment deploy-checkout-late (version 2099.1.0) occurred 150s AFTER
> incident onset; it cannot explain the initial degradation

— scores 0.00 at `INSUFFICIENT`, and no causal edge is created from it.

### 11.3 Unknown root cause (§50)

`seed_unknown_scenario`: three unrelated peers degrade within seconds of each
other, with no spans, no dependency path between them and no change nearby.

Result: `primary_candidate_id = NULL`, `overall_confidence = INSUFFICIENT`, the
summary says *"Insufficient evidence to determine a root cause"*, and
`missing_evidence` lists the span-level traces, the change, and the absent
relationships. The candidates are still returned, ranked — just not crowned.

---

## 12. Database

New tables (migration `f1a2b3c4d5e6`, down-revision `e0f1a2b3c4d5`, and
reversible):

| Table | Purpose | Notable indexes |
| --- | --- | --- |
| `causal_analyses` | one versioned run per incident | unique `(incident_id, analysis_version)`, `project_id` |
| `root_cause_candidates` | hypotheses, score and confidence separate | `(analysis_id, score)`, `component_id` |
| `causal_evidence` | facts bound to a candidate (and optionally an edge) | `(source_table, source_id)`, candidate+polarity |
| `causal_relationships` | the per-incident causal graph's directed edges | unique `(source, target, relationship_type)` |

Seven Postgres enum types are created and dropped with the migration. No
existing table is modified: Phase 4 reads Phase 0–3 data and owns only its own
four tables.

---

## 13. Testing

```
tests/test_phase4_engines.py   50 tests   analyzers, candidates, graph, chains, scoring,
                                          evidence hygiene (duplicates, absences)
tests/test_phase4_api.py       13 tests   endpoints, idempotency, isolation, honesty
tests/test_phase4_demo.py      11 tests   the three scenarios end to end
lib/__tests__/causal.test.ts   30 tests   layout, semantics, wording, invariants (vitest)
```

The catalogue in §47 is covered case by case: temporal ordering / overlap / gaps /
simultaneity / out-of-order / naive timestamps; upstream, downstream, shared and
cyclic dependencies; nested-failure ordering, partial traces (no end time) and
failing traces with no spans at all; component, change, datastore and bounded
candidate generation; duplicate evidence collapsing to one fact while two rows
with identical text stay two facts; un-anchored candidates never invented; and
chain selection when several chains are possible — strongest confidence first,
bounded in length, never walked twice, and never threaded through a correlation.

The scenario suite includes the invariant that cost the most to get right: a
candidate's stored counts must equal the evidence it can be shown
(`TestEvidenceAttributionConsistency`). Edge evidence carries a `candidate_id`
because the schema requires an owner; returning it as the candidate's own
evidence made a candidate display 20 facts while its confidence had been chosen
from 7. Candidate reads now exclude edge-bound rows, and the live gate asserts
the reconciliation for every candidate.

Phase 3's scenario tests were updated where the scenario legitimately grew (the
rule count is now asserted against the scenario definition rather than a magic
number).

The live gate (`infrastructure/e2e-smoke-phase4.sh`) covers the browser views
too: it asserts the RCA page renders the derived primary candidate, the graph,
the validated chain, the alternatives, the history, the proof disclaimer — and
that an incident with no analysis yet says so instead of erroring.

---

## 14. Limitations — stated, not hidden

* **No causal instrumentation.** ARGUS infers from stored telemetry. Even a
  `HIGH` candidate is evidence-supported, not instrumented end to end.
* **Traces decide direction; without them, direction is a hypothesis.** With no
  span trees, the engine falls back to temporal + structural evidence, and the
  confidence ceiling reflects that.
* **Correlation remains heuristic.** `CORRELATES_WITH` is an honest label, not a
  causal claim.
* **Boundaries are configuration.** Evidence windows, candidate budget, trace
  cap, dependency hops, change proximity and the primary-selection floor are all
  settings; a window that is too short hides evidence (and the analysis says so
  through `missing_evidence`).
* **Recovery evidence depends on anomalies resolving.** Where they never
  resolved, recovery ordering contributes nothing and is not invented.
* **Analysis is bounded by design** — a hypothesis nobody can read is not a
  hypothesis, so candidates, edges and evidence are all capped.
* **The UI never recomputes causality.** It renders stored rows: scores,
  confidences, chains and caveats all come from the server, and the client does
  no causal arithmetic. Bulk confidence summaries are deliberately absent — a
  confidence bucket shown without its evidence is exactly the kind of conclusion
  this phase declines to hand out.
* **No authentication layer yet** (Phase 0–3 parity): isolation is enforced by
  server-side ownership validation on every request.

---

## 15. Operating several systems at once

ARGUS holds many projects (and many environments inside them) in one database,
and each is analysed independently. What that means in practice:

* **Isolation is enforced in SQL, not in application filters.** Every causal
  query resolves the incident through the caller's scope and then filters
  analyses, candidates, evidence and edges by `project_id` **and**
  `incident_id`; an out-of-scope id is a 404, never data.
* **No evidence crosses a project boundary.** Analysis reads anomalies, traces,
  spans, changes and graph context *for the incident's own project and
  environment*. A second system's evidence-free incident returns `UNKNOWN` even
  while another project in the same database has a fully-explained incident at
  `HIGH` confidence.
* **Every candidate belongs to the system being analysed.** Candidates are
  built from that project's components; a candidate pointing at another
  project's component is not something the engine can produce.
* **Concurrency is safe by construction.** Analyses are per-incident rows with
  a unique `(incident_id, analysis_version)`; one system's run cannot alter
  another's conclusion, and re-analysing one incident never touches a second.

The live gate proves all four (`== 16. Several systems at once must not
contaminate each other`), because "it should be isolated" is not evidence.

### What is *not* supported: causality across systems

Each project is its own causal universe. If system A in one project calls a
service that lives in another project, ARGUS will **not** connect the two: the
dependency graph, trace analysis and change analysis all operate inside the
requesting project's scope. Cross-system causality would require a shared
dependency graph across projects and a deliberate trust decision about mixing
tenants' telemetry — neither exists, and guessing at it would break the
isolation guarantee that makes the multi-project story useful in the first
place. Where a real dependency crosses the boundary, the honest result is an
analysis that stops at the edge of its own system and, if direction is missing,
`INSUFFICIENT`.

### Re-analysis: idempotent, not incremental

* An unchanged evidence set is **not recomputed**: the stored analysis is
  returned (`reused: true`), so repeated triggers cost nothing.
* New evidence changes the input fingerprint and produces a **new version**,
  preserving the previous one and recording what changed (§35, §43).
* What is *not* implemented is a partial recomputation that updates only the
  affected candidates and scores. Each version is a full, deterministic pass
  over a bounded input — which is what makes two versions comparable and a
  conclusion reproducible. It is a deliberate trade: bounded full passes over
  per-incident windows, rather than a delta scheme whose intermediate states
  nobody could audit.

---

## 16. Phase 5

Phase 4 ends at *analyze, explain, hypothesize, validate, trace causality*.

```text
Phase 5 — Failure Reproduction Engine
```

Reproduction, automatic debugging, patch generation, remediation and
self-healing are explicitly **not** implemented here.
