# Phase 8 Implementation Report — Predictive Reliability

> **Snapshot, not current state.** This is the report written when the phase
> shipped, and its numbers are from that run. The authoritative, current
> verification matrix lives in the [README](../README.md#verification);
> nothing here is kept in sync with later work.


**Status: complete — implemented, tested, documented, validated live.**

Phase 8 adds evidence-backed reliability forecasting to ARGUS: which components
are showing increasing reliability risk, over which horizon, on what evidence,
and how trustworthy the answer is. It predicts, explains, evaluates, backtests,
warns and stops. It does not remediate anything.

---

## 1. Executive summary

ARGUS could already observe, correlate, explain, reproduce, locate a fault, and
propose and verify a fix. Phase 8 adds the forward-looking layer:

* A **forecast domain** — risk, level, confidence, calibration, coverage,
  validity window, supporting evidence and limitations, per scope × prediction
  type × horizon, with revisions and deduplication.
* A **feature-engineering engine** that derives ~60 features per component from
  telemetry, anomalies, incidents, the knowledge graph, deployments,
  configuration changes, RCA results, reproductions, code churn and verified
  fixes — every one optional, every one sourced.
* **Four deterministic baseline predictors** behind a provider-neutral
  interface, with a data-sufficiency gate for reserved ML families that none
  ship enabled.
* A **centralized risk policy**, so "HIGH" means one thing everywhere.
* **Evaluation, calibration and walk-forward backtesting** with an enforced,
  stated leakage contract.
* **Drift monitoring** that flags for review and never retrains or activates.
* **Deduplicated, cooldown-limited early warnings** for humans only.
* An **optional narrative layer** on the explanation — deterministic by default,
  with the model path gated, redacted, delimited and degrading to the stored
  explanation on any failure.
* An **API surface** (24 endpoints), a **scheduled sweep**, queue and worker
  integration, and retention that outlives the forecasts it describes.
* A **UI** — dashboard, forecast list and detail, component profile, accuracy,
  backtests, model registry and a predicted-risk overlay on the system map.

## 2. Architecture

Phase 8 introduces no new evidence source and no duplicate infrastructure. It
reads what Phases 0–7 already persisted and reuses the Redis queue, worker
runner, retention sweeper, `project_id` scoping idiom and bounded-query rule.

```text
stored evidence (Phases 1–7)
        ↓
ReliabilityFeatureEngine  ─► FeatureBundle (optional values + sources)
        ↓
quality & staleness gate  ─► INSUFFICIENT ─► UNKNOWN, limitations stated
        ↓
predictors (rules → SignalDraft)  ─► predictive_signals
        ↓
risk policy ─► forecast row + feature snapshot + dedup/revision registry
        ↓
evaluation & calibration · backtest · drift · early warnings
        ↓
API ─► Next.js dashboard, heatmap, profile, accuracy, models, graph overlay
```

Ten tables, one migration (`d5e6f7a8b9c0`, down-revision `c4d5e6f7a8b9`), with
Phase 8-specific enum type names to avoid colliding with the earlier phases'
global `risklevel` and `risksignaltype` domains.

## 3. Feature engineering

`ReliabilityFeatureEngine` is the phase's largest component (~1,440 lines). Two
design rules dominate it:

1. **Every feature is `Optional`.** A missing series is `None`, never `0.0` —
   "no error-rate series exists" is not "the error rate is zero".
2. **Every feature records its source**, so the stored snapshot is auditable and
   the explanation cites observability rather than asserting.

Feature families: latency (p50/p95/p99, slope, volatility, tail frequency,
dependency latency trend — slopes normalized **per hour**, not per sample);
errors (span error rate and trend, error-rate change, log counts and ratios,
burst frequency, unique and repeated patterns); anomalies (frequency per hour,
density, repetition); incidents (open count, frequency trend, recurrence, time
since last, resolution time and its trend); dependencies (count, depth,
degraded, critical, failure frequency, upstream risk, downstream impact);
change (deployment frequency, failure frequency, size, lines, services, files,
rollbacks, code churn, recent changes, high-risk changes, regression signals,
verified and failed fixes); resources (saturation rate and count); and
sufficiency (metric/series/span/log counts, staleness).

Toward the end of the phase, live validation surfaced a real defect: a single
metric sample sat below the five-sample floor yet produced a `LOW` level —
exactly the false comfort the phase forbids. The quality gate was tightened so
insufficient evidence yields `UNKNOWN`.

## 4. Prediction models

| Model family | Status | Notes |
| :--- | :--- | :--- |
| `ROLLING_TREND` | shipping | Recent-window direction and magnitude |
| `EWMA` | shipping | Smoothed level versus longer baseline |
| `THRESHOLD_TRAJECTORY` | shipping | When a configured ceiling is crossed inside the horizon |
| `HISTORICAL_FREQUENCY` | shipping | Historical pace, and whether it is increasing |
| `LOGISTIC_REGRESSION`, `GRADIENT_BOOSTED_TREES`, `TIME_SERIES`, `SURVIVAL` | reserved | Registered behind the same contract; only usable when the data-sufficiency gate passes; none enabled |

Predictors are rule sets: rules matched against the feature bundle emit signal
drafts, which are stored and cited. Projection is bounded by
`RELIABILITY_MAX_PROJECTION_GROWTH`. There is no fake ML — no model without the
training data to justify it.

## 5. Forecast lifecycle

Eligible scope → feature bundle → quality/staleness gate → predictor → signals →
risk score and level (centralized policy) → confidence, calibration status and
coverage → **feature snapshot written before the forecast row** → dedup/revision
bookkeeping → validity window from the horizon. A horizon lapsing scores an
outcome once, and evaluation consumes it. Repeated passes extend or revise the
current row rather than piling up duplicates, and the previous forecast is kept
as the "why did risk change" reference.

## 6. Backtesting

`POST /reliability/backtests` runs a **walk-forward** replay: split at a
training window, forecast at the boundary, observe the horizon, score the
outcome, step forward. Splits are time-based, never random. Each stored backtest
carries its steps and configuration, so a result can be reproduced rather than
trusted.

## 7. Leakage prevention

The contract is code and is stated in the response: a forecast made at time *T*
may read only rows at or before *T*. The live gate proves the practical half —
creating an incident *after* generation did not rewrite the stored forecast, and
an unelapsed horizon reports **no** outcome rather than a null one.

## 8. Calibration

`ReliabilityEvaluationRun` scores elapsed forecasts exactly once, as
`TRUE_POSITIVE`, `FALSE_POSITIVE`, `TRUE_NEGATIVE`, `FALSE_NEGATIVE` or
`INCONCLUSIVE`. Calibration status compares predicted bands against observed
frequency; accuracy is always published with its sample counts, because
precision without a denominator is a claim rather than a measurement.

## 9. Drift monitoring

Feature, prediction, outcome, calibration and data drift are assessed and
stored with a worst status (`STABLE` / `WATCH` / `FLAGGED`) and a review policy.
Drift **flags for review**; there is no code path from a drift record to a
retrain or a model activation, and the gate asserts exactly that.

## 10. UI

| Route | Purpose |
| :--- | :--- |
| `/reliability` | Dashboard — current risk, emerging warnings, component tallies |
| `/reliability/forecasts` | Forecast list with band, horizon, coverage, confidence |
| `/reliability/forecasts/[id]` | Explanation, signals (observed vs baseline), the exact snapshot, limitations |
| `/reliability/components/[componentId]` | Component reliability profile and forecast history |
| `/reliability/accuracy` | Outcomes, precision, calibration and sample counts |
| `/reliability/backtests` | History and a form to run a walk-forward replay |
| `/reliability/models` | Model registry — families, versions, statuses |
| `/system-map` | Predicted-risk overlay on the knowledge graph |

Presentation rules live in `lib/reliability.ts` and are unit-tested: risk is a
band with uncertainty, a level without a score renders as a band, and `UNKNOWN`
renders as "insufficient evidence" rather than green.

## 11. Security

Every read requires a `project_id` and a foreign id answers `404`. The model
registry publishes no customer evidence. No remediation endpoint exists —
warnings are acknowledge/dismiss only. Prompt-injection protections apply
wherever an optional AI explanation touches untrusted evidence text. No
autonomous action of any kind ships: no rollback, deployment, scaling,
production configuration change or automatic patch.

## 12. Testing

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend suite | `pytest -q` | **1234 passed** |
| Phase 8 backend tests | `pytest tests/test_phase8_*.py -q` | **141 passed** |
| Lint | `ruff check app tests` | clean |
| Types | `mypy app` (incl. all Phase 8 modules) | clean |
| Frontend tests | `vitest run` | **132 passed** (19 for `lib/reliability.ts`) |
| Frontend type check | `tsc --noEmit` | clean |
| Frontend build | `next build` | succeeds, 36 routes incl. 7 reliability routes |
| Phase 8 live gate | `bash infrastructure/e2e-smoke-phase8.sh` | **42/42** |
| Phase 1 live gate | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 live gate | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Phase 4 live gate | `bash infrastructure/e2e-smoke-phase4.sh` | **70/70** |
| Phase 5 live gate | `bash infrastructure/e2e-smoke-phase5.sh` | **104/104** |
| Phase 6 live gate | `bash infrastructure/e2e-smoke-phase6.sh` | **159/159** |
| Phase 3 live gate | `bash infrastructure/e2e-smoke-phase3.sh` | **103/103** |
| Phase 7 live gate | `bash infrastructure/e2e-smoke-phase7.sh` | **90/90** |

Backend test areas: feature engineering and quality gates, forecast generation
and dedup/revision, leakage and unelapsed horizons, evaluation and lifecycle,
drift, warnings and cooldown, API scope and shape, the optional narrative layer
(injection handling, redaction, mock-provider refusal, degradation) and the
deterministic demo scenarios.

### Defects found by validation and fixed

1. **Below-floor samples produced `LOW`.** Insufficient evidence reported as low
   risk — fixed by tightening the quality gate to `UNKNOWN`.
2. **Slope normalized per sample step**, so the risk score depended on the scrape
   interval — fixed to normalize per hour.
3. **A `datetime` leaked into the JSON feature snapshot** — fixed at the
   boundary so the snapshot is plain JSON.
4. **Revision/dedup invariant and lookup key** were wrong in the first
   implementation — corrected before the suite went green.
5. **Request-body enums arrived as strings** (`use_enum_values`) — coerced at
   the API boundary.
6. **The Phase 8 live gate poisoned earlier phases.** It created a
   "smoke leakage probe" incident in the seeded demo project, and Phases 4–6
   smoke tests select the demo project's *newest* incident — so running Phase 8
   first made Phase 4/5/6 fail against an evidence-free incident. The gate is
   now self-cleaning and removes the probe and scratch component it created.
7. **A component-scoped forecast that outlived its component became a nameless
   heatmap cell** (the FK is `SET NULL` by design, to preserve history). The
   heatmap now omits unattributable rows instead of presenting a nameless
   column, and a regression test pins it.
8. **The explanation schema declared `ai_narrative` but nothing ever set it**, so
   the optional-narrative requirement was documented rather than implemented.
   It is now a real, provider-gated layer with injection handling, redaction,
   mock-provider refusal and degradation — all unit-tested.
9. **The Phase 3 gate silently depended on having just seeded.** It hard-coded a
   24-hour window, so on a stack seeded more than a day earlier the seeded
   anomalies fell outside it and two checks failed for a reason unrelated to the
   engine. The window is now derived from the oldest stored anomaly, which keeps
   the assertion exactly as strict while removing the hidden clock dependency.

Items 6 and 7 were found only by running the gates in sequence against a shared
stack — which is why the phase was not declared complete on unit tests alone.

## 13. Demo

`bash infrastructure/e2e-smoke-phase8.sh` runs the whole pipeline over the real
HTTP API against the compose stack. It ingests a deterministic 12-sample rising
p95 series (420 → 730 ms, 5-minute steps) for a scratch component, then:
generates forecasts and checks per-component honesty; checks provenance,
limitations and snapshot reproducibility; checks the explanation answers the
four questions and states it is not causal evidence; checks the heatmap, the
component profile (and that opening it changes nothing), and health (counts,
sample sizes, thresholds, limits, drift policy); checks the model registry leaks
no customer evidence; runs a bounded walk-forward backtest; proves an incident
after generation does not rewrite a stored forecast; runs a drift assessment and
proves it retrains nothing; evaluates due forecasts once and shows an unelapsed
horizon has no outcome; shows warnings are deduplicated and floored at `HIGH`;
proves cross-project isolation; verifies all ten tables and the forecast
indexes exist; and finally cleans up after itself.

## 14. Limitations

* **Baseline models dominate.** Strong on trends, weak on interactions, and they
  degrade when behavior changes shape rather than level.
* **Insufficient history is common and honest.** `INSUFFICIENT` coverage and
  `UNKNOWN` levels are the designed response, not a failure mode.
* **False positives and false negatives are both expected**, which is why
  precision ships with sample counts.
* **Calibration needs elapsed horizons**; until then it is `UNKNOWN`.
* **Architecture changes break comparability** across the change boundary;
  drift flags the symptom rather than repairing history.
* **Telemetry quality bounds everything** — sparse or late telemetry surfaces as
  staleness and low coverage, not as a confident answer.
* **A prediction is not a fact.** It is the most evidence-supported expectation
  available at generation time, with its evidence and limitations attached.

## 15. Next phase

**Phase 9 — Safe Autonomous Remediation.** Phase 9 will build controlled
remediation workflows — proposal → verification → policy check → approval →
execution — on top of the prediction, explanation and warning intelligence
created here. Phase 8 provides the forecast and the warning, and stops before
the action: `Predict → Explain → Evaluate → Warn → Human decides`.
