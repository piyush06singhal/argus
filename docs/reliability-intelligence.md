# Reliability Intelligence & Autonomous Learning

Phase 10 turns the history ARGUS already accumulated into knowledge it can reuse.
Phases 1–9 write evidence — incidents, causal analyses, reproductions, verified
patches, forecasts, remediations and their outcomes. Phase 10 reads those rows,
normalizes them into **experiences**, mines **patterns** from them, validates the
patterns against their own evidence, and answers the question an operator
actually asks:

> Have we seen this before, what did we do, and did it work?

It does **not** learn from telemetry directly, does not train a model, and does
not let a pattern change anything. This document covers the domain, the pipeline,
the guarantees, the API, and the limitations.

---

## 1. The guarantee this layer makes, and the one it refuses to make

```text
"Something is wrong."                     → Phase 3
"Here is the most evidence-supported       → Phase 4
 explanation, with its uncertainty."
"Here is a reproduction that proves it."   → Phase 5
"Here is the code that is suspicious."     → Phase 6
"Here is the smallest defensible fix."     → Phase 7
"Here is the risk, before it happens."     → Phase 8
"Here is a bounded action that may help."  → Phase 9
────────────────────────────────────────────────────────────────
"Here is what history says about this,    → Phase 10
 how often, and how well it held up."       ← this phase
```

Every learned statement carries **its own support**: how many observations, over
what window, from which provenance class, at which confidence bucket, and what
the validation checks said. A pattern with two observations is rendered as a
*weak* pattern with two observations — not as a fact with a decimal place. And
nothing Phase 10 produces is ever executed: `KNOWLEDGE` informs, a
`RECOMMENDATION` advises, a human decides, and Phase 9 remains the only component
that can act.

Three specific things this layer will not do:

1. **Learn from unconfirmed AI output.** An AI root-cause hypothesis is not a
   confirmed root cause. `AI_GENERATED` (and `MOCK`) records are excluded from
   learning by default; the flag exists because a deployment might consciously
   decide otherwise, not because it is a good idea.
2. **Invent an answer.** Retrieval answers only from stored experiences. When
   history cannot answer a question, the answer says so — there is no "probably
   something with latency" fallback.
3. **Widen a boundary on its own.** `INTELLIGENCE_AUTO_ACTIVATE_ENABLED` is off
   by default and, even when on, only *informational* knowledge may be
   activated. Nothing here edits a policy, enables a capability, or crosses a
   project scope.

---

## 2. The domain

Twelve tables, one migration (`b1c2d3e4f5a6`), plus the learned-relationship
tables added by `c2d3e4f5a6b7`. Every table is **derived data**: it records what
ARGUS concluded *from* Phases 0–9 rows, never instead of them. Deleting an
incident cascades the experiences that cite it, so the learning layer can never
become a shadow copy of history the platform has itself forgotten.

| Table | Holds |
| :--- | :--- |
| `reliability_experiences` | one normalized episode: what failed, what was observed, what was done, what happened |
| `learning_events` | completed outcomes worth learning from, with provenance and subject |
| `reliability_knowledge` | the knowledge row: type, scope, confidence, support, evidence, limitations |
| `intelligence_knowledge_versions` | every revision of a knowledge row, so a re-derivation is not a silent overwrite |
| `intelligence_knowledge_reviews` | human decisions (approve, reject, deprecate, supersede) with actor and reason |
| `intelligence_learning_runs` | one run and its report — counts, skips, errors, coverage |
| `intelligence_learning_experiments` | counterfactual comparisons, explicitly labelled observational |
| `intelligence_component_profiles` | per-component, per-window reliability profile |
| `intelligence_recommendations` | evidence-backed advisory items |
| `intelligence_recommendation_outcomes` | what happened after a decision — kept separate from the decision |
| `intelligence_relationships` | learned relationships between components (§23, §24) |
| `intelligence_event_hooks` | the producers that publish learning events |

Two invariants hold across all of them:

* **Provenance or nothing.** A row that cannot name where it came from is not
  written.
* **Timestamps are UTC.** A window query that mixes a naive and an aware value
  is a data-leak class of bug in the time dimension, so it is refused rather than
  coerced.

---

## 3. The pipeline

```text
learning_events                       (published by hooks as outcomes complete)
      ↓  consume (bounded batch, per project)
reliability_experiences               (normalized, evidence-cited, as-of bounded)
      ↓  mine (nine miners, each answering one question)
mined patterns                        (grouped by normalized failure/resolution shape)
      ↓  validate (seven checks against the pattern's own corpus)
knowledge row + version + review      (CANDIDATE → VALIDATING → VALIDATED → ACTIVE)
      ↓  read
dashboard, patterns, recommendations, search, component profiles, relationships
```

### 3.1 Events

`LearningEventType` names the completed outcomes: incident resolved, remediation
completed, patch verified, patch regression detected, forecast confirmed / false
positive / missed, root cause confirmed / rejected, reproduction confirmed /
failed, rollback completed. `LearningEventType` values are only ever *claimed*
once — a run takes a bounded batch and marks what it consumed — so a slow run
cannot double-count an outcome into a sample count.

Events are published by hooks wired into the producers (Phase 3 incident
resolution, Phase 5 reproduction results, Phase 7 patch verification, Phase 8
forecast evaluation, Phase 9 remediation outcomes), not by a periodic scan of
"whatever looks new". A hook that cannot resolve its subject records an
unprocessable event with the reason instead of guessing.

### 3.2 Experiences

`experience_builder` turns the rows an event points at into one normalized
episode: the failure signature (anomaly types, metric behaviour, affected
components, dependency/deployment/resource context, health) and the resolution
signature (action types, verification verdict, rollback, recovery bucket).

Two properties matter:

