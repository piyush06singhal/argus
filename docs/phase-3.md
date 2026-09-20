# Phase 3 — Anomaly & Incident Intelligence

> **Phase 3 performs anomaly detection and incident correlation. It does not
> perform causal root-cause analysis.** Returning a root cause is Phase 4's job.

Phase 1 taught ARGUS to collect evidence. Phase 2 taught it the structure of the
software producing that evidence. Phase 3 adds the layer that turns telemetry
into something an engineer can act on: **detection → correlation →
contextualization → explanation → visualization**.

```
Telemetry (Phase 1: events, logs, metrics, traces, spans, deployments, health)
    │
    ├─ ingest hook  ──►  argus:detect:jobs   (async, idempotent by fingerprint)
    └─ scheduled sweep ─────────────────────┐
                                            ▼
                          Baseline engine (STATIC | ROLLING)
                                            ▼
                     Deterministic detectors (no AI, no invented scores)
                                            ▼
                          Anomalies (fingerprinted, deduplicated)
                                            ▼
                Incident correlation engine (shared-evidence clustering)
                                            ▼
                 Incident manager (lifecycle, timeline, evidence, blast radius)
                                            ▼
        Deterministic summary  +  Phase 2 knowledge-graph context
                                            ▼
                      API  +  Incident Intelligence UI
```

## The boundary that defines this phase

These distinctions are enforced in code, not just documented:

| Not this | But this |
| --- | --- |
| Anomaly = Incident | An anomaly is one detected deviation; an incident groups several |
| Incident = Root cause | An incident is a correlated set of symptoms with context |
| Correlation = Causation | Grouping is based on shared *evidence*, never on causal inference |
| Affected component = Failing component | The blast radius is labelled `DIRECTLY_OBSERVED` vs `UPSTREAM/DOWNSTREAM/DEPENDENCY_CONTEXT` |
| Deployment before anomaly = Cause | Recorded as `is_context_only`, with the words "does not establish that the deployment caused the incident" |
| Confidence = Probability of cause | Confidence is **evidence strength** from the detecting rule |

The phrase "caused the incident" appears in the product exactly once: inside the
sentence that denies it.

## Anomaly model

`Anomaly` (`app/models/anomaly.py`) carries provenance on every row: `source`
(METRIC/LOG/TRACE/SPAN/HEALTH_CHECK/…), `rule_id`, `source_event_id`, and the
`fingerprint` used for deduplication.

Anomaly types: `METRIC_THRESHOLD`, `METRIC_BASELINE_DEVIATION`,
`ERROR_RATE_SPIKE`, `LATENCY_SPIKE`, `THROUGHPUT_DROP`, `LOG_PATTERN_SPIKE`,
`TRACE_FAILURE_SPIKE`, `HEALTH_DEGRADATION`, `REQUEST_RATE_CHANGE`,
`RESOURCE_USAGE_SPIKE`, `DEPLOYMENT_RELATED_CHANGE`,
`CONFIGURATION_RELATED_CHANGE`. The last two are contextual associations only.

Lifecycle (`app/services/anomaly_state.py`):

```
DETECTED ──► ACKNOWLEDGED ──► INVESTIGATING ──► RESOLVED
    └──────────────┴──────────────┴───────────► EXPIRED (terminal)
RESOLVED ──► DETECTED   (recurrence reopens instead of duplicating)
```

## Baseline strategies

`app/services/baseline.py` is pure — no database, no I/O:

* **STATIC** — the expected value and threshold come from the rule. Used for
  explicit ceilings (p95 latency > 500 ms).
* **ROLLING** — mean, median, population standard deviation, min/max, p50/p95/p99
  over a configurable window.

Honesty rules baked into the engine:

* **Missing data is not failure.** `None` samples are dropped; a baseline without
  enough history reports `INSUFFICIENT_DATA` with `sufficient=False`, and
  nothing downstream may treat that as an anomaly. The run result reports
  `insufficient_baselines` for operators.
* **Zero variance is not a division.** When σ ≈ 0 the z-score is `None`, never
  `inf`.
