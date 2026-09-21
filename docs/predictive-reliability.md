# Phase 8 — Predictive Reliability

> **Observe · Engineer features · Forecast · Explain · Evaluate · Backtest · Warn**
>
> Phase 8 does **not** remediate anything. It answers *which components are
> showing increasing reliability risk, over which horizon, on what evidence, and
> how trustworthy that answer is* — and then stops, at a human decision.

Phases 3–7 answer *what is happening*, *why*, *can it be reproduced*, *where in
the code*, and *what change fixes it*. Phase 8 turns that history forward in
time. The distinction the whole phase is built around:

```text
Prediction ≠ Fact ≠ Anomaly ≠ Incident ≠ Causation
```

Every table, endpoint and label exists to keep those apart. A forecast never
creates an incident, never claims a component "will fail", and a feature that
moved a risk score is never presented as the cause of anything.

```text
Historical telemetry ─┐
Anomalies & incidents ─┤
Knowledge graph ───────┤
Deployments & config ──┼─► Feature engineering ─► Reliability dataset
RCA results ───────────┤        (§7–§18)              (§15)
Reproductions ─────────┤                                  │
Code churn ────────────┤                                  ▼
Verified fixes ────────┘                     Baseline predictors (§19–§22)
                                                          │
                                                          ▼
                                     Risk policy ─► Forecast + signals
                                        (§5)         (§2, §6, §34–§38)
                                                          │
                     ┌────────────────────────────────────┼──────────────────┐
                     ▼                    ▼               ▼                  ▼
              Evaluation (§28)     Backtest (§30)    Drift (§41)     Early warnings
              calibration (§33)    leakage (§65)     review only     (§39, §87)
                     │                                    │
                     └────────────────┬───────────────────┘
                                      ▼
                          Dashboard · heatmap · profile
                          accuracy · models · graph overlay
```

---

## 1. Where Phase 8 sits

ARGUS already stores everything a forecasting system needs: telemetry with
timestamps, a component graph, incident and anomaly history with outcomes,
causal analyses, reproductions, code-index history and verified fixes. Phase 8
adds **no new evidence source** — it reads what Phases 0–7 already wrote and
derives predictions from it. That is deliberate: a prediction whose inputs
cannot be pointed at is indistinguishable from a guess.

Two consequences shape the design:

* **No duplicate infrastructure.** Forecasting reuses the Redis queue, the
  worker runner, the retention sweeper, the request-scoped `project_id` idiom
  and the `bounded query` rule from earlier phases.
* **Nothing is seeded.** The demo's forecast is computed by the same code path
  the API uses, from telemetry the demo actually ingested.

## 2. Domain model

Ten tables, created by one migration (`d5e6f7a8b9c0`):

| Table | Holds |
| :--- | :--- |
| `reliability_forecasts` | One forecast per scope × type × horizon × revision: risk score, risk level, confidence, calibration status, coverage, validity window, headline, supporting evidence and limitations |
| `predictive_signals` | The individual trends that moved the score, with observed vs baseline values, trend and change rate, each bound to evidence ids |
| `forecast_feature_snapshots` | The exact feature vector, window and schema version a forecast was built from — the reproducibility record |
| `forecast_outcomes` | What actually happened once a horizon elapsed, scored as TP/FP/TN/FN/INCONCLUSIVE |
| `forecast_fingerprints` | The dedup/revision registry: one logical scope, its current forecast and its revision counter |
| `reliability_model_versions` | Registered model versions with their family, status and parameters |
| `reliability_evaluation_runs` | One accuracy evaluation: window, sample counts, precision/recall, calibration |
| `reliability_backtests` | One walk-forward replay with its steps and configuration |
| `reliability_drift_records` | Feature/prediction/outcome/calibration/data drift findings, worst status and review policy |
| `reliability_early_warnings` | Deduplicated, cooldown-limited warnings for humans, with acknowledge/dismiss |

