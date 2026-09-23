# Phase 10 Implementation Report — Reliability Intelligence & Autonomous Learning

**Status: complete — implemented, tested, documented, validated live.**

Phase 10 makes ARGUS learn from its own outcomes: it turns completed episodes into
normalized experiences, mines patterns from them, validates each pattern against
its own evidence, and answers *"have we seen this before, what did we do, and did
it work?"* — while refusing to train a model, change a policy, execute anything,
or claim more certainty than the evidence holds.

---

## 1. Executive summary

ARGUS could observe, detect, correlate, explain, reproduce, locate a fault,
generate and verify a fix, forecast risk, and act through a closed registry. What
it could not do was *remember*. Phase 10 adds the layer that closes the loop:

* A **learning domain** — experiences, events, knowledge with versions and
  reviews, runs, experiments, component profiles, recommendations with separate
  outcomes, learned relationships and event hooks, across twelve tables and three
  migrations.
* **Learning events published by the producers themselves**: incident resolution,
  reproduction results, patch verification, forecast evaluation and remediation
  outcomes each publish an event with provenance, a dedup key and an
  unprocessable reason when the subject cannot be resolved.
* **Normalized experiences** with temporal purity (every query bounded by an
  `as-of` cutoff), a structured failure signature, a resolution signature, and the
  ids the episode was built from.
* **Nine deterministic miners** (failure, remediation, regression, deployment,
  dependency, recovery, component reliability, predictive, recommendation
  effectiveness) — pure functions, no model, same corpus → same patterns.
* **Validation against each pattern's own corpus**: seven checks with per-check
  detail, recorded on the row; a failing pattern becomes a candidate with a
  reason rather than being silently dropped.
* **A governance lifecycle** (`CANDIDATE → VALIDATING → VALIDATED → ACTIVE`) with a
  version ledger, human reviews that can only lower belief, autonomous activation
  off by default and limited to informational high-confidence patterns, and
  ageing-by-deprecation instead of deletion.
* **Learned relationships** stored in their own table with their support and an
  explicit direction, never written into `component_dependencies` or `graph_edges`.
* **Grounded search** that answers with citations or states that history has no
  comparable case — and a **recommendation** surface where acceptance and
  correctness are two different facts.
* **An API surface** of 28 endpoints, a scheduled sweep (staleness, expiry,
  learning runs), retention for the event log, and a **UI** — reliability
  intelligence center, pattern explorer with review controls, learned
  relationships, recommendation queue, experience browser, run history, component
  learning profiles and grounded search.

## 2. Architecture

Phase 10 adds no new infrastructure. It reuses PostgreSQL, the worker, the sweep
idiom, `project_id` scope enforcement and the retention sweeper.

```text
Phase 1–9 outcomes (incidents, patches, forecasts, remediations, reproductions)
        ↓  published by the producers (learning_hooks)
learning_events ──► claimed once by a run (bounded batch, dedup key)
        ↓
experience_builder ──► reliability_experiences   (as-of bounded, evidence-cited)
        ↓
pattern_miners (9) ──► mined patterns
        ↓
knowledge_validation (7 checks, per-pattern corpus)
        ↓
reliability_knowledge + version + review        (CANDIDATE → … → ACTIVE)
        ↓
profiles · learned relationships · grounded search · recommendations
        ↓
a human decides                          (nothing here executes)
```

Two boundaries are structural rather than conventional:

* **Learning reads, never writes, the domains it learns from.** Nothing in
  `app/services/intelligence_*`, `learning_*`, `pattern_miners`,
  `recommendation_engine` or `relationship_builder` writes to incidents,
  deployments, patches, forecasts, remediation or graph tables.
* **Nothing learned can act.** There is no code path from a knowledge row to a
  policy, an action, a limit or an execution; the strongest effect available is a
  recommendation a person accepts or dismisses.

