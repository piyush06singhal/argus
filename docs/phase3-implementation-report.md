# Phase 3 — Implementation Report: Anomaly & Incident Intelligence

**Status: COMPLETE — all increments delivered, all gates green, validated live.**

Phase 3 turns the Phase 1 evidence stream and the Phase 2 structural graph into an
anomaly-and-incident layer: deterministic detection, evidence-based correlation, a
validated incident lifecycle, and explainable context. It reports **correlation,
never causation** — nothing in this phase claims why something happened.

---

## Gate evidence

| Gate | Command | Result |
| --- | --- | --- |
| Backend suite | `pytest -q` | **687 passed** |
| Lint | `ruff check app tests alembic` | clean |
| Format | `ruff format --check` | clean (128 files) |
| Types | `mypy app` | clean (85 source files) |
| Phase 0/1 regression | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 regression | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Phase 3 live gate | `bash infrastructure/e2e-smoke-phase3.sh` | **102/102**, three consecutive runs |
| Frontend types | `npx tsc --noEmit` | clean |
| Frontend lint | `npm run lint` | clean (one pre-existing warning in `system-map`) |
| Frontend tests | `npx vitest run` | **25 passed** |
| Frontend build | `npm run build` | success |
| Benchmark | `apps/api/.venv/bin/python infrastructure/anomaly-benchmark.py` | see output |
| Migrations | `alembic upgrade head` / `downgrade -1` on live PostgreSQL 16 | reversible |

The frontend and benchmark commands need the API virtualenv / `node_modules`; the
benchmark defaults to a temporary SQLite database so it never touches live data.

---

## What was delivered

**Detection** — `AnomalyRule`, `AnomalyBaseline`, `Anomaly`, `AnomalyObservation`,
`AnomalyFingerprint`, `AnomalySuppression`, `MaintenanceWindow`, plus
`Incident`/`IncidentEvidence`/`IncidentTimelineEvent` extensions. Nine pure,
individually testable detectors (threshold, baseline deviation, z-score, error
rate, latency p50/p95/p99, rate change, log-pattern spike, trace-failure rate,
health transition) over STATIC and ROLLING baselines with explicit
`INSUFFICIENT_DATA` handling — missing data is never treated as failure.

**Correlation** — union-find clustering over shared evidence (same component,
graph adjacency, shared deployment context), a capped cluster span so transitive
chaining cannot over-group, and a stored rationale for every grouping.

**Incidents** — a validated lifecycle state machine (409 on illegal transitions),
fingerprint-based deduplication, auto-resolve, reopen-on-recurrence, a timeline,
structured evidence with `relevance_reason`, observed blast-radius
classification, and deterministic summaries built only from stored evidence.

**API & UI** — anomaly/rule/suppression/window routes, incident intelligence
routes (timeline, anomalies, evidence, components, graph context, deployments,
configuration changes), reliability metrics, Prometheus series, an incident
dashboard, an anomaly center, an investigation page and a graph-context overlay.

---

## Bugs found and fixed during live validation

Live validation was not a formality; it surfaced seven real defects, each now
covered by a regression test.

1. **Cross-environment pooling (the serious one).** A project-scope detection
   pass read telemetry with *no* environment filter, so a span-triggered pass
   pooled production with staging and minted a second, environment-less incident
   for the same story. Scope is now exact everywhere it decides data:
   `environment_id=None` means `IS NULL`, in detection, correlation and incident
   resolution alike. `build_clusters` additionally partitions a mixed input so no
   call site can merge scopes.
2. **A per-span detection enqueue that could not matter.** No detector reads
   spans (detection consumes metrics, logs, traces and health checks), so each
   span pushed a detection job that could never change an outcome — one busy
   trace, hundreds of identical jobs. The hook is gone; spans still feed the
   Phase 2 graph.
3. **A suppression that could not be switched off.** Suppressions and windows had
   create/list only, so "auditable" meant "auditable until it gets in the way".
   Added `PATCH` deactivation for both, re-checking the window invariant
   (`422` if a patch would leave a window with no effect).
4. **Naive-vs-aware datetime comparison.** A patch validation compared a stored
   (naive, SQLite) timestamp with an aware payload and raised a 500. The same
   three-line helper was copy-pasted across six modules — and missing from the one
   that needed it. Consolidated into `app/core/time.py`.
5. **Demo incident attributed to a staging component.** The re-anchor script
   resolved components by name, and the demo topology mirrors names across
   environments — so the production incident pointed at a staging mirror.
6. **Stale demo rules that a re-seed could never repair.** Rules are keyed by
   fixed demo ids and a re-seed skipped existing ones, pinning the scenario to an
   earlier run's scope. `--force` now reconciles them.
7. **A gate that only passed once.** Sections consumed demo state (a suppression
   left active, an anomaly acknowledged twice → 409, an incident picked by list
   position). The gate now scopes its artifacts to a reused scratch project,
   deactivates what it creates, identifies the demo incident by the demo story,
   and drives lifecycle transitions on its own throwaway anomaly. Three
   consecutive runs pass with identical counts.

---

## Limitations (honest)

* Detection is bounded by configuration (`ANOMALY_MAX_TELEMETRY_SAMPLES`,
  `CORRELATION_MAX_ANOMALIES`); the benchmark measures the caps, not an
  unbounded scan.
* Correlation is heuristic and evidence-based. It can group temporally close but
  unrelated signals, and it can miss a real relationship that shares no evidence.
  This is stated in the API rationale and the UI.
* Severity and confidence are rule-derived evidence strengths, not probabilities.
* The environment-to-environment comparison inherited from Phase 2 compares
  last-seen versions only.
* No token authentication (single-tenant Phase 0–3): isolation is ownership
  validation on every request plus bounded queries, not identity.

## Deliberately not implemented (Phase 4+)

Root-cause causal inference, causal graphs, automated diagnosis, code-level root
cause detection, failure reproduction, patch generation and verification,
predictive failure forecasting, autonomous remediation, self-healing.

Phase 3 is **Detect → Correlate → Contextualize → Explain → Visualize.** It is
not Diagnose → Fix → Deploy.