Ownership follows the Phase 2–7 pattern: project- and environment-scoped rows
use database-level `ON DELETE CASCADE`; component references use
`ON DELETE SET NULL` so deleting a component never erases forecast history.
Because a forecast that outlives its component can no longer be attributed, the
**heatmap omits such rows rather than rendering a nameless column** — the
forecast stays retrievable through the forecast endpoints.

Enum types are given Phase 8-specific names (`predictive_signal_type`,
`reliability_model_type`, …) because PostgreSQL enum types are database-global
and an earlier phase already owns `risklevel` and `risksignaltype` with
different members.

## 3. Scopes, horizons and prediction types

* **Scope** is component × environment. `eligible_scopes()` discovers
  components with telemetry inside the lookback window, bounded by
  `RELIABILITY_MAX_COMPONENTS_PER_RUN` so one sweep cannot scan a whole tenant.
  A component with no recent telemetry is *skipped*, not forecast as calm.
* **Horizons** are an enum with a real duration (`ONE_HOUR`, `SIX_HOURS`,
  `TWENTY_FOUR_HOURS`, `SEVEN_DAYS`); `RELIABILITY_DEFAULT_HORIZONS` is
  configuration, and adding a horizon is a data change rather than a code hunt.
* **Prediction types** are separate questions, never collapsed:
  `FAILURE_RISK`, `ERROR_RATE_RISK`, `LATENCY_RISK`, `AVAILABILITY_RISK`,
  `RESOURCE_EXHAUSTION_RISK`, `DEPENDENCY_FAILURE_RISK`, `REGRESSION_RISK`,
  `INCIDENT_RISK`, `RELIABILITY_DEGRADATION`.

## 4. Feature engineering

`ReliabilityFeatureEngine` is the largest piece of the phase. It builds one
`FeatureBundle` per scope from stored rows, with two properties that matter more
than the feature list itself:

* **Every feature is `Optional`.** A series that cannot be computed is `None`,
  never `0.0`. "No error-rate series exists" and "the error rate is zero" are
  different statements, and the second is a much stronger claim.
* **Every feature records its source.** `bundle.sources` names the tables that
  supplied it, so the snapshot can be audited and the explanation can cite
  observability rather than assert.

Representative features by family:

| Family | Features |
| :--- | :--- |
| Latency | `span_latency_p50/p95/p99`, `span_latency_slope`, `span_latency_volatility`, `tail_latency_frequency`, `dependency_latency_trend` (slopes normalized **per hour**, not per sample, so risk does not move with scrape interval) |
| Errors | `span_error_rate`, `span_error_rate_trend`, `error_rate_change`, `error_log_count`, `error_log_ratio`, `error_burst_frequency`, `unique_error_types`, `repeated_error_patterns` |
| Anomalies | `anomaly_frequency_per_hour`, `component_anomaly_density`, `repeated_anomaly_patterns` |
| Incidents | `open_incident_count`, `incident_frequency_trend`, `recurring_incident_count`, `time_since_last_incident_seconds`, `incident_resolution_time_average_seconds`, `incident_resolution_time_trend` |
| Dependencies | `dependency_count`, `dependency_depth`, `degraded_dependency_count`, `critical_dependency_count`, `dependency_failure_frequency`, `upstream_risk`, `downstream_impact` |
| Change | `deployment_frequency_per_day`, `deployment_failure_frequency`, `deployment_size`, `deployment_lines_added/removed`, `deployment_services_changed`, `deployment_files_changed`, `rollback_frequency`, `recent_code_churn`, `recent_code_changes`, `high_risk_change_count`, `recent_regression_signals`, `verified_fix_count`, `failed_patch_count` |
| Resources | `resource_saturation_rate`, `resource_saturated_count`, `resource_metrics_evaluated` |
| Sufficiency | `metric_sample_count`, `metric_series_count`, `span_count`, `log_count`, telemetry staleness |

### Data quality and staleness