* **Temporal purity.** Every query is bounded by `as_of`. An experience assembled
  "as of 1 June" sees only what was stored by 1 June — so a later resolution
  cannot retroactively explain an earlier failure, and a backtest is honest.
* **Cited evidence.** The experience stores the ids it was built from. A
  knowledge row derived from it can therefore be traced back to the incident,
  the remediation and the verification that produced it.

### 3.3 Miners

Nine deterministic miners, each answering one question over the corpus:

| Miner | Question |
| :--- | :--- |
| `FailurePatternMiner` | which failures keep happening, in what shape? |
| `RemediationPatternMiner` | which action works for which failure? |
| `RegressionPatternMiner` | which changes made things worse? |
| `DeploymentPatternMiner` | do deployments correlate with episodes? |
| `DependencyPatternMiner` | do upstream/downstream conditions precede failures? |
| `RecoveryPatternMiner` | how long does recovery take, and what shape does it have? |
| `ComponentReliabilityPatternMiner` | which components are chronically unreliable? |
| `PredictivePatternMiner` | which forecasts came true, and which were noise? |
| `RecommendationEffectiveness` | were the recommendations worth following? |

Mining is rule-based and pure: no model, no randomness, no I/O. The same corpus
produces the same patterns in the same order, which is what makes the version
ledger meaningful.

### 3.4 Validation

`knowledge_validation` runs seven checks over the pattern's own entries and
returns a verdict with per-check detail:

| Check | Asks |
| :--- | :--- |
| `sample_size` | are there enough observations for the tier being claimed? |
| `data_quality` | are the entries complete enough to support the claim? |
| `temporal_consistency` | do the observations hold together in time? |
| `stability` | does the pattern appear across adjacent windows? |
| `cross_component_consistency` | does a component-scoped claim survive other components? |
| `support_strength` | what fraction of the compared cases actually support it? |
| `false_discovery` | how many comparisons were made to find this one? |

A failing check does not delete the pattern: it is recorded on the knowledge row
so the UI can show *why* a pattern is only a candidate. Knowledge is never
silently dropped, and never silently promoted.

---

## 4. Reading the API

All endpoints are under `/api/v1` and project-scoped: `project_id` is required on
every read, and a project can only ever see its own knowledge.

```text
GET    /intelligence/health                          is the layer working?
GET    /intelligence/dashboard                       the knowledge overview
GET    /intelligence/metrics                         quality metrics per project

GET    /intelligence/knowledge                       learned patterns, filtered
GET    /intelligence/knowledge/{id}                  one pattern, with its evidence
GET    /intelligence/knowledge/{id}/versions         the revision ledger
POST   /intelligence/knowledge/{id}/review           human decision (approve/reject/…)
GET    /intelligence/patterns, /patterns/{id}        the pattern explorer

GET    /intelligence/experiences, /experiences/{id}  the normalized history
GET    /intelligence/relationships                   learned relationships (§23, §24)
GET    /intelligence/components/{id}/profile         one component's learning profile
GET    /intelligence/remediation-effectiveness[/compare]

GET    /intelligence/recommendations                 advisories
GET    /intelligence/incidents/{id}/recommendations  advisories for one incident
POST   /intelligence/recommendations/{id}/decide     accept or dismiss
POST   /intelligence/recommendations/{id}/outcome    record what happened

POST   /intelligence/learning-runs                   run the pipeline now
GET    /intelligence/learning-runs[/{id}]            run history and reports
POST   /intelligence/sweep                           enqueue the scheduled pass
GET    /intelligence/event-hooks                     the producers wired in
GET    /intelligence/search                          grounded question answering
```

Two responses are worth reading closely:

* `GET /intelligence/metrics` — coverage, provenance mix, confidence
  distribution, staleness and the relationship stage. This is how you find out
  that the layer is *working* rather than merely *answering*.
* `GET /intelligence/search` — an answer with citations, or an explicit
  `no comparable case`. The count of citations is part of the response, so a
  confident-sounding answer with no citation is visibly an empty one.

---

## 5. What the UI shows

The Reliability Intelligence Center (`/intelligence`) is a *learning* view, not a
second incident dashboard: it leads with what has been learned, how well
supported it is, and what remains a candidate. The pattern explorer, the
relationship view, the recommendation queue, the run history, the experience
browser and the search panel each repeat their own framing — that a pattern is an
observation, that a learned relationship is not a dependency, and that an
answered question without a comparable case is an honest answer.

---

## 6. Honest limitations

1. **Learning is per project.** Knowledge does not cross project boundaries,
   even when two projects are the same software. `CROSS_PROJECT` exists as a
   scope for a future phase with a real tenancy story behind it.
2. **Provenance exclusion is a blunt instrument.** Excluding `AI_GENERATED`
   discards a genuinely useful hypothesis, together with the bad ones. The
   alternative — learning from unverified model output — is worse.
3. **Sample floors are defaults, not truths.** `INTELLIGENCE_MIN_SAMPLES_*` are
   configuration, and a floor of three observations is a policy choice, not a
   statistical result.
4. **Recommendations are advice.** An accepted recommendation is a human's
   decision; the outcome is recorded *separately*, because acceptance and
   correctness are different things.
5. **Correlation is not causation, and this layer inherits that.** A relationship
   learned from co-failure says two components failed together; it says nothing
   about direction. The undirected relationship stays undirected.

---

## 7. Related documents

* [`docs/learning-governance.md`](learning-governance.md) — how knowledge is
  validated, reviewed, versioned, aged and (not) activated.
* [`docs/phase-10-report.md`](phase-10-report.md) — the delivery report,
  including the defects live validation caught.
* [`docs/data-model.md`](data-model.md) — the tables, enums and indexes.
* [`docs/roadmap.md`](roadmap.md) — what comes after this phase.
