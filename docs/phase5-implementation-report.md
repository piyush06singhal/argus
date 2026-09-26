# Phase 5 — Completion Report

> **Snapshot, not current state.** This is the report written when the phase
> shipped, and its numbers are from that run. The authoritative, current
> verification matrix lives in the [README](../README.md#verification);
> nothing here is kept in sync with later work.


**Failure Reproduction, Incident Replay & Hypothesis Validation**

Status: **complete** — implemented end to end, verified live against the compose
stack, all gates green.

---

## 1. Implemented

**Engine (17 services + harness)**

| Area | Delivered |
| :--- | :--- |
| Orchestration | `reproduction_orchestrator` (lifecycle, per-repetition execution, cancellation, deadline, always-cleanup), `reproduction_state` (the only definition of legal transitions), `reproduction_sweep` (reaper) |
| Planning | `reproduction_planner`, `reproduction_context`, `reproduction_expectations` (expected behaviour derived from the incident's own signals) |
| Isolation | `reproduction_sandbox` (LOCAL_PROCESS + DOCKER backends, POSIX/Docker limits, loopback-only, sanitized env, naming, metrics) |
| Execution | `replay_engine` (validation gate, modes, relative timing, per-item recording), `fault_injection` (typed faults, triggers, telemetry-derived impact audit) |
| Evidence | `telemetry_capture` (namespaced, expectation-labelled, bounded), `environment_snapshot` (sanitized, severity-scored differences), `reproduction_artifacts` (content-addressed, immutable) |
| Judgement | `reproduction_comparator` (8 dimensions, per-dimension formulas), `hypothesis_validator` (4 outcomes, determinism, limitations) |
| Safety | `reproduction_sanitizer` (fail-safe config + input sanitization, independent pre-flight check) |
| Harness | `reproduction/runners/service_runner.py`, `reproduction/templates/demo_commerce.json`, `reproduction/environments/`, `reproduction/fixtures/` |
| Data | `models/reproduction.py` (12 tables), `schemas/reproduction.py`, migration `a2b3c4d5e6f7_phase5_reproduction.py` |
| API | `routes/reproduction.py` — 18 paths, incl. `POST /incidents/{id}/reproductions`, start/cancel/retry, and the reading surface (plan, safety, status, inputs, telemetry, artifacts, comparison, validation, environment, faults, manifest, metrics) |
| Worker | `reproduction_run` job kind in `worker_runner.py`, `enqueue_reproduction_run` in `queue.py` |
| Observability | reaper wiring in the app lifespan, `REPRO_*` configuration, `/reproductions/metrics` |
| Frontend | `lib/reproduction.ts` + typed API client, `/reproductions` index, `/incidents/{id}/reproductions` history, `/reproductions/{id}` workspace (safety gate, live polling, comparison, validation, artifacts), sidebar + incident/causal-analysis links |
| Docs | `docs/phase-5.md`, this report, README/roadmap/data-model updates |

## 2. Architecture

`Incident → Phase 4 hypothesis → plan → safety validation → sandbox → replay +
faults → capture → compare → validate → artifacts → destroy`, driven
asynchronously by the existing Redis worker. See
[docs/phase-5.md](phase-5.md) §1 for the component map.

## 3. Sandbox

- **Isolation:** per-service processes in their own process group (default) or
  per-service containers on an internal network (`--cap-drop ALL`,
  `no-new-privileges`, read-only root fs) when Docker is selected.
- **Limits:** CPU seconds, memory, disk, process count, file size, open files,
  wall-clock deadline, telemetry bytes — all configurable, conservative defaults.
- **Network:** `ISOLATED` by default; loopback-only sockets; no proxy variables in
  a sandbox's environment; optional allow-listed egress.
- **Cleanup:** unconditional. Orchestrator `finally` + reaper; failures recorded
  and reported, never silently leaked.

## 4. Replay

Synthetic and recorded HTTP requests, events, messages and trace-derived inputs;
`SEQUENTIAL` default with `PARALLEL`/`TIMED`/`BURST`/`RATE_LIMITED` available;
relative timing preserved with original timestamps kept as provenance; every item
validated immediately before transmission and every outcome recorded.

## 5. Fault injection

Eight typed faults, three drivable triggers, targets resolved through the sandbox
handle, injection via a typed spec file re-read per request by the target
service, and an audit whose "requests affected" figure is derived from captured
telemetry rather than from ARGUS's own counter.

## 6. Comparison

Eight independence dimensions (component, error, latency, trace topology, log
pattern, failure sequence, temporal, recovery) scored separately with their
formulas stored alongside; unavailable dimensions are excluded rather than scored
as dissimilarity; the result is a bucket plus dimension scores — never a
percentage.

## 7. Validation

`SUPPORTED` / `PARTIALLY_SUPPORTED` / `NOT_SUPPORTED` / `INCONCLUSIVE`, each with
supporting and contradicting observations, environment differences, missing
inputs, determinism (a rate over the runs that happened), and limitations. A
failed reproduction is never reported as a refutation.

## 8. Security

Documented in full in [docs/phase-5.md](phase-5.md) §6. Summary: no arbitrary
execution, no production access, no credentials in a sandbox or a snapshot,
allow-listed logical targets only, project-scoped execution, sanitization that
fails safe, and an independent pre-flight check inside the replay engine.

## 9. Testing

| Gate | Result |
| :--- | :--- |
| Backend suite | **866 passed** (103 Phase 5 tests across 6 suites) |
| Lint / format / types | `ruff check`, `ruff format --check`, `mypy app` — clean (161 files, 116 modules) |
| Frontend tests | **82 passed** (27 Phase 5) |
| Frontend type check / build | clean; production build succeeds, 24 routes |
| Phase 5 live gate | `bash infrastructure/e2e-smoke-phase5.sh` — **104/104** |
| Phase 1 live gate | **46/46** |
| Phase 2 live gate | **28/28** |
| Phase 3 live gate | **103/103** |
| Phase 4 live gate | **70/70** |
| Migrations | 9, reversible on PostgreSQL 16 |

## 10. Demo

All three scenarios run through real sandboxes in `tests/test_phase5_demo.py` and
are exercised live by the gate:

- **Successful reproduction** — datastore `LATENCY` fault; the sandbox produces
  the same `datastore → inventory → checkout` failure sequence as the incident;
  verdict `SUPPORTED`.
- **Counterexample** — deployment hypothesis, un-injected baseline; the failure
  does **not** appear; verdict `NOT_SUPPORTED`/`INCONCLUSIVE`.
- **Intermittent** — probabilistic `HTTP_5XX` at `intensity 0.25` over 3
  repetitions; behaviour classified from the runs that happened, with an explicit
  "not a probability" note.

## 11. Bugs found by live validation (and fixed)

Live end-to-end runs — not the unit suite — found four defects, each now covered
by a regression test:

1. **500 on any explicitly requested fault.** `BaseSchema` sets
   `use_enum_values=True`, so a request model hands back the plain value
   (`"LATENCY"`); the planner override builder read `.value` and raised
   `AttributeError`. The supported path of §21 was unreachable from the API.
2. **Undrivable fault triggers were not actually rejected.** The same enum/str
   mismatch made `trigger is FaultTrigger.MANUAL` never match, so the validator's
   guard was decorative and a `MANUAL` trigger reached the planner.
3. **`completed_runs` was never written.** The column the history and live views
   report progress with was always `0`, so every experiment claimed "0 of 1 runs"
   even after completing. It is now incremented in the same transaction that
   finalizes a run, with a SQL-level `+ 1` (no lost increment, correct value even
   if the process dies mid-experiment).
4. **Run durations were always `0 ms`.** The duration was computed after the run
   row had already been finalized. It is now stamped before finalizing and
   restamped once teardown completes, so a completed run reports real time.

Two further issues were found and corrected in the frontend:

5. **Similarity dimension labels fell back to raw enum names.** The comparator
   stores dimensions keyed `ERROR`, `FAILURE_SEQUENCE`; the label map was
   lower-case, so the UI showed `FAILURE_SEQUENCE` instead of "Failure sequence".
6. **Determinism rendering expected the wrong keys** (`failures` / `repetitions`
   instead of the real `runs` / `reproduction_rate` / `classification`), so the
   repeatability line rendered "not measured" while the raw payload sat beside it.

## 12. Limitations

See [docs/phase-5.md](phase-5.md) §19 — missing state, missing telemetry,
environment mismatch, timing sensitivity, non-determinism, unavailable external
dependencies, unknown hidden dependencies and insufficient instrumentation. A
failed reproduction is not proof that a hypothesis is wrong, and the record
states which of these applied.

## 13. Next phase

**Phase 6 — AI Debugger.** Not started, and deliberately not scaffolded here.