## 3. Testing

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend suite | `pytest -q` | **1746 passed, 1 skipped** |
| Phase 10 backend tests | the twelve `test_phase10_*.py` files | **250 collected** |
| Lint | `ruff check app tests` | clean |
| Format | `ruff format --check app tests` | clean |
| Types | `mypy app` | clean (199 modules) |
| Frontend tests | `vitest run` | **203 passed** (10 files) |
| Frontend type check | `tsc --noEmit` | clean |
| Frontend build | `next build` | succeeds, 12 `/intelligence` routes |
| Phase 10 live gate | `bash infrastructure/e2e-smoke-phase10.sh` | **87/87** |
| Phase 10 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase10.sh` | **89/89** (both Phase 10 revisions reverse and re-apply) |
| Phases 1–9 live gates | `bash infrastructure/e2e-smoke-phase{1,3,5,8,9}.sh` | **46 · 103 · 104 · 42 · 69** (all re-run against this revision) |

Phase 10 test areas, by file:

| File | Tests | Covers |
| :--- | :--- | :--- |
| `test_phase10_api.py` | 38 | Scope, shape, status codes, the full read surface over HTTP |
| `test_phase10_knowledge.py` | 33 | Lifecycle, versions, review decisions, staleness, ceilings |
| `test_phase10_grounding.py` | 25 | Grounded answers, citations, the honest "no comparable case" |
| `test_phase10_patterns.py` | 24 | The miners, labels, observational framing, component scope |
| `test_phase10_security.py` | 24 | Provenance exclusion, cross-project isolation, poisoning defence |
| `test_phase10_recommendations.py` | 22 | Generation, evidence citation, decisions, separate outcomes |
| `test_phase10_similarity.py` | 21 | Retrieval, thresholds, "not its own historical match" |
| `test_phase10_relationships.py` | 19 | Support, direction, undirected stays undirected, never a dependency |
| `test_phase10_experiences.py` | 16 | Normalization, `as-of` purity, evidence ids |
| `test_phase10_learning_run.py` | 13 | Run reports, idempotency, eligible projects, event claiming |
| `test_phase10_events.py` | 9 | Event publication, dedup, subject resolution, unprocessable reasons |
| `test_phase10_demo.py` | 6 | The §93–§98 scenarios end to end |

### Defects found by validation and fixed

1. **Two concurrent correlation passes could open two live incidents for the same
   fingerprint.** Correlation runs from several places at once — the detect
   endpoint, the async ingest hook, the sweep — and each pass looked up an
   incident by fingerprint and inserted one when it found nothing. Two passes in
   flight at the same time cannot see each other's *uncommitted* insert, so both
   created a row: the Phase 10 gate produced two incidents with the identical
   fingerprint, one of them holding no anomalies at all (the other pass had
   claimed them), and the empty one then answered `409` on every lifecycle
   transition. The invariant is now the database's job — a partial unique index on
   `(project_id, fingerprint)` for *unresolved* incidents, with a migration — and
   the persistence path opens a savepoint, adopts the row the other pass wrote and
   continues as an update. A `POST /incidents` that names an already-live
   fingerprint is refused with `409` and a reason instead of a 500. Verified live:
   four concurrent detect+correlate passes on one scope now leave exactly one
   incident, with no duplicate fingerprint and no evidence-less row.
2. **A learning run's summary lost the run id when the run failed.** The failure
   path recorded the error but not the identifier, so the run history could not
   link the report to the row it described — and the gate's own audit check
   silently skipped. The id is set before the work starts.
3. **`eligible_projects` did not do what it said.** It was documented as the set
   of projects with pending work and returned every active project, so a sweep
   could run a full pass over projects with nothing to learn — the sweep now
   filters on pending events.
4. **The rejected-pattern re-open gate was unreachable.** "A human said no;
   re-mining must not overturn that unless the evidence has substantially grown"
   was implemented in a branch that could never be taken, because the status
   transition was validated first and rejected it — so a rejected pattern could
   be re-created as a fresh candidate by a corpus that had barely moved. The gate
   now runs before the create path, where it can actually fire; a rejected row is
   still never resurrected (the state machine allows it only `SUPERSEDED`), and
   the re-mined evidence becomes a new candidate that has to earn its own way.
5. **`verify_citations` broke when called directly.** The helper assumed it was
   always invoked inside a retrieval that had already filtered its corpus, so a
   direct call could cite a row the caller was not allowed to see.
6. **The relationship builder compared naive and aware datetimes.** SQLite returns
   naive timestamps, so a relationship's coverage window crashed on the test
   backend and would have compared an aware value against a naive one on
   PostgreSQL with a different session setting — a timezone-dependent bug in the
   one place that decides whether two episodes happened close together.
7. **Chronic-component knowledge was always one run late.** Mining ran before the
   component profiles it depends on, so the signal was computed from the previous
   run's profile. The stage order is now explicit.
8. **A phase-5 flake hid a real evidence-loss window.** The capture path waited
   for a sandbox to settle using two signals — the services' own in-flight count
   and telemetry file stability — but only ran the stability check when the drain
   was *observed*. Under load a probe can fail for the whole budget, and the
   clock-only exit captured a half-written file: the exact silent loss that wait
   exists to prevent. The stability check now always runs, gets its own floor so
   the probe phase cannot starve it, and waits for telemetry that has not appeared
   yet instead of treating "nothing on disk" as "settled". Two tests pin it, and
   the observed flake (2 failures in 4 full-suite runs) has not recurred in the
   runs since.
9. **The Phase 10 gate could pass without testing its own subject.** Three
   branches recorded a *pass* when nothing was produced — "no recommendation was
   produced for this incident (acceptable: it is advisory)", "no open incident
   remained to advise on (skipped)", and a missing run id skipping the run-audit
   check. A gate whose step 8 silently succeeds when the recommendation engine
   returns nothing is worse than no gate: the check count varied between runs
   (87 vs 85 on identical code), which is how it was noticed. Each branch now
   fails with the payload that produced it.

Item 1 is the one worth dwelling on. It was invisible to 1700 unit tests because
it needs two correlations genuinely in flight, and it was invisible to the *first*
live run because the race only sometimes lands — the gate that finally caught it
was the phase that consumes incidents rather than the phase that creates them.

## 4. Demo

`bash infrastructure/e2e-smoke-phase10.sh` drives the whole pipeline over the real
HTTP API against the compose stack, and is self-cleaning:

* an isolated scratch project, so no other phase's gate can be perturbed;
* four ingested multi-component episodes correlated into four incidents — three
  completed and one left open, which is exactly the floor the learner needs and
  the boundary it must respect;
* run 1 consuming the outcomes: real counts from the run's own report, no
  unprocessable events, exactly the three completed episodes normalized;
* run 2 proving idempotency: no duplicate experience, no inflated sample count;
* knowledge: pattern explorer, detail with its algorithm and limitations, the
  version ledger, a review decision refused without a reason, an activation
  refused because the pattern is a candidate, and a rejection recorded with its
  reviewer and reason;
* grounded search: an answer with citations, and an honest "no comparable
  historical case" for a question the history cannot answer;
* recommendations: generated for the live incident, refused for an empty actor,
  accepted by a named operator, then given an *ineffective* outcome — recorded
  separately from the decision;
* learned relationships carrying their support and repeating that they are not
  dependencies;
* two projects side by side, with the second seeing none of the first's learning;
* the twelve tables, seven enum types and the relationship uniqueness index
  asserted in PostgreSQL, with the migrations reversed and re-applied under
  `DDL_PROBE=1`;
* the learning workspace rendering, including its framing copy;
* cleanup, including that nothing learned outlives the project it learned from.

## 5. Limitations

* **Learning is per project and per outcome.** Knowledge does not cross project
  boundaries, and an episode with no recorded outcome teaches nothing.
* **Provenance exclusion is blunt.** Excluding `AI_GENERATED` and `MOCK` by
  default keeps unconfirmed model output out of the corpus and discards the
  hypotheses that were right along with the ones that were wrong.
* **Sample floors are policy.** Three observations before a pattern may leave
  `CANDIDATE` is a configured threshold, not a statistical result; there is no
  false-discovery-rate control beyond the recorded comparison count.
* **A learned relationship is not a dependency.** It records that two components
  failed together; with no directional evidence the relationship stays
  undirected, and it never writes to the declared dependency or graph tables.
* **The failure identity includes the correlation time bucket.** A pattern's
  identity is derived from the incident's dedup fingerprint, which is bucketed by
  the hour of detection. The effect is that evidence accumulates fastest for
  recurrences detected inside the same bucket; the miners that do not depend on
  the bucket (chronic component, dependency and deployment patterns) are
  unaffected. This is the first thing to revisit in a follow-up phase.
* **Nothing learned ages out of the tables.** Knowledge is deprecated, superseded
  and versioned, never deleted — by design — while the event log is swept like
  every other evidence table. A deployment with an extreme learning volume should
  watch the knowledge tables' growth.

## 6. Next phase

The roadmap's Phase 11 is product surface — a reliability command center across
projects, orgs and teams — rather than a new capability. The boundary Phase 10
sets is that learning concludes and advises; nothing it produces changes a policy,
executes an action or crosses a scope without a person.

See [`docs/reliability-intelligence.md`](reliability-intelligence.md) for the
design and [`docs/learning-governance.md`](learning-governance.md) for how
knowledge is validated, reviewed, versioned and aged.
