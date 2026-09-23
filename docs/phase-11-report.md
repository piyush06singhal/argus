# ARGUS — Phase 11 Delivery Report

**Unified Reliability Intelligence Platform & Autonomous Engineering Control Plane**

Status: **shipped**. Every gate below was run against the live Docker Compose
stack in this delivery, and every number is reproducible from a clean checkout.

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend suite | `cd apps/api && pytest -q` | **1912 passed, 1 skipped** |
| Lint / format / types | `cd apps/api && ruff check app tests && ruff format --check app tests && mypy app` | clean (224 modules) |
| Frontend suite | `cd apps/web && npm test` | **231 passed** |
| Frontend types / lint | `cd apps/web && npx tsc --noEmit && npx next lint --dir app/platform` | clean |
| Frontend build | `cd apps/web && npm run build` | succeeds, 14 platform routes |
| Phase 11 live gate | `bash infrastructure/e2e-smoke-phase11.sh` | **90/90**, five consecutive runs |
| Phase 11 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase11.sh` | **91/91** (the Phase 11 revision reverses and re-applies) |
| Phase 0/1 live gate | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 3 live gate | `bash infrastructure/e2e-smoke-phase3.sh` | **103/103**, twice |

---

## 1. Executive summary

Phases 0–10 each added a capability and each owned its own tables, endpoints and
vocabulary. What none of them owned was the question an operator actually has at
07:00 on a bad morning: **what is the state of my system, what is being done about
it, and who decided that?**

Phase 11 answers that without adding an eleventh subsystem. It adds a *control
plane*: one derived system state, one operational object (the **Reliability Case**)
that references what the other phases concluded rather than copying it, one
workflow engine that drives a case from detection to learning, one dashboard, one
search, one governance and audit surface, and one health model that includes
ARGUS itself.

Three properties were treated as non-negotiable:

1. **Derived, never duplicated.** System state and dashboard sections are computed
   from Phase 0–10 rows at read time. Nothing in Phase 11 becomes a second source
   of truth that can drift from the incident, forecast, remediation or knowledge it
   describes.
2. **A claim carries its basis.** Every component state ships with
   `state_evidence`; every report and postmortem ships a `limitations` list; every
   search response says what it filtered by; every configuration version records
   who changed it and why.
3. **Phase 9 remains authoritative.** Phase 11 orchestrates and observes. It does
   not execute remediation, does not raise a policy ceiling, does not self-authorize,
   and has no code path to a shell, a credential or a deployment. Its own overview
   endpoint states that boundary in the payload.

## 2. Architecture — the unified control plane

```text
        Phase 0–10 rows (the only source of truth)
                 │
   ┌─────────────┴──────────────┐
   │                            │
System state (derived)      Platform events (§10)
   │                            │
   │                     event correlation → case timeline
   │                            │
   └────────────┬───────────────┘
                ▼
        Reliability Case  ──►  Workflow run (10 stages)
                │                    │
                │                    ├─ evidence from the phases that own it
                │                    ├─ Phase 4 diagnosis / Phase 8 prediction
                │                    ├─ Phase 9 remediation (its gates decide)
                │                    └─ Phase 10 learning (on completion)
                ▼
   overview · catalog · SLO · change intelligence · search · data quality
   governance · audit · notifications · health · reports · postmortems