Coverage is an explicit verdict — `GOOD` / `PARTIAL` / `POOR` / `INSUFFICIENT`
— computed from the share of dimensions that actually produced a value. Below
`RELIABILITY_MIN_SAMPLES` usable samples a series cannot carry a forecast at all.
Telemetry older than `RELIABILITY_STALE_TELEMETRY_SECONDS` before the forecast
time marks the bundle stale.

The rule this enforces: **insufficient evidence produces `UNKNOWN`, never
`LOW`.** A low score from a missing series would be false comfort, and the
quality gate refuses to produce it.

## 5. Predictors

Four deterministic, statistical predictors ship. There is no fake ML: a
"model" that had no training data would be decoration.

| Predictor | Question it answers |
| :--- | :--- |
| `rolling_trend` | Is the recent window moving, and in which direction? |
| `ewma` | What is the smoothed level versus the longer baseline? |
| `threshold_trajectory` | At the observed pace, when does a configured ceiling get crossed inside the horizon? |
| `historical_frequency` | How often has this happened before, and is that pace increasing? |

Each is a **rule set**: a predictor matches rules against the feature bundle and
emits `SignalDraft`s, which become stored `predictive_signals`. A forecast's
headline names the dominant signals; its score comes from the risk policy, and
projection is capped by `RELIABILITY_MAX_PROJECTION_GROWTH` so a steep slope
cannot produce an unbounded number.

The model interface is provider-neutral: `LOGISTIC_REGRESSION`,
`GRADIENT_BOOSTED_TREES`, `TIME_SERIES` and `SURVIVAL` are registered families
behind the same contract, gated by a data-sufficiency check
(`RELIABILITY_ML_MIN_SAMPLES`) — none ship enabled, and none can activate
without passing it and being explicitly promoted.

### Risk policy, in one place

Thresholds live in configuration (`RELIABILITY_THRESHOLD_MEDIUM/HIGH/CRITICAL`)
and are applied by a single module, so the API, the worker, the UI and the tests
cannot disagree about what "HIGH" means. `UNKNOWN` is a first-class level and
means *insufficient evidence*, not healthy.

## 6. Forecast lifecycle

```text
eligible scope
   ↓ feature bundle (window + baseline window)
   ↓ quality + staleness gate ──► INSUFFICIENT ─► UNKNOWN forecast, limitations stated
   ↓ predictor → signal drafts
   ↓ risk score → risk level (centralized policy)
   ↓ confidence + calibration status + coverage
   ↓ feature snapshot persisted  ← written BEFORE the forecast row
   ↓ dedup/revision bookkeeping  ← one current row per logical scope
   ↓ valid_from / valid_until from the horizon
   → GENERATED / ACTIVE ─► (horizon elapses) ─► outcome scored once ─► evaluated
```

Dedup means repeated passes do not pile up duplicates: a new pass either extends
the current row or records a new revision, and the previous forecast is
retained as the "why did risk change" reference. The snapshot is written first
so a reproducible record always exists for any stored forecast.

## 7. Backtesting and leakage prevention

`ReliabilityBacktest` replays history as a **walk-forward** simulation:
training window → forecast time → horizon → the outcome that followed. Splits
are time-based, never random, because shuffling temporal reliability data
manufactures information that did not exist at prediction time.

The leakage contract is enforced and stated in the response: a forecast made at
time *T* may read only rows at or before *T*. The live gate proves the practical
half of it — creating an incident *after* a forecast was generated does not
rewrite the stored forecast, and an unelapsed horizon has **no** outcome rather
than a null one.

## 8. Calibration and accuracy

`ReliabilityEvaluationRun` scores forecasts whose horizons have elapsed, exactly
once each. Outcomes are `TRUE_POSITIVE`, `FALSE_POSITIVE`, `TRUE_NEGATIVE`,
`FALSE_NEGATIVE` or `INCONCLUSIVE` — the last being important: an outcome that
cannot be judged is not silently counted as correct.

Calibration status (`GOOD` / `ACCEPTABLE` / `POOR` / `UNKNOWN`) compares the
predicted risk band against observed frequency. Accuracy is published with its
sample counts, because precision without a denominator is a claim, not a
measurement.