* **A rolling baseline never judges an observation against itself.** The sample
  under evaluation is excluded from its own baseline, so a genuine spike cannot
  inflate its expected value and partially hide itself.

## Detectors

All detectors are deterministic pure functions in `app/services/detectors.py`:
`detect_threshold`, `detect_baseline_deviation`, `detect_z_score`,
`detect_rate_change`, `detect_error_rate`, `detect_latency_ratio`,
`detect_pattern_spike`, `detect_trace_failure_rate`, `detect_health_transition`.
Each returns a `DetectionOutcome` carrying the observed/expected values, the
deviation, the threshold, a bounded confidence, and a plain-language reason.

Log patterns are normalized deterministically (`normalize_log_pattern`):
`user_id=123` and `user_id=456` collapse to `user_id=<num>`. It deliberately
replaces only well-known dynamic shapes (UUIDs, emails, IPs, long hex, numbers)
rather than attempting a universal parser that would wrongly collapse messages.

Every detector must be able to explain itself. The API returns an
`explanation` block (`app/services/anomaly_explanation.py`) answering *why was
this detected*: condition, baseline strategy and sample count, observed vs
expected, the threshold crossed, the telemetry source, and the fingerprint
material.

## Severity

Severity is a deterministic, explainable function (`app/services/anomaly_severity.py`)
of magnitude, criticality, duration and blast radius. Every decision stores its
`reasons` and `factors` in the anomaly metadata; the API and UI can show exactly
which inputs produced the level. No AI-generated scores anywhere.

## Fingerprinting & noise reduction

```
anomaly_fingerprint  = hash(project | environment | component | type | metric-or-pattern)
incident_fingerprint = hash(project | environment | primary component | dominant type
                            | time bucket | related component set)
```

Fingerprints are pure functions of stable context — never a timestamp, never a
random id — so the same problem in the same place always produces the same key.
The `anomaly_fingerprints` registry counts occurrences and points at the current
row, which turns "an anomaly every 10 seconds for 5 minutes" into **one evolving
anomaly** rather than 30 records. Cooldowns and `persistence_cycles` suppress
single-sample noise before anything is opened.

## Correlation

`app/services/incident_correlation.py`. Correlation requires **meaningful shared
evidence**, not merely shared timing:

* same project **and** same environment are hard preconditions — production and
  staging can never merge, and neither can two projects';
* times must fall inside the correlation window (default 300 s);
* two anomalies link only when they sit on the **same component**, or on
  **structurally adjacent components** (bounded hop count, default 2) using both
  `component_dependencies` and Phase 2 evidence-derived graph edges;
* the cluster's total time span is capped, so transitive chaining cannot grow a
  cluster into "everything that happened this hour";
* additional context (same metric, same log template, a nearby deployment)
  enriches the recorded rationale but can never create a link on its own.

Two unrelated services erroring at the same moment stay separate — that is the
false-merge protection, and it is directly tested.

Every cluster stores its `rationale`: the signals that fired, the reasons in
plain language, the anomaly and component counts, and the window used. "Why were
these grouped?" is answerable from the data.

## Incident lifecycle

`app/services/incident_state.py` is the single source of truth for legal
transitions, shared by the API and the manager, and mirrored by the frontend so
the UI can only offer actions the backend accepts:

```
OPEN ─┬─► ACKNOWLEDGED ─┬─► INVESTIGATING ─┬─► MITIGATED ─┐
      │                 │                  │              ├─► RESOLVED ─┬─► CLOSED
      └─────────────────┴──────────────────┴──────────────┘             │
                          RESOLVED/CLOSED ──► OPEN  (reopen) ◄──────────┘
```

* Illegal transitions return **409**, never a silent write.
* An incident whose anomalies are all resolved/expired is auto-resolved, and the
  transition is attributed to `system:auto_resolve` on the timeline.
* A recurrence inside the same fingerprint bucket **reopens** the incident
  instead of creating a duplicate.

## Incident timeline, evidence and blast radius