```

The control plane is a *reader and an orchestrator*, not a writer of facts. Its own
tables record only what it decided: cases, timelines, workflow runs, platform
events, SLOs and budgets, notifications, data-quality issues and configuration
versions.

## 3. System state (§2–§5)

`build_system_state` composes one scope's state from stored evidence: components
with a state and a reason, active anomalies, open incidents, recent changes,
forecasts, remediations and dependencies. Precedence is explicit and tested as a
table — an open incident outranks a degraded forecast, a resolved incident cannot
be reported as healthy, and a component with *no* evidence is `UNKNOWN`, never
`HEALTHY`. Every section is bounded by configuration, and every component carries
`state_evidence`, so the dashboard can show why rather than only what.

State changes are recorded as transitions (`component_state_transitions`) with the
trigger that caused them, which is what makes the timeline and the "improvement
plan" possible without storing a second history.

## 4. Reliability cases (§14–§18)

A case is the object a person works on. It deliberately is **not** a second
incident, **not** a copy of the evidence, and **not** the workflow:

* incidents stay authoritative; a case points at one and never edits it;
* evidence is assembled by querying the phases that own the rows at request time;
* the workflow engine drives the sequence; the case is the durable record.

Status changes go through one legal-transition table shared by the API and the UI
(illegal moves are `409` with the legal set named). The timeline is a story:
replaying an event appends nothing (dedup keys), and every entry records its source
and whether it was a system action.

## 5. Workflow engine (§11–§13)

A run moves through `DETECTED → TRIAGED → ANALYZING → DIAGNOSED →
REMEDIATION_READY → AUTHORIZED → EXECUTING → VERIFYING → RESOLVED → LEARNED`, and
progression is **evidence-driven, not time-driven**: the sweep only advances a run
to a stage whose precondition stored rows actually satisfy. Preconditions are
checked before every advance, a stop is recorded with its reason, and a terminal
run cannot be resurrected (a real defect the suite caught: `check_preconditions`
returned "all clear" for a finished run, which `advance` read as permission).

## 6. SLOs and error budgets (§32–§35)

An objective names its indicator, its **metric**, its unit and which side of the
target is good (`AT_LEAST` / `AT_MOST`) — an objective that names no metric is
refused. Targets are bounded per indicator family: a ratio target lives in `0..1`
while a latency target is a magnitude, because a single `0..1` bound made a
millisecond latency objective impossible to express. Evaluation records
compliance, sample count, data quality and burn state per window; a never-evaluated
objective is `UNKNOWN`, never "meeting". Burn thresholds are configuration, so the
API, the sweep and the UI cannot disagree about what `FAST_BURN` means.

## 7. Governance (§91–§94, §100)

* **Authorization** stays exactly where Phase 9 put it: validation → safety →
  policy → approval/authority → execution-time re-check. Phase 11 adds no bypass,
  and the workflow's `AUTHORIZED` stage is reached only through those gates.
* **Audit** is the union of Phase 9's hash-chained action audit, the case timeline
  and the platform event stream. Deleting a project deletes what it recorded; the
  gate asserts that, because history that outlives its subject is a leak.
* **Configuration** is versioned. A write names its scope and its reason; only the
  scopes the platform owns are writable; the ledger is append-only; and a rollback
  is a *new* version restoring older content (`restored_from` / `new_version`), so
  the record of what was configured and when is never rewritten.
* **Secrets never round-trip**: redacted fields are named in the response and
  excluded from the payload (§92).

## 8. Search (§17, §18)

One parser, one response shape: grouped results by kind, with the filters the
engine applied returned on the response, an explicit no-match answer, bounded
results per kind, rate limiting, and project isolation enforced in SQL. A query
that asks for something ARGUS does not implement is *told*, not silently emptied.

## 9. Intelligence integration (§19–§29)

The dashboard composes state, incidents, predictions, remediations and changes into
one view; the service catalog adds ownership, dependencies, blast radius and an
operational state per component; change intelligence answers "did this deployment
make things worse?" by reading failure rates and incident correlations rather than
by asserting causation; and the **case assistant** answers only from one case's
stored rows, cites them, labels facts/hypotheses/predictions separately, lists its
unknowns, and refuses to state a root cause that is not a stored candidate. It
ships switched off, and the capability sheet is served either way so an operator
can see the guarantees and the refusals rather than trust them.

## 10. Integrations (§95–§99)

Provider abstractions exist for repositories, notification channels, identity and
the external systems ARGUS talks to, with a declared capability set and
`ADAPTER_UNAVAILABLE` (rather than a silent no-op) when an operator has not
configured one. Webhooks carry a signature, a tolerance window and a replay guard,
and are rate-limited; an unverifiable payload is rejected, not trusted.

## 11. Platform health and graceful degradation (§57–§60, §105–§107)

`/platform/health`, `/platform/readiness` and `/platform/dependencies` report each
subsystem (ingestion, detection, graph, reproduction, fixes, prediction,
remediation, learning, platform), whether it is **required or optional**, and what
degrades when it is missing. ARGUS monitors itself on the same terms as the systems
it watches: queue depth, ingestion failures, slow queries, pool saturation, sweep
liveness. Degradation is a *reported state*, and a degradation notification is
deduplicated so an alert storm cannot become a second outage.

## 12. Security and trust boundaries

Unchanged from Phases 0–10 and re-verified here:

* no shell, no arbitrary command, no credential extraction, no deployment;
* external data (logs, traces, payloads, **and dashboard content**) is data, never
  instructions;
* an out-of-scope identifier is `404`, never data — asserted by the live gate for
  projects, cases, services and objectives;
* Phase 9's kill switch, default-deny policy and autonomy classification remain the
  only path to a live effect.

## 13. Performance

Every traversal, window, list and result set is clamped by configuration
(`PLATFORM_STATE_COMPONENT_LIMIT`, `PLATFORM_SEARCH_MAX_PER_KIND`,
`PLATFORM_SLO_SNAPSHOT_LIMIT`, `PLATFORM_WORKFLOW_BATCH`, …). System state and the
dashboard are cached briefly (`PLATFORM_STATE_CACHE_TTL_SECONDS`), the per-project
sweep step is bounded and failure-isolated, and indexes back the queries the
dashboard actually issues (project+status, project+opened_at). The gate's 18 steps
including a full sweep run in seconds against a database seeded with prior phases'
data.

## 14. Testing

| Suite | Tests | Notes |
| :--- | :--- | :--- |
| Phase 11 state | 29 | precedence table, `UNKNOWN` vs `HEALTHY`, transitions, history |
| Phase 11 case | 29 | reference allocation and races, legal/illegal moves, timeline dedup, linked evidence |
| Phase 11 workflow | 21 | stage order, preconditions, terminal runs, stops |
| Phase 11 SLO | 20 | target bounds per indicator, evaluation, burn states, budgets |
| Phase 11 platform | 65 | configuration/versioning/rollback, notifications, data quality, reports, postmortems, search, sweep isolation |
| Web (platform) | +28 | presentation rules: bands not points, `UNKNOWN` first-class, no fake precision |

### Defects found by running Phase 11 live (and fixed)

<a id="defects-found-by-running-phase-11-live-and-fixed"></a>

Running the gate against PostgreSQL found six backend defects. Each has a regression
test; the last three would have reached production. Two frontend defects surfaced
while cross-checking the UI against the API — both would have made a surface
unusable.

1. **`max(reference)` is a string comparison.** `'CASE-9' > 'CASE-10'`, so case
   reference allocation stopped growing at nine and every later case re-derived a
   reference that already existed — a duplicate-key violation on
   `POST /platform/sweep`.
2. **A lost case-reference race was fatal.** Two concurrent openers (the API sweep
   and the scheduled sweep) both computed the same reference; the loser raised
   instead of retrying. Claims are now atomic under a savepoint with a freshly
   derived reference.
3. **A failing sweep step poisoned the pass.** The sweep documents every step as
   failure-isolated, but a database-level failure left the shared transaction
   aborted, so every *later* step raised `PendingRollbackError`, hiding the real
   cause and 500ing the request. Steps now run inside savepoints.
4. **Concurrent detection collided on the fingerprint registry.** The explicit
   detect endpoint, the scheduled sweep and the ingestion worker could all reach
   the same sample first; the loser 500'd on
   `uq_anomaly_fingerprints_project_fingerprint`. The claim is now conflict-tolerant
   and adopts the rival's row, or reports honestly that it cannot count the cycle.
5. **Two detect+correlate passes deadlocked PostgreSQL.** A fingerprint `UPDATE`
   and an anomaly `UPDATE` blocked each other across two transactions. Fixed with a
   per-project write mutex (`SELECT … FOR UPDATE` on the project row) taken by the
   endpoint, the sweep and the worker, with deterministic project ordering so two
   sweeps cannot take the same locks in opposite orders.
6. **Project deletion deadlocked the background sweep.** The cascade `DELETE FROM
   environments` and a sweep's `INSERT INTO error_budget_snapshots` held rows each
   other needed next. The delete path now takes the same per-project mutex.

Cross-checking the UI against the API surfaced two vocabulary mismatches — both
would have made a surface unusable:

7. **The objective form sent `GTE`/`LTE`** and omitted the metric name, so every
   objective created from the UI was rejected; the indicator list also offered a
   `THROUGHPUT` that does not exist. The form now uses the API's own vocabulary.
8. **The configuration editor offered `PROJECT`/`ENVIRONMENT`/`PLATFORM`** — none of
   which are writable — so every save from it was refused. It now offers exactly the
   scopes a write is accepted for (the data-quality disposition list had the same
   class of drift: `SUPPRESSED` instead of `IGNORED`, fixed in the same pass).

A pre-existing Phase 3 test fragility was also observed (the async-hook step in
`e2e-smoke-phase3.sh` counts a fixed window); it passed 103/103 on both confirming
runs and is listed under limitations rather than papered over.

## 15. End-to-end demo

`bash infrastructure/e2e-smoke-phase11.sh` walks the entire lifecycle over the real
HTTP API against the Compose stack, in 18 steps, using an isolated scratch project
so the seeded demo is never perturbed:

detection → correlation → **derived system state** → **control-plane sweep opens a
case** → case timeline and a legal transition (and a refusal for an illegal one) →
capability sheet and the assistant's honest refusal → service catalog and recorded
ownership → objective, evaluation and error budget, including an in-range and an
out-of-range target → grouped search with isolation → data quality → versioned
configuration, ledger and rollback → feature flags → notifications → reports,
metrics and improvement plan → platform health, readiness and degradation →
cross-project isolation → the workspace rendering all eleven pages → cleanup that
deletes everything the run created.

It is adversarial about its own subject: it fails if the pipeline produces nothing
(state with zero components, no case, no incident) rather than reporting a pass for
"nothing happened", and it asserts what ARGUS *refuses* — an unauthenticated scope,
a foreign identifier, an unknown status, an out-of-range target, an objective with
no metric, a write to a scope the platform does not own.

## 16. Limitations

Stated plainly, because a platform that overstates itself is worse than useless.

* **State is derived and therefore as good as its inputs.** A `DEGRADED` component
  is degraded *according to stored telemetry*; if nothing was instrumented, the
  state is `UNKNOWN` rather than a guess — which means a quiet system and an
  unobserved one look similar unless you read the evidence coverage.
* **A case is an index, not a proof.** It gathers what the phases concluded; if
  Phase 4 declined to name a root cause, the case shows the decline, not an answer.
* **Correlation is per project.** Phase 11 unifies the *surfaces*, not the
  reasoning: two projects are still separate causal universes and knowledge still
  does not pool across them (`PLATFORM_CROSS_PROJECT_INTELLIGENCE_ENABLED` exists
  and is off).
* **Prediction remains an expectation.** The dashboard renders Phase 8's bands with
  their uncertainty and never upgrades a forecast into a fact.
* **Remediation reaches ARGUS's own runtime only.** Phase 9's registry and adapters
  are unchanged; infrastructure actions stay `ADAPTER_UNAVAILABLE` until an operator
  configures an adapter, and nothing in Phase 11 can execute anything.
* **The AI case assistant is off by default, and it is an explainer.** It answers
  from stored rows with citations; it is not consulted by any decision path, and it
  cannot change state, policy or lessons.
* **Notifications are in-app by default.** `EMAIL` and `WEBHOOK` channels exist as
  abstractions; a deployment that expects paging must configure and test them.
* **No authentication yet.** Isolation remains server-side ownership validation on
  every request; token auth and per-project authorization are the next work item, and
  until then ARGUS must sit behind a trusted boundary.
* **Workflow deadlines are wall-clock.** A pass that cannot advance a run because
  its evidence has not arrived will stop it at the configured deadline and say so —
  which is honest, but it does mean a slow upstream phase becomes a stopped run.
* **Sweep tuning is a deployment concern.** Project ordering and the per-project
  mutex serialise writers; a deployment with a very large number of projects should
  raise `PLATFORM_WORKFLOW_BATCH` deliberately rather than expect a single pass to
  finish everything.
* **One known test fragility.** `e2e-smoke-phase3.sh`'s async-hook step waits a
  fixed period for the worker; on a loaded machine it can observe the anomaly before
  it lands. It is a gate timing assumption, not a product defect, and it is listed
  here rather than hidden.
