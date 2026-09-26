# Console guide

A tour of the ARGUS console: what each workspace is for, what you do there, and
what the numbers mean. The console is a client of the public API — every screen
is backed by endpoints you can call yourself ([api.md](api.md)).

> **Reading ARGUS correctly, in one paragraph.** ARGUS distinguishes three
> things everywhere: *what was observed* (telemetry, anomalies), *what was
> inferred* (correlations, causal candidates, forecasts — each with a confidence
> bucket and stated limitations), and *what was done* (reproductions, patches,
> remediations — each with evidence and a verdict). A screen that cannot
> substantiate a claim says "insufficient evidence" instead of showing a
> confident number. `UNKNOWN` always means *not enough evidence* — never
> *healthy*.

---

## Getting in

Open **http://localhost:3000**.

* **Not connected** → the header badge is red and pages report the API error.
  Go to **Connect** (`/connect`), paste a bearer token, save. The badge turns
  green when the console can actually reach the API with that credential.
* The token is held in your browser and sent with each request; it is not
  written to server logs.
* Role decides what you can do: `VIEWER` reads, `OPERATOR` also writes,
  `ADMIN` also manages tokens and policy. Controls you cannot use are not
  offered — the API would refuse them anyway.

---

## Workspaces

### Dashboard (`/`) — "what is going on right now"

Counts of projects, active incidents, deployments and events, plus a guided
empty state when the platform has no data yet. This is a status board, not an
analysis surface; everything is one click from the real workspace.

### Platform Overview (`/platform`) — the unified control plane

The whole system's reliability state in one place, derived by precedence from
everything below it: an open critical incident outranks a warning, a warning
outranks a forecast risk, and a resolved incident never renders as healthy.

* **Reliability Cases** (`/platform/cases`) — correlated incident *cases* with a
  timeline, workflow state, approvals and a case assistant. A case is the unit
  of investigation, not a single alert.
* **Service Catalog** (`/platform/services`) — every known component with owner,
  criticality and current reliability profile.
* **SLO & Reliability** (`/platform/slo`) — objectives, evaluation windows, burn
  and error budgets; evaluations state the window they measured.
* **Changes** (`/platform/changes`) — deployments and configuration changes,
  correlated with incidents. Correlation is presented as proximity, never as
  cause.
* **Global Search** (`/platform/search`) — one query across projects, components,
  incidents, cases and knowledge.
* **Reports** (`/platform/reports`) — postmortem-style reports assembled from
  stored evidence.
* **Data Quality** (`/platform/data-quality`) — gaps, staleness, duplicates and
  provenance problems in the data ARGUS is reasoning over. **Check this before
  trusting a ratio or a trend** — bad input explains most surprising output.
* **Governance** (`/platform/governance`) — policy, retention and control-plane
  state, including the kill switch.
* **Activity** (`/platform/activity`) — the event stream of what the platform
  itself did.
* **Platform Health** (`/platform/health`) — self-observability: is the learning
  layer working, are sweeps running, are queues backed up.

### Projects & System Map (`/projects`, `/system-map`)

Register projects and environments; the System Map renders the knowledge graph of
components, dependencies, endpoints and ownership, with versions you can compare
over time. Graph edges earned from traces are distinguishable from declared
ones — evidence beats configuration, but the two are never silently merged.

### Observability (`/observability/*`)

Logs, Metrics, Traces and Events — the raw ingested record, filterable and
paginated. Traces open into the full span tree; this is the evidence every
inference downstream is built on.

**Ingestion Health** (`/ingestion-health`) answers "is my data arriving?":
per-source status and error counts, queue depth, dead-letter contents, and
retention state.

### Incidents (`/incidents/*`)

* **Incidents** — everything detected, with severity, status and timeline. The
  lifecycle is explicit: detected → acknowledged → investigating → mitigated →
  resolved, plus reopen. *Mitigated* means the impact stopped; *resolved* means
  the investigation concluded. Those are different facts.
* **Incident Dashboard** — the operational view: what is open, what is noisy,
  what is oldest.
* **Root Cause Analysis** (`/incidents/rca`) — the causal workspace. You ask for
  an analysis, then read candidates with their **confidence bucket**, the
  evidence behind each, and the analysis's own **limitations**. The causal graph
  shows directed edges that are *hypotheses*, drawn from evidence (temporal
  order, dependency structure, failing traces, change proximity) — never a
  claim that correlation proves causation.
* **Incident detail** — evidence, timeline, affected components, related
  deployments, code mappings, and the actions available (investigate, mitigate,
  resolve, reopen).

### Anomaly Center (`/anomalies`)