The timeline (`incident_timeline_events`) records only real stored facts, ordered
by when they happened (UTC), plus entries explicitly marked `is_context_only`.
Nothing is fabricated to fill gaps.

Evidence (`incident_evidence`, extended in place) carries `provenance`,
`observed_value`, `expected_value`, `confidence`, and a **`relevance_reason`** —
why the item is *relevant*, never why it is the cause. Deployment and
configuration evidence is scored 0.5 with the wording "temporal context only".

Affected components are classified `DIRECTLY_OBSERVED`, `UPSTREAM_CONTEXT`,
`DOWNSTREAM_CONTEXT` (derived from dependency direction) or
`DEPENDENCY_CONTEXT` (structural graph neighbours). Presence in the graph is
never presented as failure.

## Suppression & maintenance windows

Suppression is **recorded, never silent**. Anomalies inside an active
suppression rule or maintenance window are still detected, stored and returned by
the API with `suppressed=true`, the rule reference, the reason and the timestamp.
Maintenance windows may suppress or downgrade severity — never hide data.

Suppressions are **deactivated, never deleted** — `PATCH
/api/v1/anomaly-suppressions/{id}` and `PATCH /api/v1/maintenance-windows/{id}`
close or disable a rule while its start time, reason and creator stay on file.
That is what makes "auditable" mean something: a mute that cannot be switched off
would be a leak, and a mute that can be deleted would leave no trace that
detection was ever quiet. A window still may not be inert — a patch that turns
off both `suppress_anomalies` and `downgrade_severity` is rejected with 422,
same as creation.

## Metrics

`app/services/anomaly_metrics.py` computes the reliability metrics from stored
rows. Two definitions are stated explicitly wherever the numbers appear, because
"MTTR" means different things in different tools:

* **MTTA** = mean(`acknowledged_at − detected_at`) over incidents acknowledged in
  the window.
* **MTTR** = mean(`resolved_at − detected_at`) over incidents resolved in the
  window — measured from *detection*, not acknowledgement.

Both are reported alongside their definition string in the API and the UI.
Prometheus gets `argus_anomalies_detected_24h`, `argus_anomalies_open`,
`argus_anomalies_suppressed_24h`, `argus_anomalies_deduplicated`,
`argus_incidents_created_24h`, `argus_incidents_open`,
`argus_incidents_resolved_24h`, per-severity/per-type breakdowns, and
`argus_incident_mtta_seconds` / `argus_incident_mttr_seconds`.

## Security and isolation

There is no token authentication yet (single-tenant Phase 0–3), so isolation is
enforced by **ownership validation on every request**, not by trusting a
client-supplied `project_id`:

* every list/detail route validates the project exists, and that a supplied
  environment belongs to it;
* nested resources (evidence, timeline, anomalies, components, graph) are
  fetched through a scoped helper — an out-of-scope id is **404, never data**, so
  existence does not leak;
* evidence may only reference components and anomalies inside the incident's own
  project;
* correlation is hard-partitioned by project *and* environment — `build_clusters`
  partitions a mixed input rather than merging it, so a stale call site cannot
  correlate production with staging;
* **scope is exact**: for detection, correlation and incident resolution,
  `environment_id=None` means "environment-less telemetry", never "all
  environments". A project-scope pass reads rows with `environment_id IS NULL`
  only; pooling environments would let one environment's incident fire an
  anomaly attributed to another. (List APIs are different on purpose: there
  `environment_id` is an optional *filter*, so omitting it spans the project.);
* traversals and result sets are bounded (context windows, `limit()` on every
  aggregate, pagination on every list).

## Performance

Indexed by `project_id`, `environment_id`, `component_id`, `detected_at`,
`status`, `severity`, `fingerprint`, `anomaly_type` and `incident_id`. Detection
is bounded per run (`ANOMALY_MAX_ANOMALIES_PER_RUN`,
`ANOMALY_MAX_TELEMETRY_SAMPLES`); correlation loads a bounded candidate set;
the dashboard buckets server-side rather than shipping raw rows. A benchmark
lives at `infrastructure/anomaly-benchmark.py`.

