<div align="center">

# ARGUS

**Autonomous Software Reliability & Engineering Intelligence**

A software reliability platform that builds a continuously evolving model of how a
system behaves and how it is built — then connects telemetry to architecture,
incidents, and change, so a failure can be explained instead of guessed at.

[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-20-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-14-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-1912%20backend%20%2B%20231%20frontend-brightgreen)](#verification)
[![Migrations](https://img.shields.io/badge/migrations-reversible-informational)](docs/development.md)

</div>

---

## What ARGUS is

Most observability stacks tell you *that* something is wrong. ARGUS is built to
answer *why* — and to be honest about when it cannot.

It does this in layers, each one resting on the last:

1. **Ingest** telemetry from any OpenTelemetry source — logs, metrics, traces,
   events — through a durable queue, with retention and validation.
2. **Model** the software that produced it: a knowledge graph of components,
   environments, endpoints, dependencies and their owners, with provenance on
   every relationship.
3. **Detect** abnormal behaviour deterministically, with baselines that say what
   "normal" meant, and group related signals into incidents.
4. **Explain** failures: evidence-supported hypotheses, the causal chain that
   connects the evidence, how confident the system is, and what contradicts the
   conclusion.
5. **Reproduce** a hypothesis: build an isolated sandbox for the relevant system
   state, replay sanitized inputs, inject controlled faults, capture what the
   sandbox actually did, and compare it against the original incident.

The design constraint that shapes everything: **ARGUS never claims more than its
evidence supports.** A root-cause candidate ships with its evidence counts, its
score components, the facts that argue against it, and the reason its confidence
is what it is. `"Insufficient evidence to determine a root cause"` is a
first-class, tested outcome — not a failure mode.

## Status

| Phase | Scope | State | Design |
| :--- | :--- | :--- | :--- |
| **0** | Foundation — data model, REST API, seed data, Docker, web app | ✅ shipped | [docs/architecture.md](docs/architecture.md) |
| **1** | Observability & Ingestion — OTLP, Redis queue + worker, retention, Prometheus | ✅ shipped | [docs/ingestion.md](docs/ingestion.md) |
| **2** | Software Knowledge Graph — typed nodes/edges, provenance, reconciliation, snapshots, impact | ✅ shipped | [docs/software-knowledge-graph.md](docs/software-knowledge-graph.md) |
| **3** | Anomaly & Incident Intelligence — detectors, baselines, correlation, lifecycle, evidence | ✅ shipped | [docs/phase-3.md](docs/phase-3.md) |
| **4** | Root Cause & Causal Analysis — temporal/trace/dependency/change analysis, causal graph, scoring | ✅ shipped | [docs/phase-4.md](docs/phase-4.md) |
| **5** | Failure Reproduction Engine — isolated sandbox, sanitized replay, controlled faults, comparison, hypothesis validation | ✅ shipped | [docs/phase-5.md](docs/phase-5.md) |
| **6** | AI Debugger — code intelligence, trace→code mapping, evidence-grounded analysis, validated code claims | ✅ shipped | [docs/phase-6.md](docs/phase-6.md) |
| **7** | Automated Fix Generation & Verification — fix hypotheses, patch generation, safety validation, isolated workspace, build/tests, two-sided regression test, verification, human review | ✅ shipped | [docs/phase-7.md](docs/phase-7.md) |
| **8** | Predictive Reliability — feature engineering, deterministic baseline predictors, risk policy, evaluation & calibration, walk-forward backtesting, leakage prevention, drift, early warnings, human-only | ✅ shipped | [docs/predictive-reliability.md](docs/predictive-reliability.md) |
| **9** | Safe Autonomous Remediation — action registry, safety & policy gates, approval or autonomous authorization, controlled execution, verification, rollback, audit, six execution regimes | ✅ shipped | [docs/safe-autonomous-remediation.md](docs/safe-autonomous-remediation.md) |
| **10** | Reliability Intelligence & Autonomous Learning — normalized experiences, nine pattern miners, validation, knowledge lifecycle with versions and human review, component profiles, learned relationships, grounded search, recommendations | ✅ shipped | [docs/reliability-intelligence.md](docs/reliability-intelligence.md) · [docs/learning-governance.md](docs/learning-governance.md) |
| **11** | Unified Reliability Platform — derived system state, Reliability Cases, cross-phase workflow orchestration, service catalog, SLOs & error budgets, change intelligence, global search, governance & audit, notifications, platform health and graceful degradation, reports & postmortems | ✅ shipped | [docs/unified-reliability-platform.md](docs/unified-reliability-platform.md) · [docs/phase-11-report.md](docs/phase-11-report.md) |

Phase 7 is the first phase that can produce a change — and it still does not
merge, deploy or remediate. It plans a fix from evidence that already exists,
generates the smallest defensible patch, validates it against scope and safety
rules, applies it inside a disposable git workspace, runs the repository's own
checks, proves the failure is gone with a two-sided regression test, compares
before and after, stores the evidence hashed and immutable, and stops at a human
decision. Nothing is changed in your repositories, and no result is presented as
proof.

Phase 8 turns that history forward in time. It answers *which components are
showing increasing reliability risk, over which horizon, on what evidence, and
how trustworthy that answer is* — then stops at a human decision. A forecast is
never a fact, never creates an incident, and never claims a component "will
fail"; the four statistical predictors are labelled baselines because a "model"
with no training data would be decoration. Expectations ship with their
confidence, calibration, coverage, evidence and limitations attached.

Phase 9 is the first phase that can *act*. It is built so that acting is the
hardest thing ARGUS does: a remediation is a proposal until it clears validation,
a safety assessment and a policy decision, and it executes only through an
explicit action registry — no shell, no arbitrary command, no provider
credentials, no deployment. Every action declares its risk, its blast radius, its
verification and its reversal up front; an irreversible one always needs a person;
autonomous execution is considered only in a scope whose name *and* declared type
both say non-production, only for actions the registry says may run unattended,
and only under the risk ceiling. Success means the system's behaviour changed for
the better over a verification window, not that a handler returned. Everything
that happens is hash-chained, reversible where the action allows, and stoppable
with one emergency-stop call. It ships disabled.

Phase 11 is the consolidation: everything above becomes one platform. It adds no
new intelligence and no new authority — it derives one **system state** from the
rows the other phases own, gives the work a single operational object (a
**Reliability Case**) with one timeline, drives a case through a **workflow** whose
stages advance only when their evidence exists, and surfaces governance, search,
objectives, change intelligence, data quality, notifications and the platform's own
health in one place. Nothing is duplicated: no `platform_incidents` table exists,
because a second table with an opinion about whether something is resolved is
exactly the contradiction this phase removes. Phase 9 stays authoritative over
every live effect, ARGUS still cannot reach your infrastructure, and the AI case
assistant ships off and answers only from one case's stored rows, with citations
and its unknowns attached.

Phase 10 turns that accumulated history into reusable knowledge — and is
careful about what "knowledge" means. Outcomes that completed (an incident
resolved, a patch verified, a forecast confirmed, a remediation effective) are
normalized into **experiences**, nine deterministic miners look for patterns
across them, and every pattern is validated against its own corpus before it is
allowed to advise anything. A pattern is written with its support, its window,
its provenance mix and its failed checks attached; it is versioned, reviewed by a
human, aged out when new data stops confirming it, and never deleted. Learning
excludes unconfirmed AI output by default, never crosses a project boundary, and
cannot change a policy, execute an action or raise a limit — the strongest thing
a learned pattern can do is inform a recommendation, which a person then accepts
or dismisses.

## How it works

```
                    OpenTelemetry / webhooks / API
                                 │
                                 ▼
                    Ingestion ─► Redis queue ─► worker
                                 │
                                 ▼
        ┌────────────────────────────────────────────────┐
        │  PostgreSQL — the single source of truth       │
        │  telemetry · incidents · evidence · graph      │
        └────────────────────────────────────────────────┘
               │                │                 │
               ▼                ▼                 ▼
        Knowledge Graph   Anomaly Detection   Causal Analysis
        (structure)       + Correlation       (explanation)
               │                │                 │
               └────────────────┴─────────────────┘
                                 ▼
                     Failure Reproduction
        plan ─► safety validation ─► disposable sandbox ─► sanitized replay
        ─► controlled faults ─► captured telemetry ─► comparison ─► verdict
                                 ▼
                          AI Debugger (Phase 6)
        repository snapshot ─► trace→code mapping ─► validated analysis
        ─► grounded hypotheses with citations ─► auditable timeline
                                 ▼
                Fix Generation & Verification (Phase 7)
        fix hypothesis ─► patch ─► safety validation ─► disposable workspace
        ─► build/static/tests ─► two-sided regression test ─► reproduction
        ─► comparison ─► VERIFIED | NOT_VERIFIED ─► human review
                                 ▼
                   Predictive Reliability (Phase 8)
        feature engineering ─► quality/staleness gate ─► baseline predictors
        ─► risk policy ─► forecast + signals ─► evaluation · calibration
        ─► walk-forward backtest · drift ─► early warnings ─► human decides
                                 ▼
                Safe Autonomous Remediation (Phase 9)
        proposal ─► safety ─► policy ─► approval | autonomous authority
        ─► controlled execution ─► verification ─► rollback if required
        ─► post-analysis ─► hash-chained audit ─► human or emergency stop
                                 ▼
              Reliability Intelligence & Learning (Phase 10)
        outcomes ─► learning events ─► normalized experiences ─► nine miners
        ─► validation ─► knowledge (versioned, reviewed) ─► profiles · learned
        relationships ─► grounded search ─► recommendations ─► human decides
                                 ▼
         Unified Reliability Platform (Phase 11) — the control plane
        derived system state ─► platform events ─► Reliability Case ─► workflow
        (10 evidence-gated stages) ─► objectives · error budgets · change
        intelligence · search · data quality · governance & audit · reports
        ─► platform health, degradation and self-monitoring
                                 ▼
                     Next.js investigation UI
        system map · anomaly center · incidents · root cause analysis
        · reproduction workspace · AI debugger · fix & verification workspace
        · predictive reliability dashboard, heatmap and component profiles
        · remediation console, action detail and policy editor
        · reliability intelligence center, pattern explorer, learned
        relationships, recommendation queue and grounded search
        · unified platform overview, reliability cases, service catalog,
        objectives & error budgets, changes, search, data quality, governance,
        reports, activity and platform health workspaces
```

## Features

<details open>
<summary><b>Observability &amp; ingestion (Phase 1)</b></summary>

- OTLP/JSON ingestion for traces, logs and metrics (protojson camelCase and
  snake_case), plus native REST ingestion endpoints
- Durable async pipeline: `202` → Redis → worker, with dead-letter capture and
  per-source health
- Trace cross-reference validation, span-tree storage, retention policies with
  preview and sweep, Prometheus `/metrics`
</details>

<details>
<summary><b>Software Knowledge Graph (Phase 2)</b></summary>

- Typed nodes and relationships mirrored from canonical entities — a graph
  *overlay*, never a duplicate representation
- Provenance (`Configured` / `Observed` / `Inferred`) and confidence semantics on
  every edge
- Component identity with aliases and ownership; service endpoints with
  normalized path templates; trace-driven relationship discovery
- Reconciliation that marks relationships `STALE` instead of deleting them,
  versioned snapshots with set-level diffs, environment comparison, dependency
  impact analysis, data-quality checks, and an interactive SVG graph explorer
</details>

<details>
<summary><b>Anomaly &amp; Incident Intelligence (Phase 3)</b></summary>

- Deterministic baselines (static and rolling: mean/median/stddev/p50/p95/p99)
  with explicit `INSUFFICIENT_DATA` — missing data is never treated as failure
- Nine detectors — threshold, baseline deviation, z-score, rate change, error
  rate, latency ratio, log-pattern spike, trace-failure rate, health transition —
  each a pure function that stores why it fired
- Fingerprint deduplication and cooldown so one condition is one evolving
  anomaly, not one per sample
- Graph-aware incident correlation with false-merge protection and a stored
  rationale for every grouping; validated lifecycle state machine; timelines;
  structured evidence with provenance and relevance reasons; blast-radius
  classification
- Auditable suppression and maintenance windows: anomalies are recorded, never
  silently dropped; suppressions are deactivated via `PATCH`, not deleted
- Reliability metrics (MTTA/MTTR with stated definitions) and Prometheus series
</details>

<details>
<summary><b>Root Cause &amp; Causal Analysis (Phase 4)</b></summary>

- **Evidence-driven candidates**, not a detector that guesses: bounded
  generation from anomalies, failing span trees, the dependency graph and change
  events — one hypothesis per component, no type assumed before the evidence is
  read
- **Direction from stored spans.** A failing child span inside a parent span is a
  record of the call that failed; propagation order is derived from when failures
  concluded, so nested failures cannot be inverted
- **Temporal analysis** with `before ≠ caused` enforced as code: precedence,
  gaps, simultaneity, persistence, recovery ordering
- **Change analysis that refuses a late deployment.** A change after onset is a
  `TEMPORAL_CONTRADICTION`: penalised, and never given a causal edge
- **A causal graph where every edge names the facts that justify it**, and a
  validator that rejects impossible arrows
- **Deterministic scoring** (temporal, trace, dependency, propagation, change,
  recovery, resource — minus a contradiction penalty) with **confidence kept
  separate from score**: `HIGH` / `MEDIUM` / `LOW` / `INSUFFICIENT`. No probability
  is ever emitted
- **Alternative hypotheses** shown with supporting *and* contradicting evidence,
  and why each one's confidence differs
- **Versioned re-analysis.** Identical evidence returns the stored analysis; new
  evidence appends a version and preserves what changed
- **An investigation workspace** — causal-graph explorer, evidence inspector
  ("why does ARGUS believe this edge exists?"), timeline synchronization, history
</details>

<details>
<summary><b>Failure Reproduction (Phase 5)</b></summary>

- **A plan you can read before anything runs.** Strategy, target component,
  services, expected behaviour derived from the incident's own signals, resource
  limits, timeout and repetitions — and planning **never executes**: an
  experiment starts only on an explicit `confirm_sandbox=true`
- **Isolated sandboxes.** Per-service processes behind POSIX limits (default) or
  per-service containers on an internal network with `--cap-drop ALL` and a
  read-only root filesystem; loopback-only sockets, sanitized environment,
  bounded CPU / memory / disk / processes / telemetry
- **Sanitization that fails safe.** Secret-named subtrees are tainted wholesale,
  PII becomes deterministic pseudonyms (structure preserved, identity
  unrecoverable), and the replay engine re-checks immediately before sending —
  a failed check sends nothing
- **Sanitized replay.** Synthetic and recorded requests, events, messages and
  trace inputs; sequential by default; relative timing preserved because a
  failure is a property of ordering, not of the clock face
- **Controlled faults.** Latency, timeout, HTTP 4xx/5xx, connection failure,
  response corruption, resource pressure, dependency unavailable — applied
  inside one sandbox, with a telemetry-derived audit of how many requests they
  actually affected
- **Explainable comparison.** Eight independence dimensions (component, error,
  latency, trace topology, log patterns, failure sequence, temporal, recovery)
  each with its stored formula; an unavailable dimension is excluded, never
  scored as dissimilarity
- **Honest verdicts.** `SUPPORTED` / `PARTIALLY_SUPPORTED` / `NOT_SUPPORTED` /
  `INCONCLUSIVE` — with environment differences, missing inputs, repeatability as
  an observation over the runs that happened, and stated limitations. A failed
  reproduction is never reported as a refutation
- **Auditable artifacts.** Content-addressed (SHA-256) and immutable after the
  experiment: plan, manifest, environment snapshots, telemetry, comparison and
  validation
- **Cleanup you can check.** Every sandbox is destroyed on every path — success,
  failure, cancellation, timeout, or a dead worker — and
  `GET /reproductions/metrics` reports orphaned sandboxes and cleanup failures
  rather than hiding them
</details>

<details>
<summary><b>AI Debugger (Phase 6)</b></summary>

- **Code intelligence over real repositories.** Validated registration (local
  paths confined to allowed roots, or git remotes) with *measured* capabilities;
  immutable snapshots pinning the exact revision, with commit metadata and
  version evidence — `RESOLVED` / `UNRESOLVED` / `UNKNOWN` are first-class
- **Incremental indexing by content hash**, never timestamps: unchanged files
  are reused with stable symbol ids, moves are detected, an unchanged revision
  re-indexes as a true no-op, and per-file commit attribution comes from the
  real VCS
- **Trace → code mapping** by confidence-ordered strategies (exact span,
  endpoint route, operation name, service heuristic, stack frame) — with the
  spans that could *not* be mapped recorded and their reasons shown
- **A debug session that reasons only over stored evidence:** bounded, redacted
  context (redaction report stored), a deterministic investigation that needs no
  model at all, and an optional model-assisted analysis over a **read-only,
  budgeted, fully recorded** tool surface. A provider failure degrades — stored
  as `DEGRADED` with its reason — never fakes a result
- **Every code claim is validated against the pinned snapshot** before it is
  displayed: only `VALID` locations are findings; rejections (`NOT_FOUND`,
  `OUT_OF_SNAPSHOT`, `LINE_OUT_OF_RANGE`, `AMBIGUOUS`, `STALE`) stay visible as
  audit, never silently dropped
- **Hypotheses ranked by validation status, not model confidence:** supporting
  and contradicting evidence with resolvable citations, a test approach when
  testable, and a recurrence count
- **Grounded follow-up questions** that cite only validated references, report
  failed citations and missing evidence, and expose the tool budget they used
- **Prompt-injection containment:** source text, commit messages and telemetry
  are delimited data; planted instructions are reported, not followed; the tool
  surface cannot mutate anything
- **A reaper for abandoned work:** sessions stuck analysing, runs stuck running
  and repositories stuck indexing after a process death are closed with honest
  terminal states
- **Metrics that measure honesty** (`/debugger/metrics`): claimed vs validated
  locations, rejected citations, degraded analyses, refused tool calls
</details>

<details>
<summary><b>Automated fix generation &amp; verification (Phase 7)</b></summary>

- **Fix hypotheses planned from evidence that already exists** — a debug
  session's validated code locations seed the scope allowlist, sensitive areas
  are excluded by default, and the category comes from the evidence text or
  stays `UNKNOWN`
- **Two generators, one contract:** a deterministic generator composes a real
  unified diff from the pinned snapshot's stored bytes, and a model-assisted
  generator receives only the scoped, redacted files. Both are parsed and
  safety-validated before anything is stored; malformed output, hallucinated
  files and out-of-scope diffs are recorded as failures, never repaired
- **Safety validation before and after application:** scope, path traversal,
  sensitive files (CI, auth, infrastructure, migrations), dependency and
  configuration changes, introduced secrets, and test tampering
  (deleted tests, weakened assertions, skips, disabled lint/typing) — a
  tampering patch never reaches a workspace
- **Disposable git workspaces:** a temp worktree on an `argus/fix/…` branch,
  one per candidate, destroyed on every path. Your repository is opened
  read-only and is byte-identical afterwards
- **A command registry, not a shell:** named entries with fixed argv, timeouts,
  environment passthrough and an offline network policy; discovery is
  marker-based, so an unknown stack is reported rather than guessed
- **A two-sided regression test derived from the patch itself:** it must fail on
  the base commit and pass on the patched tree, or it is not evidence
- **Verification levels with explicit reasons:** static → tests → reproduction →
  regression validation → `FULLY_VERIFIED`, with before/after metrics compared
  against configured thresholds and any breach refusing verification
- **`NOT_VERIFIED` is a first-class result:** a patch that builds and passes
  tests while the failure still reproduces is never "verified with caveats"
- **Artifacts are hashed and immutable after a terminal run**, stored outside
  the workspace, and exported with the §64 checklist and an explicit statement
  that nothing was merged, deployed or released
- **Human review is the end of the line:** `AWAITING_REVIEW` → approve, reject or
  regenerate. Approval requires a stored `VERIFIED` run, a recorded decision is
  final, and no code path merges or deploys
</details>

<details>
<summary><b>Predictive Reliability (Phase 8)</b></summary>

- **A forecast domain, not a number:** risk score, level, confidence,
  calibration status, data coverage, validity window, supporting evidence and
  explicit limitations — per component, prediction type and horizon, with
  revisions and deduplication
- **Feature engineering with two rules that matter more than the feature list:**
  every feature is `Optional` (a missing series is `None`, never `0.0`) and every
  feature records the table that supplied it, so each snapshot is auditable
- **Data quality is a verdict, not a default:** `GOOD` / `PARTIAL` / `POOR` /
  `INSUFFICIENT` coverage, a sample floor and a staleness check — insufficient
  evidence yields `UNKNOWN`, never `LOW`
- **Four deterministic statistical predictors** (`rolling_trend`, `ewma`,
  `threshold_trajectory`, `historical_frequency`) behind a provider-neutral
  interface, with ML families reserved and gated by data sufficiency — none
  ship enabled and there is no fake ML
- **One risk policy:** thresholds live in configuration and are applied by a
  single module, so the API, the worker, the UI and the tests cannot disagree
  about what `HIGH` means
- **Evaluation, calibration and walk-forward backtesting** with an enforced
  leakage contract: a forecast at time *T* may read only rows at or before *T*,
  splits are time-based, an unelapsed horizon has *no* outcome, and precision is
  published with its sample counts
- **Drift monitoring that flags for review and retrains nothing** — there is no
  code path from a drift record to a model change
- **Early warnings for humans only:** deduplicated, cooldown-limited, and
  acknowledge/dismiss. No rollback, deployment, scaling or automatic patch
  endpoint exists at all
- **Predictive reliability UI:** dashboard, forecast list and detail with the
  exact feature snapshot, component profiles, accuracy, backtest runner, model
  registry, and a predicted-risk overlay on the system map
- **An optional narrative layer, off by default:** the explanation is
  deterministic unless `RELIABILITY_NARRATIVE_PROVIDER` is set. `deterministic`
  composes prose from stored rows; `model` re-words it with a configured
  provider under untrusted-data delimiters, with secrets redacted and a mock
  provider treated as *none* — and any failure degrades to the stored
  explanation and says so
</details>

<details>
<summary><b>Safe Autonomous Remediation (Phase 9)</b></summary>

- **A closed action registry, not a command runner:** twelve declared actions —
  pause/resume a background job, enable/disable a feature flag, suppress a
  degraded dependency, restart, roll back a deployment or configuration, scale
  within a limit, route traffic, apply a verified patch. Each declares its
  parameters, allowed environments, risk, required permissions, verification
  plan, rollback strategy, maximum blast radius and whether it may ever run
  unattended. No action accepts a command, a shell, a script or a credential.
- **Five gates in a fixed order, each recorded either way:** validation →
  safety → policy → approval/authority → execution-time re-check. An action is
  refused with a reason, never silently dropped, and a failed safety assessment
  cannot be overridden by policy.
- **Default deny:** no policy row means `OBSERVE_ONLY`. Configuration can only
  narrow — the process's hard ceilings clamp whatever a stored policy says, and a
  process-level kill switch refuses every live effect whatever the database says.
- **Two signals decide autonomy:** an environment counts as non-production only
  when its name is in the configured allow-list *and* its declared
  `environment_type` is not `PRODUCTION`. Otherwise the action waits for a person.
- **Six regimes, all individually verified:** `OBSERVE_ONLY`, `DRY_RUN`,
  `SHADOW`, `HUMAN_APPROVAL`, `AUTONOMOUS`, `EMERGENCY_STOP`.
- **Loop protection:** bounded attempts with backoff, per-scope budgets and
  cooldowns, concurrency caps, and a circuit breaker that is unique per
  `(project, environment, action_type)` in the database.
- **Verification decides success, not the handler:** checks read real telemetry
  over a window; `NOT_OBSERVABLE` is never a pass, and a dry run is never
  reported as a verified outcome.
- **Rollback and post-mortem:** the reversal is planned before execution, its
  own verification is recorded, and a rollback of something never applied is
  refused.
- **A real control plane:** a pause is a row the ingestion worker and every
  background sweep consult before doing work, scoped per project and environment,
  and honoured on read so an expired pause holds nothing down.
- **Hash-chained, attributable audit:** every state change and gate decision
  binds its predecessor; tampering is detectable, and `GET …/audit/verify` names
  the first break.
- **A remediation console:** action list with filters and platform metrics,
  action detail with the full evidence/gate/execution/verification trail and the
  human decision controls, and a policy editor that shows the clamped values.
</details>

<details>
<summary><b>Unified Reliability Platform (Phase 11)</b></summary>

- **One derived system state, with reasons.** Every component resolves through a
  single precedence (incident → remediating → at-risk → degraded → healthy), and a
  component with *no* evidence is `UNKNOWN`, never `HEALTHY`. Each state ships the
  evidence behind it, and the precedence is tested as a table — a precedence bug is
  the one that renders a resolved incident as healthy.
- **Reliability Cases: the object a person actually works on.** `CASE-<n>`
  references, one legal-transition table shared by API and UI, a deduplicated
  timeline that records its source, and evidence *assembled at request time* from
  the phases that own the rows — so a case cannot show yesterday's anomalies while
  a component keeps degrading.
- **A workflow that is evidence-gated, not timer-driven.** Ten stages from
  `DETECTED` to `LEARNED`, each entered only when stored rows satisfy its
  precondition; stops are first-class with a reason; and a terminal run cannot be
  resurrected. `AUTHORIZED` is reached only through Phase 9's gates.
- **Objectives and error budgets that name what they measure.** An objective must
  name its metric; target bounds are per indicator family (a ratio is `0..1`, a
  latency target is milliseconds); evaluation records compliance, sample count and
  data quality; a never-evaluated objective is `UNKNOWN`; burn thresholds live in
  configuration so API, sweep and UI cannot disagree.
- **Change intelligence that refuses to equate recent with guilty:** failure rates,
  correlated incidents and graph-affected components, with temporal relevance kept
  separate from causal relevance.
- **Global search with one query language:** grouped results, the filters actually
  applied returned on the response, an honest no-match answer, an unknown filter
  reported rather than ignored, and project isolation enforced in SQL.
- **A service catalog per component** — ownership (team, contact, on-call,
  documentation), dependencies, endpoints, blast radius, objectives and operational
  state — with sections it cannot compute named instead of returned empty.
- **A data-quality center that reports and never repairs:** cross-phase consistency
  checks (an incident with no component, a prediction with no snapshot, a
  remediation with no authorization, knowledge with no evidence) become dispositions
  an operator owns. A check that *errors* is asserted to be a bug, not a finding.
- **Versioned configuration and append-only history:** a write names its scope and
  its reason, only the scopes the platform owns are writable, secrets never
  round-trip, and a rollback is a *new* version that records what it restored.
- **Notifications that cannot become a second outage:** nine kinds, deduplicated
  and cooldown-limited per subject, over a channel abstraction that says when a
  channel is not configured.
- **Platform health that includes ARGUS:** subsystems labelled required or
  optional, a readiness split, and a dependency contract stating what degrades when
  an optional subsystem is missing — plus self-monitoring thresholds for queue
  depth, ingestion failures, slow queries and pool saturation.
- **Reports and postmortems that state their limitations**, and a postmortem that
  falls back to the incident's own timeline when no case exists.
- **An AI case assistant that is off by default and grounded when on:** retrieval
  first, answers only from one case's stored rows, citations with every claim,
  facts/hypotheses/predictions labelled separately, unknowns listed, and a refusal
  for an action, an out-of-scope question or a root cause that is not a stored
  candidate. Its capability sheet is served either way.
- **Concurrency that was made real, not assumed:** one writer per project
  (`SELECT … FOR UPDATE`), deterministic sweep ordering, savepoint-isolated steps and
  atomic claims for derived-but-unique rows. Running this phase live found and fixed
  two genuine PostgreSQL deadlocks and a duplicate-key race — see the
  [delivery report](docs/phase-11-report.md#defects-found-by-running-phase-11-live-and-fixed).
</details>

<details>
<summary><b>Reliability Intelligence &amp; Autonomous Learning (Phase 10)</b></summary>

- **Experiences, not a second copy of history:** every outcome in Phases 1–9 is
  normalized into one episode carrying its failure shape, its resolution shape
  and the ids it was built from. Every query is bounded by an `as-of` cutoff, so
  a later resolution can never retroactively explain an earlier failure.
- **Nine deterministic miners, no model:** failure, remediation, regression,
  deployment, dependency, recovery, component-reliability and predictive
  patterns, plus recommendation effectiveness. The same corpus always produces
  the same patterns, which is what makes the version ledger meaningful.
- **Knowledge that states its own support:** observation count, window, support
  strength, confidence bucket, provenance mix, algorithm version, and the
  per-check result of validation — `sample_size`, `data_quality`,
  `temporal_consistency`, `stability`, `cross_component_consistency`,
  `support_strength` and `false_discovery`. A pattern that fails a check is
  recorded as a candidate with the reason, never silently dropped.
- **A lifecycle that only ever raises belief through validation:** `CANDIDATE →
  VALIDATING → VALIDATED → ACTIVE`, with a human able to lower belief at any
  point and never able to push past it. A rejected pattern stays rejected.
- **Autonomous activation is off by default** and, even when enabled, can only
  ever activate informational, high-confidence knowledge — remediation, recovery
  and predictive patterns always need a person.
- **Versioned and aged, never deleted:** re-derivation writes a version; a
  materially different conclusion supersedes the old row; knowledge nothing
  confirms for 90 days is deprecated with the date of its last confirmation.
- **Grounded search:** an answer with citations, or an explicit "no comparable
  historical case". There is no confident-sounding fallback.
- **Learned relationships (§23, §24) that never claim to be dependencies:** a
  relationship learned from co-failure carries its support and stays undirected
  when the evidence is undirected.
- **Recommendations are advice with two separate facts:** accepting one is a
  human's decision, the outcome is an observation — `EFFECTIVE`, `INEFFECTIVE`
  and `REGRESSION_CAUSING` are recorded separately from `ACCEPTED`.
- **A learning workspace:** reliability intelligence center, pattern explorer
  with the evidence behind each pattern and review controls, learned
  relationships, recommendation queue with decision and outcome panels,
  experience browser, run history with the full report, component learning
  profiles, and grounded search.
</details>

## Quick start

**Requirements:** Docker with Compose. Nothing else — no local Python or Node
needed for the containerised path.

```bash
git clone https://github.com/piyush06singhal/argus.git
cd argus
cp .env.example .env          # defaults work locally
docker compose up --build -d  # migrations + idempotent seed run on boot
```

| Service | URL | Notes |
| :--- | :--- | :--- |
| Web UI | http://localhost:3000 | System map, incidents, root cause analysis, reproduction workspace |
| API | http://localhost:8000 | OpenAPI docs at `/docs` |
| Metrics | http://localhost:8000/metrics | Prometheus format |
| PostgreSQL | `localhost:5432` | Set `DATABASE_PORT` in `.env` if 5432 is taken |

The stack seeds a deterministic demo — **ARGUS Demo Commerce** (checkout →
inventory → datastore) — with a working dependency graph, scripted telemetry,
and the incidents that Phases 3, 4 and 5 analyse and reproduce. Everything the
demo shows is derived by the real engines from that telemetry; no answer is
seeded, and no reproduction result is hard-coded.

Reproduction runs sandboxes inside the API container by default — no extra
service and no Docker-in-Docker required. To run experiments against containers
instead of processes, set `REPRO_SANDBOX_BACKEND=docker` and mount the Docker
socket into the API service; the backend refuses clearly if the daemon is
unreachable rather than silently falling back.

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

## Verification

Every number below is reproducible from a clean checkout. The live gates run
against the Docker Compose stack and assert **invariants**, not fixtures — so
they pass on repeat runs, not only on a pristine database.

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend test suite | `cd apps/api && pytest -q` | **1912 passed, 1 skipped** |
| Lint / format / types | `cd apps/api && ruff check app tests && ruff format --check app tests && mypy app` | clean (224 modules) |
| Frontend tests | `cd apps/web && npm test` | **231 passed** |
| Frontend type check | `cd apps/web && npx tsc --noEmit` | clean |
| Frontend lint | `cd apps/web && npx next lint --dir app/platform` | clean |
| Frontend production build | `cd apps/web && npm run build` | succeeds, 65 routes |
| Phase 0/1 live gate | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 live gate | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Phase 3 live gate | `bash infrastructure/e2e-smoke-phase3.sh` | **103/103** |
| Phase 4 live gate | `bash infrastructure/e2e-smoke-phase4.sh` | **70/70** |
| Phase 5 live gate | `bash infrastructure/e2e-smoke-phase5.sh` | **104/104** |
| Phase 6 live gate | `bash infrastructure/e2e-smoke-phase6.sh` | **159/159** |
| Phase 7 live gate | `bash infrastructure/e2e-smoke-phase7.sh` | **90/90** |
| Phase 7 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase7.sh` | **91/91** |
| Phase 8 live gate | `bash infrastructure/e2e-smoke-phase8.sh` | **42/42** |
| Phase 8 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase8.sh` | **43/43** |
| Phase 9 live gate | `bash infrastructure/e2e-smoke-phase9.sh` | **69/69** |
| Phase 9 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase9.sh` | **70/70** |
| Phase 10 live gate | `bash infrastructure/e2e-smoke-phase10.sh` | **87/87** |
| Phase 10 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase10.sh` | **89/89** (both Phase 10 revisions reverse and re-apply) |
| Phase 11 live gate | `bash infrastructure/e2e-smoke-phase11.sh` | **90/90** (five consecutive runs) |
| Phase 11 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase11.sh` | **91/91** (the Phase 11 revision reverses and re-applies) |
| Migration under a live pool | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase4.sh` | **73/73** |
| Fresh-database bootstrap | empty DB → `alembic upgrade head` → `seed_data.py` | 19 migrations apply from zero into 121 tables (11 for Phase 11); demo incident, its analysis, its reproduction, its forecasts, its remediations, its cases and its state are derived correctly |
| Migrations reversible | `alembic upgrade head` / `downgrade -1` on PostgreSQL 16 | verified both directions |

The Phase 4 gate also exercises the browser views and proves that several
projects can be analysed side by side without contaminating each other: a second
system's evidence-free incident stays `UNKNOWN` while another project holds a
`HIGH`-confidence analysis, and every candidate resolves to a component of the
project being analysed.

The Phase 11 gate walks the whole control plane over the real API against an
isolated scratch project, in 18 steps: ingest → detect → correlate → derived system
state → sweep opens a case → case timeline and transitions → the assistant's
capability sheet and its refusal while switched off → catalog and recorded ownership
→ objectives, evaluation and error budgets → grouped search and isolation → data
quality → versioned configuration, ledger and rollback → feature flags →
notifications → reports, metrics and improvement plan → platform health, readiness
and graceful degradation → cross-project isolation → all eleven workspace pages →
cleanup that deletes everything the run created. It fails if the pipeline produces *nothing* —
zero components, no incident, no case — rather than reporting a pass for an empty
run, and it asserts what ARGUS refuses: a missing project scope, a foreign
identifier, an unknown status, an out-of-range objective target, an objective with
no metric, a write to a scope the platform does not own.

Running it against PostgreSQL found six real defects that the unit suite could not
— two genuine deadlocks, a duplicate-key race on case references, a fatal
reference race, a failing sweep step poisoning the pass, and concurrent detection
colliding on the fingerprint registry — plus two frontend/API vocabulary mismatches
(the objective form and the configuration editor offered values the API does not
accept). All eight are fixed with regression tests; the details are in the
[Phase 11 delivery report](docs/phase-11-report.md#defects-found-by-running-phase-11-live-and-fixed).

The Phase 10 gate ingests four multi-component episodes over the real API,
correlates them into incidents, remediates and verifies one, leaves one open,
then runs the learning pipeline twice: the first run consumes the completed
outcomes and must produce real counts, the second must produce nothing new. It
then exercises the pattern explorer, the version ledger, a review decision
refused without a reason, an activation refused for a candidate, a rejection
recorded with its reviewer, grounded search with and without a comparable case,
a recommendation accepted by a named operator and given an *ineffective*
outcome, learned relationships that repeat they are not dependencies, a second
project that sees none of the first's learning, the twelve tables and seven enum
types in PostgreSQL, and the learning workspace rendering. It is self-cleaning.

It is also adversarial about its own subject: the gate fails if the pipeline
produces no knowledge, no relationship or no advice, rather than reporting a pass
for "nothing was produced".

The Phase 8 gate ingests a deterministic degradation timeline over the real API
and then exercises forecast generation, provenance and snapshot reproducibility,
the four-question explanation (which states it is not causal evidence), the risk
heatmap, the component profile, platform health, the model registry, a bounded
walk-forward backtest, leakage (an incident created *after* generation does not
rewrite a stored forecast), a drift assessment that retrains nothing, evaluation
of due forecasts, warning deduplication, and cross-project isolation. It is
self-cleaning, so it never leaves an incident behind that would mislead another
phase's gate.

The Phase 5 gate drives the whole reproduction engine through the real API —
plan → confirm → sandbox → replay → fault → capture → compare → validate →
artifacts → destroy — and additionally asserts what the engine *refuses*: a
start without confirmation, a start without a project scope, another project's
experiment, a shell-command fault target, an execution-shaped fault parameter and
a URL as a replay target. It then checks the security invariants that matter for
this phase: no credential value in an environment snapshot, no reproduction
signal in production telemetry, no sandbox left on disk, and no orphaned sandbox
or cleanup failure in the engine metrics.

## Project layout

```
apps/
  api/                    FastAPI backend (async SQLAlchemy 2.0, Pydantic v2)
    app/api/v1/routes/    REST endpoints (290 paths, 333 operations)
    app/core/             config, database, logging, dependencies
    app/models/           SQLAlchemy ORM models
    app/schemas/          request/response schemas
    app/services/         ingestion, detection, correlation, causal analysis, reproduction, code intelligence
    reproduction/         sandbox harness: runners, templates, environments, fixtures
    alembic/              reversible migrations
    tests/                unit, integration and scenario suites
  web/                    Next.js 14 + Tailwind UI
    app/system-map/       knowledge-graph explorer + impact/snapshot/quality panels
    app/incidents/        incident investigation, root cause analysis, reproduction history
    app/anomalies/        anomaly center and rules
    app/reproductions/    reproduction workspace (safety gate, live run, comparison, verdict)
    app/debugger/         AI debugger workspace (sessions, analysis audit, hypotheses, conversation)
    app/platform/         unified platform: overview, cases, catalog, objectives, changes,
                          search, data quality, governance, reports, activity, health
    lib/                  typed API client, presentation rules, pure helpers
infrastructure/
  e2e-smoke-phase{1,2,3,4,5,6,7,8,9,10,11}.sh live end-to-end gates
  graph-benchmark.py            graph performance benchmark
  anomaly-benchmark.py          detection/correlation benchmark
docs/                     architecture, data model, per-phase design + reports
docker-compose.yml        web + api + postgres + redis
```

## Design principles

- **Evidence, not conclusions.** A root-cause candidate is an evidence-supported
  hypothesis with a stated confidence and a visible list of what contradicts it.
- **Score is not confidence.** A deterministic score with published components
  sits beside a coarse confidence bucket; no probability is ever emitted.
- **Contradictions are surfaced, not smoothed.** A change that happened after
  onset reduces confidence and never becomes a causal edge.
- **A reproduction is an experiment, not a claim.** The result (what the sandbox
  observed) and the verdict (what that means for the hypothesis) are separate
  fields, and a failed reproduction is never presented as a refutation.
- **Nothing runs implicitly.** A plan executes nothing; an experiment starts only
  on explicit confirmation, with a project scope, inside a disposable sandbox.
- **Absence of evidence is not evidence of difference.** A comparison dimension
  that could not be measured is excluded from the score, not counted against the
  hypothesis.
- **Every conclusion is auditable.** Each causal edge names its stored facts, and
  a candidate's displayed counts always match the evidence shown beneath them.
- **Provenance everywhere.** Configured / Observed / Inferred is always visible;
  confidence means evidence strength, never a causality probability.
- **Reconciliation never deletes.** Disappeared relationships become `STALE`;
  detection that went quiet remains on record.
- **Exact scope where it decides data.** `environment_id=None` means
  *environment-less*, never "all environments" — one environment's telemetry can
  never fire another's anomaly or join its incident.
- **Bounded by construction.** Every traversal, window, candidate set and list is
  clamped server-side; no query scans the telemetry database for one incident.
- **Deterministic where it can be.** Same input, same output, same explanation;
  AI is an optional interpretation layer that may only re-word what the engine
  established — it cannot invent evidence, alter scores, or override a
  contradiction.

## Security &amp; trust model

ARGUS is designed to have no unrestricted capabilities: no shell or code
execution, no production database access, no credential extraction, no
infrastructure modification. External data — logs, traces, payloads — is treated
as **data, never as instructions**. Project and environment isolation is enforced
in SQL on every query; an out-of-scope identifier is a `404`, never data.

Phase 5 is the one place ARGUS *executes* something, so its boundary is stated
and tested explicitly:

- **Nothing executable crosses the API.** A client names a logical sandbox
  service, an HTTP method/path, and a *typed* fault — never a command, URL,
  image, mount or arbitrary parameter. Fault parameters reject execution-shaped
  keys, and nested structures are refused outright.
- **Never against production.** A sandbox gets no production credentials, no
  production network, no host filesystem, and no egress by default; the plan
  records `production_access: BLOCKED` and `arbitrary_commands: REJECTED`.
- **Sanitized or it does not run.** A secret-named subtree is tainted wholesale,
  PII becomes deterministic pseudonyms, and the replay engine re-derives "is
  anything sensitive still here?" immediately before sending. A failed check
  sends nothing.
- **Bounded and disposable.** CPU, memory, disk, process, wall-clock and
  telemetry ceilings; loopback-only sockets; one sandbox per repetition; every
  sandbox destroyed on every exit path, with failures reported through the
  engine metrics rather than hidden.
- **Ownership is checked, not assumed.** Starting, cancelling or retrying an
  experiment requires a matching project scope; knowing an id is not authority to
  run it.

Remediation (Phase 9) follows exactly that shape, with the registry, policy and
reversal rules added where they belong:

```
Proposal → Safety → Policy → Approval | Autonomous authority
        → Execution → Verification → Rollback if required → Audit
```

The unified platform (Phase 11) adds observability and orchestration without adding
capability: its own overview endpoint states the control-plane boundary in the
payload, the workflow reaches `AUTHORIZED` only through Phase 9's gates, governance
refuses to write any scope the platform does not own, secrets are redacted before
they can round-trip, and dashboard content is treated as *data* — never as an
instruction to the assistant or to any service.

See [docs/architecture.md](docs/architecture.md) for the full boundary and trust
model, [docs/phase-5.md](docs/phase-5.md) §6 for the reproduction boundary
specifically, and
[docs/unified-reliability-platform.md](docs/unified-reliability-platform.md) for the
control plane.

## API overview

All endpoints are versioned under `/api/v1`; interactive documentation is at
`/docs` when `API_ENVIRONMENT=development`.

| Area | Endpoints |
| :--- | :--- |
| Projects & environments | `/projects`, `/projects/{id}/environments`, `/projects/{id}/components` |
| Observability | `/observability/{events,logs,metrics,traces}`, `/observability/traces/spans` |
| Ingestion | `/ingestion/{sources,sources-health,webhook,queue,stats,dead-letter}`, `/ingestion/retention/*` |
| OTLP | `/otlp/v1/{traces,logs,metrics}` |
| Knowledge graph | `/projects/{id}/graph/*` (nodes, edges, search, paths, snapshots, diff, environments/compare, reconcile, health, discovery, data-quality, endpoints) |
| Components | `/components/{id}/{graph/*,endpoints,owner,aliases}` |
| Anomalies | `/anomalies`, `/anomaly-rules`, `/anomaly-suppressions`, `/maintenance-windows`, `/projects/{id}/anomalies/detect` |
| Incidents | `/incidents`, `/incidents/{id}/{timeline,anomalies,evidence,components,graph,summary}` and lifecycle actions |
| Reliability | `/projects/{id}/{reliability-metrics,incident-dashboard}` |
| Causal analysis | `/incidents/{id}/{analyze,causal-analysis,causal-graph,causal-chain,root-causes,hypotheses,evidence-analysis}` |
| Reproduction | `/incidents/{id}/reproductions`, `/reproductions`, `/reproductions/metrics`, `/reproductions/{id}/{plan,safety,status,inputs,telemetry,artifacts,comparison,validation,environment,faults,manifest,start,cancel,retry}` |
| Remediation | `/remediation/{action-types,actions,actions/{id},proposals,assessments,policy,policy-decisions,approvals,executions,verifications,rollbacks,audit,breakers,controls,metrics,sweep,emergency-stop}` and `/incidents/{id}/remediation` |
| Learning | `/intelligence/{health,dashboard,metrics,knowledge,knowledge/{id},knowledge/{id}/versions,knowledge/{id}/review,patterns,patterns/{id},experiences,experiences/{id},relationships,components/{id}/profile,remediation-effectiveness,remediation-effectiveness/compare,recommendations,recommendations/{id},recommendations/{id}/decide,recommendations/{id}/outcome,incidents/{id}/recommendations,learning-runs,learning-runs/{id},sweep,event-hooks,experiments,search}` |
| Platform control plane | `/platform/{overview,state,state/recompute,state/components/{id}/history,live,health,readiness,dependencies,metrics,activity,engineering,context,projects,environments/compare,dependencies,story/{correlation_id},sweep}` |
| Cases & workflow | `/platform/cases`, `/platform/cases/{id}`, `/platform/cases/{id}/{status,story,ask}`, `/platform/case-assistant` |
| Service catalog & ownership | `/platform/services`, `/platform/services/{id}`, `/platform/services/{id}/{blast-radius,ownership}` |
| Objectives & changes | `/platform/{slo,slo/evaluate,slo/{id},slo/{id}/error-budget,changes,changes/risk,changes/failure-rate}` |
| Search & data quality | `/platform/{search,search/help,data-quality,data-quality/check,data-quality/{id}/status}` |
| Governance & reports | `/platform/{configuration,configuration/rollback,feature-flags,integrations,notifications,notifications/{id}/read,reports,improvement-plan,incidents/{id}/postmortem,webhooks/{source},webhooks/requirements}` |
| Ops | `/health/{live,ready,dependencies}`, `/metrics` |

List endpoints share one pagination contract:

```json
{ "items": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 0 }
```

## Limitations

Stated plainly, because a reliability tool that overstates itself is worse than
useless:

- **No causal instrumentation.** ARGUS infers from stored telemetry. Even a
  `HIGH` confidence candidate is evidence-supported, not instrumented end to end.
- **Traces decide direction.** Without span trees, direction is a hypothesis and
  the confidence ceiling reflects it.
- **Causality does not cross project boundaries.** Each project is its own causal
  universe; many systems are analysed side by side, but not reasoned about
  together.
- **Re-analysis is idempotent, not incremental.** Unchanged evidence is
  returned as stored; new evidence produces a full deterministic pass as a new
  version.
- **Detection and inference are bounded by configuration.** A window that is too
  short hides evidence — and the analysis says so through `missing_evidence`.
- **A reproduction is an experiment under stated conditions.** It runs from
  synthetic fixtures, not from production state, and it cannot reproduce what was
  never instrumented or what depended on an external service. `SUPPORTED` means
  "consistent with this experiment's evidence", not "proven".
- **No authentication yet.** Isolation is server-side ownership validation on
  every request. Token auth and per-project authorization are on the roadmap.
- **Phase 7 stops at review, by design.** There is still no merge, no
  pull-request approval and no deployment anywhere in the codebase: a verified
  patch is a reviewed candidate, not a shipped change. Phase 9 can remediate, but
  only through its closed registry and effectively only against ARGUS's own
  runtime — it never edits your repositories, deploys, or reaches your
  infrastructure.
- **Environment classification needs configuration to be right.** A scope counts
  as non-production only when its name is in the allow-list and its declared
  `environment_type` is not `PRODUCTION`; an environment typed `DEVELOPMENT` that
  is in fact carrying customer traffic is still described as non-production.
- **Verification observes a window.** A remediation whose problem recurs after
  the verification window is not caught by that verification — the breaker and
  the post-analysis pass exist for the rest, and neither is proof.
- **Fix verification inherits your test coverage.** ARGUS adds a two-sided
  regression test derived from the patch, but it cannot know what your suite
  does not cover; the deterministic generator recognises a handful of defect
  shapes, and anything else is refused or requires a configured model.
- **Predictions are expectations, not facts.** Phase 8 ships deterministic
  baselines, not learned models: they are strong on trends and weak on
  interactions, they need enough history before they will speak at all
  (`UNKNOWN` when they do not have it), and their calibration cannot be judged
  until horizons elapsed. False positives and false negatives are both expected.
- **Remediation reaches ARGUS's own runtime.** Phase 9 executes entirely through
  a closed registry whose shipped, executable actions act on ARGUS's own control
  plane: pausing and resuming background jobs, and enabling or disabling ARGUS's
  own feature flags. Anything that would touch your infrastructure — restarts,
  deployments, scaling, traffic routing, applying a patch — is refused with
  `ADAPTER_UNAVAILABLE` until an operator configures an adapter.
- **Autonomous execution carries residual risk.** It is bounded, scoped,
  reversible where the action allows, breaker-limited, audited and revocable by
  one call, and it ships disabled (`REMEDIATION_EXECUTION_ENABLED` and a default
  regime of `OBSERVE_ONLY`).
- **Deployment and vendor operations are not started.** ARGUS proposes and can
  act on its own runtime; shipping a change to your systems remains a human or
  CD decision.
- **Learning is per project and per outcome.** Phase 10 learns from completed
  outcomes inside one project; it does not pool knowledge across projects even
  when two projects are the same software, and an episode with no recorded
  outcome teaches it nothing.
- **Excluding AI-generated evidence is a blunt instrument.** The default keeps
  unconfirmed model output out of the corpus, which also discards the hypotheses
  that were right. The alternative — learning from unverified output — is worse.
- **Sample floors are policy, not statistics.** Three observations is the
  default before a pattern may leave `CANDIDATE`; there is no false-discovery-rate
  control at the level of a journal, only a recorded count of comparisons.
- **A learned relationship is not a dependency.** It says two components failed
  together; where the evidence has no direction, the relationship stays
  undirected, and it never writes to the declared dependency or graph tables.
- **Unified state is derived, so it is as good as its inputs.** A `DEGRADED`
  component is degraded *according to stored telemetry*; where nothing was
  instrumented the state is `UNKNOWN`, which means a quiet system and an unobserved
  one look alike unless you read the evidence coverage.
- **A case is an index, not an answer.** It gathers what the phases concluded; if
  Phase 4 declined to name a root cause, the case shows the decline.
- **Phase 11 unifies surfaces, not reasoning.** Two projects are still separate
  causal universes, knowledge still does not pool across them, and cross-project
  intelligence ships off.
- **The case assistant is an explainer, off by default.** No decision path consults
  it, it cannot change state, policy or lessons, and it answers only from one case's
  stored rows.
- **Notifications are in-app by default.** `EMAIL` and `WEBHOOK` exist as channels a
  deployment must configure and test; nothing pages anyone out of the box.
- **Workflow deadlines are wall-clock.** A run whose evidence has not arrived stops
  at the configured deadline and says so — honest, but a slow upstream phase becomes
  a stopped run.
- **Sweep throughput is a deployment concern.** Writers are serialised per project
  and sweeps iterate in a fixed order, so a very large project count wants a
  deliberately raised batch size rather than an expectation that one pass finishes
  everything.

## Roadmap

| Phase | Scope |
| :--- | :--- |
| 6 | ✅ AI Debugger — code intelligence, validated code claims, grounded debugging analysis |
| 7 | ✅ Automated Fix Generation & Verification — candidates verified in isolation, never auto-applied |
| 8 | ✅ Predictive Reliability — evidence-backed forecasts, evaluation, backtesting, drift, warnings |
| 9 | ✅ Safe Autonomous Remediation — registry-gated proposals, approval or policy-scoped autonomy, verification, rollback, audit |
| 10 | ✅ Reliability Intelligence & Autonomous Learning — experiences, nine miners, validated versioned knowledge, grounded search, recommendations |
| 11 | ✅ Unified Reliability Platform — derived system state, reliability cases, evidence-gated workflow, SLOs & budgets, governance, search, platform health |
| 10 | ✅ Reliability Intelligence & Autonomous Learning — normalized experiences, nine miners, validation, versioned knowledge, human review, grounded advice |
| 11 | Full product surface — reliability command center across projects, orgs and teams; enhanced UI, reporting, insights |

See [docs/roadmap.md](docs/roadmap.md) for detail.

## Documentation

| Document | Contents |
| :--- | :--- |
| [Architecture](docs/architecture.md) | System design, boundaries, trust model |
| [Software Knowledge Graph](docs/software-knowledge-graph.md) | Overlay design, node/edge semantics, provenance, reconciliation |
| [Phase 3 — Anomaly & Incident Intelligence](docs/phase-3.md) | Detectors, baselines, correlation, lifecycle, evidence model |
| [Phase 4 — Root Cause & Causal Analysis](docs/phase-4.md) | Causal model, evidence model, analyzers, scoring, confidence, graph, frontend |
| [Phase 5 — Failure Reproduction Engine](docs/phase-5.md) | Architecture, sandbox design, security model, lifecycle, replay, faults, capture, comparison, validation, artifacts, cleanup, limitations |
| [Phase 6 — AI Debugger](docs/phase-6.md) | Code intelligence, snapshots, trace→code mapping, debug sessions, validation, grounded Q&amp;A, safety, limitations |
| [Phase 7 — Automated Fix Generation & Verification](docs/phase-7.md) | Fix hypotheses, patch generation, safety validation, isolated workspaces, command registry, verification ladder, risk, artifacts, human review, limitations |
| [Phase 8 — Predictive Reliability](docs/predictive-reliability.md) | Forecast domain, feature engineering, predictors, risk policy, lifecycle, backtesting, leakage prevention, calibration, drift, warnings, API, UI, limitations |
| [Phase 9 — Safe Autonomous Remediation](docs/safe-autonomous-remediation.md) | Action registry, state machine, gate-by-gate pipeline, blast radius and canary, failure containment, the control plane, API, UI, what it deliberately does not do, limitations |
| [Phase 10 — Reliability Intelligence & Autonomous Learning](docs/reliability-intelligence.md) | Experience model, the nine miners, validation, the API, the learning UI, honest limitations |
| [Learning governance](docs/learning-governance.md) | Provenance classes, lifecycle, validation checks, human review, autonomous activation, ageing, versioning, what governance deliberately leaves out |
| [Phase 2 Report](docs/phase2-implementation-report.md) · [Phase 3 Report](docs/phase3-implementation-report.md) · [Phase 4 Report](docs/phase4-implementation-report.md) · [Phase 5 Report](docs/phase5-implementation-report.md) · [Phase 6 Report](docs/phase6-implementation-report.md) · [Phase 8 Report](docs/phase-8-report.md) · [Phase 9 Report](docs/phase-9-report.md) · [Phase 10 Report](docs/phase-10-report.md) | Delivery summaries, gate evidence, bugs found by live validation |
| [Data model](docs/data-model.md) | Tables, relationships, enum domains, indexes |
| [Observability model](docs/observability-model.md) | Signals, normalization, retention |
| [Development](docs/development.md) | Local setup, migrations, testing conventions |

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. `cd apps/api && pytest -q` — the backend suite must stay green.
2. `cd apps/api && ruff check app tests && ruff format --check app tests && mypy app`
3. `cd apps/web && npx tsc --noEmit && npm test && npm run lint`
4. If you touch a phase's behaviour, run its live gate
   (`bash infrastructure/e2e-smoke-phase<N>.sh`) against the compose stack.

Conventions worth preserving: exact environment scoping, reversible migrations,
bounded queries, and the rule that new user-visible conclusions ship with their
evidence.

## License

No license file is present yet, so all rights are reserved by the author. If you
intend to use, fork or redistribute ARGUS, please open an issue to ask for a
license to be added first.
