# ARGUS — Unified Reliability Platform (Phase 11)

Phase 11 consolidates Phases 0–10 into one control plane. It adds no new
intelligence and no new authority: it derives state, orchestrates what already
exists, and makes governance, search and health visible in one place.

* [1. The control plane](#1-the-control-plane)
* [2. System state](#2-system-state-2-5)
* [3. Reliability cases](#3-reliability-cases-14-18)
* [4. Workflow engine](#4-workflow-engine-11-13)
* [5. SLOs and error budgets](#5-slos-and-error-budgets-32-35)
* [6. Change intelligence](#6-change-intelligence-38-41)
* [7. Search](#7-search-17-18)
* [8. Service catalog](#8-service-catalog-30-31)
* [9. Data quality center](#9-data-quality-center-87-90)
* [10. Governance, configuration and audit](#10-governance-configuration-and-audit-91-94-100)
* [11. Notifications](#11-notifications-53-56)
* [12. Platform health and degradation](#12-platform-health-and-degradation-57-60-105-107)
* [13. Reports and postmortems](#13-reports-and-postmortems-73-85)
* [14. The case assistant](#14-the-case-assistant-27-29)
* [15. Concurrency model](#15-concurrency-model)
* [16. Data model](#16-data-model)
* [17. Configuration](#17-configuration)
* [18. API surface](#18-api-surface)

---

## 1. The control plane

```text
                    ┌────────────────────────────────────┐
                    │  Phase 0–10 tables (source truth)  │
                    └───────────────┬────────────────────┘
     derive ◄──────────────────────┤
                                   │  publish platform events
                                   ▼
   system state          platform_events ──► correlate ──► case timeline
        │                                                    │
        └──────────────────────────────► Reliability Case ◄───┘
                                             │
                                     workflow run (10 stages)
                                             │
             ┌───────────┬───────────┬───────┴────────┬────────────┐
             ▼           ▼           ▼                ▼            ▼
        diagnosis   prediction   remediation      verification   learning
        (Phase 4)   (Phase 8)    (Phase 9, gated)  (Phase 9)     (Phase 10)
```

Two rules make the diagram honest.

**Reads are derived.** Every dashboard number is computed from the rows the earlier
phases own. Phase 11 stores only its own decisions — cases, timelines, workflow
runs, events, objectives, budgets, notifications, quality issues and configuration
versions. There is deliberately no `platform_incidents` table: a second table with
an opinion about whether something is resolved is exactly the contradiction this
phase exists to remove.

**Writes go through the owner.** The workflow asks Phase 4 to analyse, Phase 9 to
execute (through its own five gates), Phase 10 to learn. It never writes their rows
directly, and it cannot raise a policy ceiling.

## 2. System state (§2–§5)

`app/services/system_state.py`

State is a *derivation with a stated reason*, not a flag. For each component the
service collects signals — open incidents, unresolved anomalies, recent
deployments, drift, forecasts, remediations, telemetry health — and resolves them
through one precedence order:

| Priority | Condition | State |
| :--- | :--- | :--- |
| 1 | an open or acknowledged incident on the component | `INCIDENT` |
| 2 | an active remediation executing or verifying | `REMEDIATING` |
| 3 | a critical or high forecast within its validity window | `AT_RISK` |
| 4 | unresolved anomalies, or a degraded dependency/telemetry signal | `DEGRADED` |
| 5 | evidence exists and none of the above | `HEALTHY` |
| 6 | no evidence at all | `UNKNOWN` |

The precedence is tested as a table, because a precedence bug is the one that makes
a resolved incident render as healthy. Every component response carries
`state_evidence` — the counts and ids behind the decision — and a component with no
evidence is `UNKNOWN`, never `HEALTHY`.

Transitions (`component_state_transitions`) record the move, the trigger, the
environment and when it happened; they are what the timeline, the improvement plan
and the history endpoint read.

## 3. Reliability cases (§14–§18)

`app/services/reliability_case.py`

A case is the unified operational object: the thing a person opens, triages, works
and closes. `CASE-<n>` references are allocated per project, the lifecycle is one
legal-transition table, and the timeline is an ordered, deduplicated story.

```text
OPEN → TRIAGED → ANALYZING → DIAGNOSED → REMEDIATION_READY → AUTHORIZED
     → EXECUTING → VERIFYING → RESOLVED → LEARNED → CLOSED
                                     └──────────► CANCELLED
```

Reading a case assembles evidence *at request time* from the phases that own it:
the incident, its anomalies, the causal analysis and its candidates, the
reproduction verdict, debug hypotheses, fix candidates, forecasts, remediation
actions, configuration changes and learned knowledge. Nothing is copied, so a case
cannot show yesterday's anomalies while the component keeps degrading.

Reference allocation is race-safe. `max(reference)` is a *string* comparison, and
`'CASE-9' > 'CASE-10'`, so the number is parsed numerically; a lost race (two
sweeps opening a case at the same instant) is retried under a savepoint with a
freshly derived reference rather than failing the caller.

## 4. Workflow engine (§11–§13)

`app/services/reliability_workflow.py`

One run per case, ten stages, evidence-driven progression:

```text
DETECTED → TRIAGED → ANALYZING → DIAGNOSED → REMEDIATION_READY
        → AUTHORIZED → EXECUTING → VERIFYING → RESOLVED → LEARNED
```

* **A stage is entered only when its precondition holds.** A run whose analysis has
  not finished stays where it is; the sweep does not advance it because a timer
  fired.
* **Every advance is recorded** with the actor, the reason and the evidence
  freshness it relied on.
* **Stops are first-class** (`WorkflowStopReason`): a deadline, stale evidence, a
  failed gate, a human decision. A terminal run is terminal — `check_preconditions`
  refuses to report "all clear" for a finished run, which is what used to let a
  sweep resurrect a stopped one.
* **Authorization comes from Phase 9.** `AUTHORIZED` means the policy engine and, if
  required, a human said yes; the workflow cannot grant it.

## 5. SLOs and error budgets (§32–§35)

`app/services/slo_service.py`

An objective names what it measures:

```json
{
  "name": "checkout error rate",
  "indicator": "ERROR_RATE",
  "metric_name": "http.checkout.error_rate",
  "comparison": "AT_MOST",
  "target": 0.5,
  "window_seconds": 86400
}
```

* **An objective that names no metric is refused** — an evaluable objective has to
  know what to read.
* **Target bounds are per indicator family.** A ratio indicator (`AVAILABILITY`,
  `ERROR_RATE`) lives in `0..1`; a magnitude indicator (`LATENCY`, `SATURATION`)
  is bounded by a large ceiling that exists to catch a dropped decimal point, not to
  forbid a 500 ms objective.
* **Evaluation states its own quality.** Compliance, sample count, data quality and
  burn state are recorded per window; an objective that has never been evaluated is
  `UNKNOWN`, never "meeting".
* **Burn thresholds are configuration** (`PLATFORM_BURN_ELEVATED`,
  `_FAST`, `_CRITICAL`), so the API, the sweep and the UI cannot disagree about what
  a fast burn is.
* **Budgets are append-only snapshots**, so a burn history is auditable.

## 6. Change intelligence (§38–§41)

`app/services/change_intelligence.py`

Answers "did this deployment make things worse?" from stored evidence: failure rates
before and after, incident correlations within the configured window, affected
components via the dependency graph, and a risk assessment that separates *temporal*
relevance from *causal* relevance. It never asserts that a deployment caused an
incident — that is Phase 4's job, with Phase 4's evidence rules.

## 7. Search (§17, §18)

`app/services/global_search.py`

One query language, one response shape:

```text
checkout kind:incidents severity:high since:7d
```

Results are grouped by kind, bounded per kind, returned with the filters the engine
actually applied, and accompanied by an explicit answer when nothing matched. An
unknown filter is reported rather than ignored — a silently ignored filter looks
like an outage. Isolation is enforced in SQL by project, and a query without a
project scope is refused.

## 8. Service catalog (§30, §31)

`app/services/service_catalog.py`

Per component: identity and aliases, ownership (team, contact, on-call,
documentation), dependencies and dependents, endpoints, blast radius, recent
changes, current operational state, and the objectives that cover it. Sections that
cannot be computed say so (`not_computable` with a reason) rather than returning an
empty list that looks like "nothing is wrong".

## 9. Data quality center (§87–§90)

`app/services/data_quality_center.py`

Cross-phase consistency checks find rows that cannot both be true: an incident with
no component, a prediction with no feature snapshot, a remediation executed without
an authorization, knowledge with no evidence, a stale component, a broken
relationship, an inconsistent state. Findings become `data_quality_issues` with a
kind, severity, subject and evidence.

Two rules:

* **It reports; it never repairs.** ARGUS does not silently rewrite another phase's
  history. An operator dispositions an issue (`OPEN`, `ACKNOWLEDGED`, `RESOLVED`,
  `IGNORED`), which changes the issue, not the finding.
* **A check that errors is a bug, not a finding.** A check that raised used to be
  swallowed by the per-check handler, so a whole class of inconsistency was never
  reported; the suite asserts a clean error list.

## 10. Governance, configuration and audit (§91–§94, §100)

`app/services/platform_config.py`

* **Versioned writes.** A configuration write names its scope and a reason, is
  validated (bounds, allowed values, scope ownership, secret rejection), and is
  stored as a new version in an append-only ledger.
* **Rollback is a new version.** Restoring version *n* writes version *n+1* whose
  content equals *n* and which records `restored_from`. History is never rewritten.
* **Only owned scopes are writable.** `PROJECT_SETTINGS`, `SLO`, `NOTIFICATIONS`,
  `LEARNING`, `RETENTION`. Deployment-owned scopes (`PROJECT`, `ENVIRONMENT`,
  `INTEGRATIONS`, `FEATURE_FLAGS`) are *reported* from environment configuration;
  writing to them is refused, not silently ignored.
* **Secrets never round-trip.** Redacted fields are named on the response and absent
  from the payload.
* **Audit** is the union of Phase 9's hash-chained action audit, the case timeline
  (`sync_case_with_phases`) and the platform event stream. Every entry names its
  actor, its source and its subject; deleting a project deletes what it recorded.

## 11. Notifications (§53–§56)

`app/services/platform_notifications.py`

Notification kinds are a closed set: critical incident, high predicted risk,
remediation approval, remediation failure, rollback, SLO burn, learning insight,
system degradation, data quality. Two properties matter more than the catalogue:

* **Deduplication and cooldown.** An alert storm is itself an outage; the same kind
  for the same subject inside the cooldown window updates the existing notification.
* **Delivery is a channel abstraction.** `IN_APP` ships; `EMAIL` and `WEBHOOK` are
  configured, and a channel that is not configured says so rather than pretending.

## 12. Platform health and degradation (§57–§60, §105–§107)

`app/services/platform_health.py`

ARGUS monitors itself on the same terms as the systems it watches: ingestion queue
depth and failure rate, detection liveness, slow queries, connection-pool
saturation, sweep liveness and the fix/reproduction/learning backlogs. Each
subsystem is labelled **required** or **optional**, and `/platform/dependencies`
states what degrades when an optional one is missing — the graceful-degradation
contract in one payload.

## 13. Reports and postmortems (§73–§85)

`app/services/platform_reports.py`

Reports (incident, reliability, executive, change, remediation, learning) and
postmortems are composed from stored rows with their sections named and their
**limitations stated**: missing evidence, unelapsed horizons, unmeasured sections
and sample sizes appear in the output rather than in the reader's imagination. A
postmortem's timeline falls back to the incident's own timeline when no case exists,
which is what makes it usable for an incident nobody routed.

## 14. The case assistant (§27–§29)

`app/services/case_assistant.py`

Retrieval first, then an answer:

1. compile an **evidence pack** from one case's stored rows (bounded and scoped);
2. answer only from that pack, citing the rows behind each claim;
3. label facts, hypotheses and predictions separately;
4. list what it does not know, and why its confidence is what it is;
5. refuse an action, an out-of-scope question, or a root cause that is not a stored
   candidate.

The assistant is **off by default** (`PLATFORM_CASE_ASSISTANT_ENABLED=false`). Its
capability sheet (`GET /platform/case-assistant`) is served either way, so the
guarantees and refusals are readable without trusting them; a switched-off
deployment refuses an ask with a reason instead of answering from nothing.

## 15. Concurrency model

Phase 11 is the first phase where many writers touch the same rows at once — three
detection paths, two sweep schedulers, and human actions on cases, SLOs and
configuration. The model is deliberately simple:

* **One writer per project.** `app/services/project_lock.py` takes
  `SELECT … FOR UPDATE` on the project row. Detection (endpoint, sweep, worker),
  correlation and project deletion all take it, so a cascade delete and a sweep
  cannot interleave.
* **Deterministic ordering.** Sweeps iterate projects by `created_at`, because two
  sweeps must take the same locks in the same order or they deadlock on the mutex
  instead of the data.
* **Savepoint isolation.** Each sweep step runs inside a savepoint: a step that
  fails is rolled back and recorded, and the pass keeps the work of the steps that
  succeeded. Without this, one database-level failure aborted the shared transaction
  and every later step failed as a knock-on effect, hiding the real cause.
* **Atomic claims for derived-but-unique rows.** Case references and anomaly
  fingerprints are claimed with a savepoint and an explicit conflict path, so losing
  a race is an *adoption*, never a 500.
* **Bounded passes.** Batch sizes, deadlines and per-scope attempts are all
  configuration; no pass is unbounded.

## 16. Data model

Eleven tables (migration `fa1b2c3d4e5`):

| Table | Holds |
| :--- | :--- |
| `reliability_cases` | the operational object; unique `(project_id, reference)` |
| `reliability_case_timeline` | ordered, deduplicated story entries with their source |
| `reliability_workflows` | one run per case: stage, status, stop reason, state |
| `platform_events` | what the phases concluded and did, as timeline input |
| `reliability_context_snapshots` | the context a case or workflow reasoned over |
| `component_state_transitions` | recorded state moves and their triggers |
| `service_level_objectives` | objectives with indicator, metric, target, comparison |
| `error_budget_snapshots` | append-only budget and burn history |
| `platform_notifications` | deduplicated, cooldown-limited notifications |
| `data_quality_issues` | cross-phase inconsistencies with their disposition |
| `configuration_versions` | the append-only configuration ledger |

Two columns were added to `component_owners` (on-call and documentation) because
§31 names them as real fields — a JSON blob would have made them unrunnable.

## 17. Configuration

Every `PLATFORM_*` setting has a safe default, and the defaults are *conservative*:
the AI assistant is off, cross-project intelligence is off, the workflow batch is
small, and every limit is a clamp on a bounded pass. The knobs that matter:

| Setting | Default | Purpose |
| :--- | :--- | :--- |
| `PLATFORM_ENABLED` | `true` | the whole control plane |
| `PLATFORM_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | `true` / `120` | the scheduled pass |
| `PLATFORM_AUTO_CASE_ENABLED` | `true` | route live high-severity incidents into cases |
| `PLATFORM_CASE_ASSISTANT_ENABLED` | `false` | §27 assistant |
| `PLATFORM_CROSS_PROJECT_INTELLIGENCE_ENABLED` | `false` | keep causal universes separate |
| `PLATFORM_CASE_EVIDENCE_LOOKBACK_SECONDS` | `86400` | how far back a case's evidence reaches |
| `PLATFORM_STATE_*_WINDOW_SECONDS` | 1800–86400 | the windows state is derived over |
| `PLATFORM_BURN_{ELEVATED,FAST,CRITICAL}` | 2 / 6 / 14 | one source of truth for burn |
| `PLATFORM_SLO_MAX_WINDOW_SECONDS` | `2592000` | the longest objective window |
| `PLATFORM_WORKFLOW_{DEADLINE,EVIDENCE_MAX_AGE}_SECONDS` | 86400 / 3600 | staleness and stop rules |
| `PLATFORM_SEARCH_{RATE_LIMIT_PER_MINUTE,MAX_PER_KIND}` | 120 / 25 | bounded search |
| `PLATFORM_{STATE,DASHBOARD,SEARCH}_CACHE_TTL_SECONDS` | 30 / 30 / 20 | read caches |
| `PLATFORM_{DB_SLOW_QUERY_MS,DB_POOL_WARN_PERCENT,QUEUE_DEPTH_WARN,INGESTION_FAILURE_WARN}` | 1000 / 80 / 1000 / 50 | self-monitoring thresholds |
| `RETENTION_PLATFORM_{EVENTS,SNAPSHOTS,NOTIFICATIONS}_DAYS` | 365 / 180 / 365 | its own data does not grow forever |

## 18. API surface

49 paths under `/api/v1/platform`, all project-scoped, all refusing an
out-of-scope identifier with `404`:

| Group | Endpoints |
| :--- | :--- |
| Overview & state | `/platform/{overview,state,live,health,readiness,dependencies,metrics,activity,engineering}` |
| Cases | `/platform/cases`, `/platform/cases/{id}`, `/cases/{id}/{status,story,ask}` |
| Workflow | via cases and the sweep; runs are readable on the case |
| Catalog & ownership | `/platform/services`, `/services/{id}`, `/services/{id}/{blast-radius,ownership}` |
| Objectives | `/platform/slo`, `/slo/evaluate`, `/slo/{id}`, `/slo/{id}/error-budget` |
| Changes | `/platform/changes`, `/changes/{risk,failure-rate}` |
| Search | `/platform/search`, `/platform/search/help` |
| Data quality | `/platform/data-quality`, `/data-quality/check`, `/data-quality/{id}/status` |
| Governance | `/platform/configuration`, `/configuration/rollback`, `/feature-flags`, `/integrations`, `/notifications`, `/notifications/{id}/read`, `/case-assistant` |
| Reports | `/platform/reports`, `/platform/improvement-plan`, `/platform/incidents/{id}/postmortem` |
| Platform ops | `/platform/{context,projects,environments/compare,dependencies,story/{correlation_id},sweep}`, `/platform/webhooks/{source}`, `/platform/webhooks/requirements` |

See [phase-11-report.md](phase-11-report.md) for the delivery evidence and the
defects the live gate found, and [roadmap.md](roadmap.md) for what is deliberately
not built.