## Limitations (honest)

* ARGUS reports **observed blast radius**, not the real one. A component that is
  failing but produced no telemetry is invisible.
* Correlation is structural and temporal. Two genuinely related anomalies on
  unrelated components can be missed; two unrelated anomalies on adjacent
  components can be grouped.
* `DEPLOYMENT_RELATED_CHANGE` / `CONFIGURATION_RELATED_CHANGE` are temporal
  associations. Nothing more is claimed.
* Severity and confidence are rule-derived evidence strengths. They are not
  probabilities of anything.
* The environment comparison inherited from Phase 2 compares last-seen versions.
* No anomaly is ever inferred from missing data; `INSUFFICIENT_DATA` is reported
  instead.

## Deliberately not implemented (Phase 4+)

Root-cause causal inference, causal graphs, automated debugging, code-level root
cause detection, failure reproduction, patch generation and verification,
autonomous remediation, predictive failure forecasting, self-healing.

Phase 3 is: **Detect → Correlate → Contextualize → Explain → Visualize.**
It is not: Diagnose → Fix → Deploy.

## Demo scenario

`app/services/demo_incident.py` implements the deterministic
**"ARGUS Checkout Latency Incident"**: a production deployment 2 minutes before a
latency spike on Checkout Service, followed by a dependency-latency increase on
Inventory Service, an error-rate spike, trace failures, a health degradation and a
"connection refused" log spike.

It is not a fixture loader. The scripted telemetry is ingested, the **real
detectors** run, and the **real correlation engine** builds the incident — if a
layer regresses, the demo fails. Timing is anchored relative to the detection
instant so the result is reproducible anywhere.

Expected output: six anomalies across two components, correlated into **one**
incident, with the deployment attached as context-only evidence and a summary
that reads:

> … Deployment … occurred 2 minutes before the first observed anomaly.
> Deployment timing is temporal context only and does not establish that the
> deployment caused the incident.

Run it against a seeded environment with:

```bash
cd apps/api
python seed_data.py                 # full demo dataset
python seed_phase3_incident.py --force   # re-anchor the incident to "now"
```

Two properties make that re-anchor trustworthy, and both were fixed after they
bit in practice:

* **Components are resolved inside the chosen environment.** The demo topology
  deliberately mirrors names across environments ("Checkout Service" exists in
  production *and* as a staging mirror), so a name-only lookup binds a production
  incident to a staging component — a cross-environment leak in the demo data
  itself.
* **`--force` reconciles the demo rules instead of skipping them.** Rules are
  keyed by fixed demo ids; skipping a pre-existing one would pin the scenario to
  whatever scope an earlier run used, so a corrected re-seed could never repair
  it.

## Verification

| Gate | Command | Result |
| --- | --- | --- |
| Backend suite | `pytest -q` | 687 passed |
| Lint / types | `ruff check`, `ruff format --check`, `mypy app` | clean |
| Phase 0/1 regression | `bash infrastructure/e2e-smoke-phase1.sh` | 46/46 |
| Phase 2 regression | `bash infrastructure/e2e-smoke-phase2.sh` | 28/28 |
| Phase 3 live | `bash infrastructure/e2e-smoke-phase3.sh` | 102/102 |
| Frontend | `tsc --noEmit`, `npm run lint`, `vitest run`, `next build` | clean, 25 tests |
| Benchmark | `apps/api/.venv/bin/python infrastructure/anomaly-benchmark.py` | see output |

The Phase 3 gate is **re-runnable**: three consecutive runs pass with identical
counts. That is a requirement, not a nicety — the gate creates rules, telemetry
and suppressions, and a gate that only passes against a pristine database hides
regressions behind state. It therefore scopes its own artifacts to a reused
scratch project, deactivates the suppressions and windows it creates, identifies
the demo incident by the demo story rather than by list position, and exercises
anomaly lifecycle transitions on the throwaway anomaly it created rather than
consuming the demo's rows.