## 9. Drift monitoring

Drift watches the ways a forecasting system goes quietly wrong: the features
shift, the predictions shift, the outcomes shift, the calibration slips, or the
data stops arriving properly. Findings are stored with a worst status
(`STABLE` / `WATCH` / `FLAGGED`) and a review policy.

> **Drift flags for review. It never retrains and never activates a model.**

There is deliberately no code path from a drift record to a model change.

## 10. Early warnings

Warnings are derived from forecasts at or above `RELIABILITY_WARNING_MIN_RISK_LEVEL`
(default `HIGH`), **deduplicated** per scope and signal set, and rate-limited by
`RELIABILITY_WARNING_COOLDOWN_SECONDS` so a rising risk does not page someone
every sweep. A warning is a notification to a human: it can be acknowledged or
dismissed, and nothing else. The API additionally exposes no remediation route
at all, and the live gate asserts that.

## 11. API surface

```
POST   /reliability/forecasts/generate            request a forecast pass
GET    /reliability/forecasts                     forecasts (project-scoped)
GET    /reliability/forecasts/{id}                one forecast + its signals
GET    /reliability/forecasts/{id}/explanation    the §34/§51 explanation
GET    /reliability/forecasts/{id}/signals        the predictive signals
GET    /reliability/forecasts/{id}/snapshot       the exact features used
GET    /reliability/forecasts/{id}/outcome        what actually happened
GET    /reliability/heatmap                       component × horizon risk
GET    /reliability/components/{id}/profile       reliability profile
GET    /reliability/components/{id}/forecasts     that component's forecasts
GET    /reliability/signals                       signal stream, project-scoped
GET    /reliability/models                        model registry
GET    /reliability/models/{id}                   one model version
GET    /reliability/evaluations                   accuracy runs
POST   /reliability/evaluate                      score due forecasts
POST   /reliability/backtests                     run a walk-forward backtest
GET    /reliability/backtests                     backtest history
GET    /reliability/backtests/{id}                one backtest + its steps
GET    /reliability/health                        platform health
GET    /reliability/drift                         stored drift findings
POST   /reliability/drift/assess                  run a drift assessment
GET    /reliability/warnings                      early warnings
POST   /reliability/warnings/{id}/acknowledge     human acknowledges
POST   /reliability/warnings/{id}/dismiss         human dismisses
```

Every read takes a required `project_id`; a foreign id answers `404`, so a UUID
is never authority. The model registry publishes no customer evidence — only
versions, families and statuses.

## 12. Asynchronous work

Generation, evaluation, drift and sweeps run through the existing Redis queue
and worker runner, and a scheduled sweep (`RELIABILITY_SWEEP_ENABLED`,
`RELIABILITY_SWEEP_INTERVAL_SECONDS`) generates the forecasts that are due,
scores the ones whose horizons elapsed, refreshes warnings and expires what has
passed. Under `test` the sweep is skipped so tests never race a timer; the
worker and the API can both drive the same work explicitly. Retention prunes
forecasts on `RETENTION_RELIABILITY_FORECASTS` and keeps evaluation, backtest
and drift history longer (`RETENTION_RELIABILITY_EVALUATIONS`), so precision and
drift trends outlive the forecasts they describe.

## 13. UI

| Route | Shows |
| :--- | :--- |
| `/reliability` | Dashboard: current risk, emerging warnings, component tallies |
| `/reliability/forecasts` | Forecast list with risk band, horizon, coverage and confidence |
| `/reliability/forecasts/[id]` | One forecast: explanation, signals with observed vs baseline, the exact snapshot, limitation block |
| `/reliability/components/[componentId]` | Component reliability profile and its forecast history |
| `/reliability/accuracy` | Prediction accuracy: outcomes, precision, calibration, sample counts |
| `/reliability/backtests` | Backtest history and a form to run a walk-forward replay |
| `/reliability/models` | Model registry: families, versions, statuses |
| `/system-map` | Predicted-risk overlay on the knowledge graph |