Detected anomalies and the rules that produced them, with the metric values and
baseline that triggered each one. Rules are yours: thresholds, z-score,
baseline deviation, latency ratio, error rate, pattern spikes, trace failure
rate. A rule that could never be evaluated is refused at creation rather than
silently detecting nothing.

### Failure Reproduction (`/reproductions`)

Controlled experiments. For a hypothesis you get a **plan** (what environment,
which faults, which replay inputs), the **environment** and safety envelope, the
captured **telemetry**, a **comparison** against the original incident, and a
**hypothesis verdict**. One sandbox per repetition, never mixed, always torn
down; if isolation is unavailable, provisioning refuses rather than degrading
quietly.

### AI Debugger (`/debugger`)

Code-level investigation grounded in stored evidence: which files and symbols
participate in the failing path, what changed recently in them, which code
locations map to the failing trace, and what hypotheses follow. ARGUS does not
invent files, symbols, commits or frames — a location with no evidence is not
displayed as a finding, and a claim is always shown next to its validation
state.

### Fix & Verification (`/fixes`)

Generated patches and their verification: a patch is parsed, safety-validated
against its hypothesis's scope (with hard refusals for out-of-scope edits,
sensitive files, dependency/CI changes and introduced secrets), applied inside
an isolated workspace, built, tested, and checked for the original failure and
for regressions. The result is a **verdict with evidence**, not a promise.
Nothing is committed, pushed or deployed: that stays a human decision.

### Reliability Intelligence (`/reliability/*`)

Forecasts, models, accuracy and backtests. **Forecasts** shows predictions with
uncertainty bands and the features behind them; **Accuracy** tracks how the
predictions turned out; **Backtests** replay a model over history; **Models**
shows what is active and how it was validated. Risk is never rendered as
certainty, and drift is flagged for review rather than acted on automatically.

### Safe Remediation (`/remediation/*`)

The control plane for actions ARGUS is allowed to take.

* **Actions** — proposals with their decision record: which policy applied, which
  safety checks ran, what was approved, what was executed, and whether rollback
  is available.
* **Policy** (`/remediation/policy`) — what is permitted at all:
  `OBSERVE_ONLY` authorizes nothing, `DRY_RUN`/`SHADOW` validate without
  touching anything, `HUMAN_APPROVAL` and `AUTONOMOUS` are the live regimes.
  Default is deny; an unconfigured scope permits nothing.

Every action requires stored evidence and a passing safety assessment, and the
audit trail is a hash chain — removing an entry breaks it visibly.

### Learning Center (`/intelligence/*`)

What ARGUS has concluded from its own history, and on what basis:

* **Learned Patterns** — regularities mined from completed incidents, each with
  sample count, support and provenance. You can accept or reject a pattern;
  rejection is respected until the evidence grows substantially.
* **Relationships** — component relationships inferred from co-occurrence over
  time, with the evidence behind them.
* **Recommendations** — suggested actions derived from patterns that actually
  played out.
* **Experiences** — the normalized record of an incident: what failed, what was
  observed, what was done, what happened next.
* **Learning Runs** — each learning pass, its inputs, outputs and metrics.
* **Knowledge Search** — search across learned knowledge and experiences.
* **Component profile** — everything ARGUS knows about one component.

Nothing here is self-activating: learning produces *knowledge for a human to
review*, and the platform records who accepted or rejected it.

### Settings (`/settings`, `/settings/tokens`)

Console and platform settings. **Access Tokens** lists token *metadata* (never
secrets) with role, scope, status and last use, and lets an admin mint and revoke
tokens. Scoped tokens are the recommended shape for CI and per-team access.

### Connect (`/connect`)

Where a token is entered, validated and replaced — with the exact error the API
returned when it is wrong. "No readable repository" is information, not noise.

---

## Conventions worth knowing

| Pattern | Meaning |
| --- | --- |
| Confidence buckets (`HIGH`/`MEDIUM`/`LOW`/`UNKNOWN`) | How much independent evidence supports an inference |
| `limitations` blocks | What the analysis could not establish — shown, not hidden |
| Evidence lists | The stored rows behind a claim; a claim without them is refused |
| `UNKNOWN` | Insufficient evidence — never "healthy" |
| Verdicts next to claims | A hypothesis and its validation are separate fields |
| Refusals with reasons | A blocked action explains which gate refused it |
| Explicit "insufficient data" states | Better than a zero that looks like a measurement |

---

## Adding screenshots

This guide is written to be useful without images: every claim is tied to a
route and a behaviour, so it cannot drift the way a screenshot does. If you want
to contribute images, put them in `docs/images/` and reference them from the
relevant section — the onboarding gate
(`infrastructure/e2e-smoke-onboarding.sh`) is what keeps the *text* honest.