The presentation layer enforces the phase's language: risk renders as a *band
with uncertainty*, a level without a score renders as a band, and `UNKNOWN`
renders as "insufficient evidence" rather than green. `lib/reliability.ts`
carries these rules so the pages cannot drift from them.

## 14. Security

* Every read is project-scoped, proven by the gate rather than trusted.
* No remediation endpoint exists; warnings are acknowledge/dismiss only.
* Prompt-injection protection applies wherever the optional narrative layer
  touches untrusted evidence text (§16): redaction, explicit delimiters, rules
  from a fixed system message only, and no mock model narrating a real system.
* The model registry exposes no customer rows.
* No autonomous action of any kind: no rollback, no deployment, no scaling, no
  production configuration change, no automatic patching. Phase 8 predicts and
  explains; a human decides.

## 15. Limitations

* **Baseline models dominate.** The four statistical predictors are honest about
  being baselines. They are strong on trends and weak on interactions, and they
  degrade when behavior changes shape rather than level.
* **History is often thin.** Many real deployments have only weeks of telemetry;
  `INSUFFICIENT` coverage and `UNKNOWN` levels are common in that state by
  design, not by failure.
* **False positives and false negatives are both expected.** Precision is
  published with sample counts precisely so a consumer can judge it.
* **Calibration needs outcomes.** Until horizons elapse, calibration is
  `UNKNOWN`; the system refuses to invent it.
* **Features assume the system's architecture is stable.** A component split or
  a renamed service breaks comparability across the boundary; drift detection
  flags the symptom rather than repairing the history.
* **Telemetry quality bounds everything.** Sparse, sampled-down or late
  telemetry shows up as staleness and low coverage, not as a confident answer.
* **Predictions are not facts.** They are the most evidence-supported
  expectation available at generation time, with the evidence attached.

## 16. Explanation and the optional narrative layer

`GET /reliability/forecasts/{id}/explanation` is **deterministic by default**.
Every number in it comes from a stored row — the signals that were persisted
with the forecast, the previous forecast, the similarity search over real
incidents — and the response says as much through `ai_narrative_provider: "none"`.

On top of that, `RELIABILITY_NARRATIVE_PROVIDER` can enable a narrative layer:

| Value | Behaviour |
| :--- | :--- |
| `none` (default) | No narrative. The API is fully deterministic. |
| `deterministic` | Prose composed from the stored explanation. No model involved. |
| `model` | The configured AI provider re-words the explanation. |

Four properties make the model path safe, and each is unit-tested:

* **The narrative is additive.** It produces prose, never a claim. The response
  is assembled from stored rows first, so a model that "decides" a component is
  `CRITICAL` changes nothing but its own sentence, which cannot reach a field
  that carries a claim.
* **Untrusted text is data.** Signal descriptions, headlines and evidence
  summaries are redacted and wrapped in `<untrusted-data>` delimiters, and the
  system prompt states that instruction-shaped text inside them is content to be
  summarised, never instructions — the same convention the Phase 6 debugger uses.
* **A mock provider is treated as no provider.** `AI_PROVIDER=mock` means *no
  model*, and fabricated prose about a real system is worse than no prose, so the
  resolver falls back to `none` and logs it.
* **Failure degrades.** A timeout, an HTTP error or an unusable answer returns
  the deterministic narrative with `ai_narrative_degraded: true`, so the
  explanation always renders and never silently implies a model spoke.

The narrative is also constrained by its prompt: it may not add facts, introduce
causes, strengthen a claim ("will fail" is forbidden), or mention limitations
that are not listed.

## 17. Next phase

**Phase 9 — Safe Autonomous Remediation.** Phase 9 turns the intelligence built
here — forecasts, warnings, causal analyses, reproductions and verified fixes —
into *controlled remediation workflows*: proposal → verification → policy check
→ approval → execution. Phase 8 deliberately provides the prediction and the
warning and stops before the action.
