# Phase 5 — Failure Reproduction Engine

> **Reproduce · Replay · Experiment · Compare · Validate**
>
> Phase 5 does **not** fix, deploy or remediate. It builds a controlled
> experiment, runs it in an isolated sandbox, and reports what that experiment
> shows — including when it shows nothing.

Phases 1–4 let ARGUS say *"here is the most evidence-supported explanation for
why this failed."* Phase 5 lets ARGUS test that explanation:

```
Incident
   ↓
Root-cause hypothesis (Phase 4)
   ↓
Evidence selection
   ↓
Reproduction plan
   ↓
Safety validation
   ↓
Isolated sandbox
   ↓
Input replay + controlled fault injection
   ↓
Captured telemetry
   ↓
Comparison with the original incident
   ↓
Hypothesis validation
   ↓
Immutable artifacts
```

The one rule that shapes every design decision below:

> **A reproduction is an experiment, not a claim.** `result` says what the sandbox
> did; `outcome` says what that means for the hypothesis. They are separate
> columns on separate tables, and nothing in the system merges them into a
> "confidence percentage".

---

## 1. Architecture

| Component | Module | Responsibility |
| :--- | :--- | :--- |
| Orchestrator | `services/reproduction_orchestrator.py` | Drives one experiment from `PLANNED` to a terminal state; owns the "always clean up" `finally` |
| Planner | `services/reproduction_planner.py` | Derives the plan: target service, expected behaviour, replay items, faults, limits |
| Context builder | `services/reproduction_context.py` | Reads the incident's own stored rows (anomalies, spans, changes, dependencies) |
| Expectations | `services/reproduction_expectations.py` | Builds expected behaviour **from the incident's evidence only** |
| Sandbox manager | `services/reproduction_sandbox.py` | Provisions, starts, inspects and destroys sandboxes; enforces limits, network policy and cleanup |
| Replay engine | `services/replay_engine.py` | Validates, sanitizes and sends replay items; records every outcome |
| Fault injection | `services/fault_injection.py` | Applies typed, bounded faults to services of the experiment's own sandbox |
| Telemetry capture | `services/telemetry_capture.py` | Reads sandbox telemetry, namespaces it, labels it against expectations |
| Comparator | `services/reproduction_comparator.py` | Scores similarity over explainable dimensions |
| Hypothesis validator | `services/hypothesis_validator.py` | Turns the comparison into `SUPPORTED` / `PARTIALLY_SUPPORTED` / `NOT_SUPPORTED` / `INCONCLUSIVE` |
| Environment snapshot | `services/environment_snapshot.py` | Captures and sanitizes the original and sandbox environments; reports differences with severity |
| Sanitizers | `services/reproduction_sanitizer.py` | Config and input sanitization, with an independent pre-flight check |
| Artifact store | `services/reproduction_artifacts.py` | Content-addressed, immutable experiment output |
| Reaper | `services/reproduction_sweep.py` | Closes abandoned experiments, destroys orphaned sandboxes |
| State machine | `services/reproduction_state.py` | The only definition of legal experiment transitions |
| Harness | `reproduction/runners/service_runner.py`, `reproduction/templates/*.json`, `reproduction/fixtures/*.json` | The sandbox side: service processes, template, synthetic fixtures |

Execution is asynchronous: `POST /start` moves the experiment to `VALIDATING` and
enqueues a `reproduction_run` job on the existing Redis queue, consumed by the
existing worker (`services/worker_runner.py`). The client polls
`GET /reproductions/{id}/status`.

```
                 INCIDENT + PHASE 4 CANDIDATE
                             │
                             ▼
                  ReproductionPlanner  ──► plan (explicit, reviewable)
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
             SandboxManager      Safety validation
        (LOCAL_PROCESS|DOCKER)   (network, limits, targets)
                    │
                    ▼
        ReplayEngine ──► sandbox services ──► faults.json (typed spec)
                    │                 │
                    │                 ▼
                    │        TelemetryCapture (§24 namespace)
                    │                 │
                    └────────┬────────┘
                             ▼
                    ReproductionComparator (dimensions)
                             ▼
                     HypothesisValidator (verdict)
                             ▼
                  ArtifactStore (hash + immutable)
                             ▼
                     Sandbox destroyed
```

---

## 2. Domain model

Nine tables, all project-scoped, all cascade-deleted with their experiment:

| Table | Holds |
| :--- | :--- |
| `reproduction_experiments` | The envelope: status, result, confidence, namespace, deadline, cancels |
| `reproduction_plans` | The explicit plan (strategy, services, expectations, constraints, limits) |
| `reproduction_hypotheses` | The hypothesis under test, copied from the Phase 4 candidate with its evidence |
| `reproduction_sandboxes` | One row per sandbox: status, backend, network policy, limits, cleanup outcome |
| `reproduction_runs` | One row per repetition: replay counters, observations, duration, failure class |
| `reproduction_inputs` | Every replay item, its sanitization record and its outcome |
| `reproduction_faults` | Every fault: type, target, trigger, activation window, requests affected |
| `reproduction_observations` | Captured, namespaced telemetry labelled against expectations |
| `reproduction_comparisons` | Per-run similarity, dimensions, formulas |
| `reproduction_validations` | The verdict, its evidence, environment differences, determinism, limitations |
| `reproduction_artifacts` | Content-addressed, immutable stored output |
| `environment_snapshots` | Original and sandbox environments, sanitized, with differences |

`result` (on the experiment and the run) and `outcome` (on the validation) are
deliberately different fields on different tables. A single collapsed
"confidence" column would make "the sandbox behaved like the incident" and "the
hypothesis is therefore true" indistinguishable at the storage layer.

---

## 3. Reproduction lifecycle

```
PLANNED ─► VALIDATING ─► PROVISIONING ─► READY ─► REPLAYING ─► RUNNING
                                                                │
                                                                ▼
                                              COLLECTING ─► COMPARING ─► COMPLETED
```

Failure exits: `FAILED`, `CANCELLED`, `TIMED_OUT`.

- Transitions are validated in one place (`reproduction_state.py`), and the API
  exposes `available_transitions` so the UI can only offer legal moves.
- `PLANNED` is not "about to run": it is a plan an engineer has not yet
  confirmed. `POST /start` requires `confirm_sandbox=true`.
- **Cancellation is cooperative** (§38). The running repetition notices between
  phases and unwinds through the same `finally` that destroys the sandbox, so the
  audit is written and nothing leaks. Killing the worker instead would strand a
  sandbox.
- **Every experiment has a deadline** (`timeout_at`), stamped by the worker when
  the run starts (not by the API request), so a crashed request cannot leave an
  experiment without one. Exceeding it stops the run, destroys the sandbox,
  collects what exists and marks `TIMED_OUT`.
- The **reaper** (`REPRO_SWEEP_ENABLED`, default on) closes experiments whose
  deadline passed with no worker driving them and destroys any sandbox belonging
  to a terminal or vanished experiment.

---

## 4. The plan is explicit

A plan answers, in stored data: what are we reproducing, why, which hypothesis,
on what evidence, with which inputs, and what behaviour should occur.

| Field | Meaning |
| :--- | :--- |
| `strategy` | `SYNTHETIC_INPUT_REPLAY`, `EVENT_REPLAY`, `DEPENDENCY_FAULT`, `CONFIGURATION_REPLAY`, `STATE_SNAPSHOT` |
| `target_component` / `target_version` | The component the hypothesis names — never chosen by the client |
| `required_services` / `required_dependencies` | What the sandbox will start, resolved through the template's alias map |
| `expected_behavior` | Derived from the incident's own signals (see §9) |
| `safety_constraints` | `production_access: BLOCKED`, `network_policy`, `arbitrary_commands: REJECTED` — enforced by code, recorded in the plan |
| `resource_limits`, `timeout_seconds`, `repetitions` | The bounds the experiment may not exceed |
| `derived_from` | Which incident rows produced each decision, plus `missing_inputs` |

Planning **never executes anything**. `POST /incidents/{id}/reproductions`
returns `201` with `status: PLANNED`, `result: NOT_RUN`, no sandbox and no runs.

---

## 5. Sandbox design

### 5.1 Backends

`REPRO_SANDBOX_BACKEND` selects one; the default is deliberately not Docker.

**`LOCAL_PROCESS` (default)** — one process per sandbox service, each in its own
process group, behind POSIX resource limits, with a sanitized environment, a
private working tree, and loopback-only sockets. It is the default because it is
always available: isolation that needs a daemon present cannot be relied on, and
a safety feature that silently degrades is not a safety feature.

**`DOCKER`** — one container per service on a dedicated **internal** Docker
network, `--cap-drop ALL`, `--security-opt no-new-privileges`, read-only root
filesystem, no host network, CPU/memory/PID limits. If the daemon is unreachable
it **fails with an actionable message** rather than falling back to
`LOCAL_PROCESS`: a silent fallback would mean the operator's isolation choice was
quietly ignored.

### 5.2 Isolation, enforced

- A fresh working tree is created *inside* the configured sandbox root
  (`REPRO_SANDBOX_ROOT`, default `<tmp>/argus-reproduction`), named
  `argus-repro-<experiment>-r<run>`.
- Services bind `127.0.0.1` only. A port reachable off-host would be a hole; the
  test suite asserts loopback reachability for every service.
- The environment handed to a sandbox carries no credential and no proxy
  variable, and does not put ARGUS's own package on `PYTHONPATH` — the sandbox
  cannot import the platform it is running under.
- Resource limits, applied in the child before `exec` so they bind from its first
  instruction: CPU seconds (`RLIMIT_CPU`), address space (`RLIMIT_AS`) and file
  size (`RLIMIT_FSIZE`), plus wall-clock, disk and telemetry-byte ceilings.
  The process-count ceiling (`REPRO_MAX_PROCESSES`) is enforced by the Docker
  backend as `--pids-limit`. The local backend cannot enforce it: Linux checks
  `RLIMIT_NPROC` against the real **UID's** total task count rather than the
  sandbox, so a low value there stops the service from creating the thread that
  answers its own health probe. The sandbox metadata records which is in force
  (`process_cap_enforced`), so the manifest never claims a limit that was not
  applied.

### 5.3 Namespaces

Every observation is stamped `repro:<experiment_id>`. Reproduction telemetry is
never written into an incident's namespace, so a reproduction can never be
mistaken for the production signal it reproduces. The live gate asserts exactly
this: no reproduction signal appears in production telemetry.

---

## 6. Security model

This phase can *execute* things, so the boundaries are stated and tested
explicitly.

### 6.1 What ARGUS will never do

- Execute a reproduction against a production database, API, network, credential
  or customer record.
- Accept a shell command, URL, image name, mount or arbitrary parameter from a
  client.
- Apply a fault outside a sandbox.
- Start an experiment implicitly, or on request without a project scope.
- Leave a sandbox, process, temporary credential or temporary file behind.

### 6.2 The API boundary

Nothing executable crosses it. A client may name:

- a **logical sandbox service** (`datastore`, not `host:port`),
- an **HTTP method and absolute path** that must not contain `..`, `//` or `:`,
- a **typed fault** whose target must match `^[a-z][a-z0-9_-]{0,63}$` and whose
  parameters reject execution-shaped keys (`command`, `shell`, `script`, `exec`,
  `argv`, `path`, `mount`, `volume`, `image`, `env`, `url`, `host`, …) and any
  nested structure.

Everything else is derived server-side from the incident.

### 6.3 Sanitization

- **Fail safe, not best-effort.** A value under a secret-named key is *tainted*:
  every scalar in that subtree becomes a placeholder, so a nested credential blob
  cannot survive as a leaf nobody checked. The only hard stop is a structure too
  deep to verify — an experiment that cannot be proven safe does not run.
- **Structure survives, identity does not.** Real identifiers become
  *deterministic* pseudonyms, so reproductions stay comparable and repeatable
  while the original value is unrecoverable from storage. PII is replaced, never
  deleted, so a request keeps its shape.
- **Redaction records contain a path and a kind — never the secret.**
- **Independent pre-flight.** The replay engine re-derives "is anything sensitive
  still here?" from the stored payload immediately before transmission; a failed
  check is a recorded `REJECTED` outcome and nothing is sent.

### 6.4 Database credentials in the demo's snapshot

The seeded demo runs with `DATABASE_PASSWORD`, `AUTH_SECRET` and friends in the
process environment. The original snapshot records only
`sanitization.dropped_keys: ["DATABASE_PASSWORD", ...]` — the key names that were
removed — and no value. The live gate asserts that no credential *value* appears
in any snapshot.

### 6.5 Injection and ownership

- Start, cancel and retry require a `project_id` query parameter that must match
  the experiment's project. Knowing a UUID is not authority to run it.
- An out-of-scope experiment answers `404`, not `403`: it is simply not visible.
- Fault targets are resolved through the sandbox handle, so a target exists only
  if that sandbox actually runs it, and the sandbox's working tree must be a live
  directory under the configured root.
- The harness is a **predefined template** (`reproduction/templates/*.json`) plus
  fixture payloads. There is no path from a request to a process argument.

---

## 7. Replay engine

**Inputs:** synthetic HTTP requests, sanitized recorded HTTP requests, events,
messages, trace-derived inputs.

**Modes:** `SEQUENTIAL` (default), `PARALLEL`, `TIMED`, `BURST`, `RATE_LIMITED`.
Concurrency is used only when explicitly configured.

**Timing is relative, never absolute** (§20). Original wall-clock timestamps are
kept as provenance only: reproducing a 14:20 incident at 09:05 must preserve the
*intervals*, because the failure is a property of ordering and distance, not of
the clock face. Each item stores `relative_offset_ms`, `original_timestamp` and
`replay_timestamp`.

**Every item is validated immediately before it is sent** — target is this
sandbox, the payload is sanitized, limits are respected, the destination is
approved. A failed validation is `REJECTED` with a stated reason; nothing is
sent. Recorded per item: `replay_id`, source, payload hash, HTTP status,
duration, sanitization record, response summary.

---

## 8. Fault injection

**Types:** `LATENCY`, `TIMEOUT`, `HTTP_4XX`, `HTTP_5XX`, `CONNECTION_FAILURE`,
`RESPONSE_CORRUPTION`, `RESOURCE_PRESSURE`, `DEPENDENCY_UNAVAILABLE`.

**Triggers:** `IMMEDIATE`, `AFTER_REPLAY_INDEX`, `AT_OFFSET`. `MANUAL` and
`ON_REQUEST_COUNT` are rejected at the API boundary: a sandbox has no actor to
fire them, and accepting them would record a fault as injected that never
activated.

**Mechanism** — deliberately dumb and one-directional: ARGUS writes a typed
`faults.json` into the sandbox's own working tree and the target service re-reads
it per request. No IPC, no in-sandbox agent, no RPC surface to abuse. Fault
injection is a file write whose content is a bounded, validated spec.

**Audit (§22)** — every fault records type, target, parameters, start/end and
*how many requests observably carried it*. That last number is derived from the
captured telemetry, not from a counter ARGUS incremented when it wrote the file,
which is what keeps "it failed naturally" and "we caused it" distinguishable
after the fact.

---

## 9. Expected behaviour

Expectations are **never hand-written**. The only constructor,
`ExpectedBehavior.from_evidence`, copies the incident's own observed signals, so
every expectation carries provenance (which incident rows produced it). Matching
is coarse — component, kind, threshold — because a narrower match would reward
reproducing noise rather than the shape of the failure. Expectations that were
not satisfied become explicit `MISSING` observations: a clean run and a run that
never exercised the failure path must not look the same.

---

## 10. Comparison

Per run, the comparator scores independent dimensions and stores the formula that
produced each one. `formula_reference` carries the full text, e.g.:

```
Component       = |shared components| / |incident components|
Error           = 1 - |original error rate - reproduced error rate| / max(both, 0.05)
Latency         = mean over shared components of min(original, reproduced) / max(original, reproduced)
Trace topology  = Jaccard of (parent, child) call pairs
Log pattern     = Jaccard of normalized error-message templates
Failure sequence= longest common subsequence of the two component orders / longer length
Temporal        = concordant pairs / all comparable pairs (Kendall-style)
Recovery        = |recovered in both| / |recovered in the incident|
Overall         = weighted mean over dimensions with available inputs only
                  (FAILURE_SEQUENCE 2.0, COMPONENT 1.5, ERROR 1.5,
                   LATENCY 1.0, TRACE_TOPOLOGY 1.0, LOG_PATTERN 1.0,
                   TEMPORAL 1.0, RECOVERY 0.5)
```

**A dimension with missing inputs reports `available: false` and is excluded** —
absence of evidence is never scored as dissimilarity. The result is a *bucket*
(`HIGH` / `MEDIUM` / `LOW` / `INSUFFICIENT`) plus the dimension scores, never
"97.2381%".

---

## 11. Hypothesis validation

| Outcome | Meaning |
| :--- | :--- |
| `SUPPORTED` | The reproduction is consistent with the hypothesis across the compared dimensions |
| `PARTIALLY_SUPPORTED` | Part of the expected behaviour reproduced |
| `NOT_SUPPORTED` | The expected behaviour did not reproduce under the available conditions |
| `INCONCLUSIVE` | The evidence cannot distinguish support from environment mismatch |

The validator considers, in order: whether any comparison exists; whether the
reproduction succeeded; **major environment differences** (which cap confidence);
missing inputs; whether faults were injected but did not propagate; and whether
replay ever reached the affected services.

**A failed reproduction is never reported as a refutation.** A null result ships
with the reason it is null:

- `failure_classification` — `ENVIRONMENT_ERROR`, `INPUT_ERROR`, `TIMEOUT`,
  `RESOURCE_LIMIT`, `DEPENDENCY_UNAVAILABLE`, `SANDBOX_ERROR`,
  `APPLICATION_FAILURE`, `NO_FAILURE_OBSERVED`, `INSUFFICIENT_TELEMETRY`,
  `UNKNOWN`;
- environment differences with severity (`MAJOR` differences cap confidence);
- missing inputs;
- stated limitations, always present.

**Determinism (§34)** is reported as an observation, not a probability:

```json
{ "runs": 4, "successful_runs": 3, "partial_runs": 0,
  "reproduction_rate": 0.75, "classification": "INTERMITTENT",
  "note": "Reproduction rate is an experimental observation over 4 run(s) — it is
           not a probability that the hypothesis is true." }
```

Repetitions are capped (`REPRO_MAX_REPETITIONS`, default 10), each in its own
fresh sandbox.

---

## 12. Artifacts

Stored **outside** the sandbox working tree, because the sandbox is disposable and
the artifacts must outlive it. One experiment's output:

```
<experiment-id>/plan.json
<experiment-id>/manifest.json
<experiment-id>/validation.json
<experiment-id>/run-1/{sandbox.json, replay-manifest.json, telemetry.json, faults.json, comparison.json}
<experiment-id>/run-N/…
```

- **Content-addressed.** Each artifact records a SHA-256 of its bytes, so an old
  experiment's evidence can still be verified against what it claims to be.
- **Immutable.** Writing *different* bytes under an existing name is refused, not
  overwritten: history is the one thing a reliability tool must not rewrite.
- Type, size, storage location, hash and `immutable` are queryable through the
  API; the manifest references every hash.

---

## 13. Cleanup guarantees

After every experiment, on every path — success, failure, cancellation, timeout,
crash:

```
sandbox processes stopped ─► workdir removed ─► sandbox row marked DESTROYED
```

- The orchestrator's `finally` handles the normal path.
- The reaper handles the path where the *process* died between provisioning and
  cleanup: it destroys sandboxes belonging to terminal or vanished experiments.
- A sandbox that could not be destroyed records `cleanup_error` and is counted by
  `GET /reproductions/metrics` as `cleanup_failures` / `orphaned_sandboxes` —
  surfaced, never silently leaked.
- There is deliberately **no "keep the sandbox" knob**: §55 makes cleanup
  unconditional, and a retention flag that promised to preserve a sandbox would
  be a switch ARGUS could not honour.

---

## 14. API

```
POST   /incidents/{id}/reproductions        plan an experiment (never executes)
GET    /incidents/{id}/reproductions        one incident's experiment history
GET    /reproductions                       experiments in a project scope
GET    /reproductions/metrics               engine health (§54)
GET    /reproductions/{id}                  one experiment, fully hydrated
GET    /reproductions/{id}/plan             the explicit plan
GET    /reproductions/{id}/safety           the confirmation payload
GET    /reproductions/{id}/status           live status + progress
GET    /reproductions/{id}/inputs           planned/recorded replay items
GET    /reproductions/{id}/telemetry        captured, namespaced telemetry
GET    /reproductions/{id}/artifacts        immutable artifacts with hashes
GET    /reproductions/{id}/comparison       per-run comparison + formulas
GET    /reproductions/{id}/validation       the verdict, evidence and limits
GET    /reproductions/{id}/environment      original vs sandbox snapshots
GET    /reproductions/{id}/faults           injected faults and their impact
GET    /reproductions/{id}/manifest         the reproduction manifest
POST   /reproductions/{id}/start            execute (explicit confirmation only)
POST   /reproductions/{id}/cancel           cancel a running experiment
POST   /reproductions/{id}/retry            plan a fresh version, same hypothesis
```

`RETRY` plans a **new version** rather than re-running the row: an experiment is a
historical record, and reusing it would erase the first attempt's observations.

---

## 15. Frontend

| Route | Purpose |
| :--- | :--- |
| `/reproductions` | Project-scoped experiment list + engine health (§54) |
| `/incidents/{id}/reproductions` | Experiment history and the "plan an experiment" entry point |
| `/reproductions/{id}` | The workspace: verdict, execution/safety gate, plan, hypothesis, inputs, faults, repetitions, telemetry, comparison, validation, environment difference, artifacts, manifest |

The workspace polls `/status` **only while an experiment is genuinely in flight**
and stops the moment it is terminal. No websocket infrastructure was added: the
page is open for minutes, and polling the endpoint that already exists is enough.

The safety gate is real, not decorative: the start button stays disabled until
the engineer ticks the confirmation box *and* the server says the experiment is
startable. The plan and its warnings are shown before anything runs.

---

## 16. Observability of ARGUS itself

`GET /reproductions/metrics?project_id=…` reports, derived from stored rows and
the filesystem rather than an in-memory counter (which would report a healthy
zero while sandboxes piled up):

`experiments` by status · `results` · `runs_completed` / `runs_failed` ·
`sandboxes_total` / `sandboxes_destroyed` / `live_sandboxes` /
`orphaned_sandboxes` · `cleanup_failures` · mean experiment, run, provisioning
and cleanup durations · `failures_by_class` · backend · network policy · sandbox
disk usage.

---

## 17. Configuration

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `REPRODUCTION_ENABLED` | `true` | Master switch; existing experiments stay queryable when off |
| `REPRO_SANDBOX_BACKEND` | `local` | `local` or `docker` |
| `REPRO_SANDBOX_ROOT` | `<tmp>/argus-reproduction` | Where working trees are created |
| `REPRO_ARTIFACT_ROOT` | `<cwd>/var/reproduction` | Where artifacts are stored (outside the sandbox) |
| `REPRO_DOCKER_IMAGE` | `python:3.11-alpine` | Sandbox image when the Docker backend is used |
| `REPRO_NETWORK_POLICY` | `ISOLATED` | `ISOLATED`, `MOCK_DEPENDENCIES`, `CONTROLLED_EGRESS` |
| `REPRO_EGRESS_ALLOWLIST` | `[]` | Hosts reachable under `CONTROLLED_EGRESS` |
| `REPRO_EXPERIMENT_TIMEOUT_SECONDS` | `300` | Per-experiment deadline |
| `REPRO_PROVISION_TIMEOUT_SECONDS` | `60` | Sandbox startup budget |
| `REPRO_MAX_CPU_SECONDS` / `_MEMORY_MB` / `_DISK_MB` / `_PROCESSES` | `60` / `512` / `64` / `32` | Per-sandbox limits |
| `REPRO_MAX_REPLAY_REQUESTS` | `200` | Replay ceiling |
| `REPRO_MAX_TELEMETRY_SIGNALS` / `_BYTES` | `5000` / `8000000` | Capture ceilings |
| `REPRO_DEFAULT_REPLAY_MODE` | `SEQUENTIAL` | Replay dispatch mode |
| `REPRO_MAX_REPETITIONS` / `_DEFAULT_REPETITIONS` | `10` / `1` | Repetition bounds |
| `REPRO_MAX_INPUTS` | `50` | Inputs a plan may select |
| `REPRO_SEQUENCE_TOLERANCE_SECONDS` | `120` | Window for aligning sequences |
| `REPRO_FORWARD_TELEMETRY` | `false` | Never forward reproduction telemetry into production tables |
| `REPRO_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | `true` / `60` | Reaper |
| `RETENTION_REPRODUCTIONS` | `180` | Retention for Phase 5 rows |

---

## 18. Testing

| Suite | Covers |
| :--- | :--- |
| `tests/test_phase5_sandbox.py` | Creation/start/stop/destroy, resource limits, loopback-only binding, sanitized env, backend selection, harness shipping inside the image |
| `tests/test_phase5_security.py` | Input sanitization, config sanitization, replay safety gate, API rejection of executable content, cross-project isolation |
| `tests/test_phase5_engines.py` | Replay modes, fault engine, comparator, validator, expectations, lifecycle, artifact store, environment snapshots |
| `tests/test_phase5_cleanup.py` | Cancellation, timeout + reaper, failure classification, worker jobs |
| `tests/test_phase5_api.py` | Planning surface (never executes), execution refusals, reading surface, performance ceilings |
| `tests/test_phase5_demo.py` | The three scenarios end-to-end with real sandboxes: successful reproduction, counterexample, intermittent |

Plus the live gate: `bash infrastructure/e2e-smoke-phase5.sh` runs the entire
engine against the compose stack through the real HTTP API — plan → confirm →
sandbox → replay → fault → capture → compare → validate → artifacts → destroy —
and asserts the security refusals, the namespace isolation and the cleanup
outcome.

---

## 19. Limitations

Reproduction may fail — or succeed misleadingly — because of:

- **missing state.** The sandbox starts from synthetic fixtures, not from the
  production datastore;
- **missing telemetry.** Signals the original system never recorded cannot be
  replayed;
- **environment mismatch.** Different dependency versions, topology or flags;
  `MAJOR` differences cap the verdict's confidence;
- **timing sensitivity.** Relative timing is preserved, but absolute scheduling
  and machine speed are not;
- **non-determinism.** Reported as a rate over the runs that happened, never as a
  causal probability;
- **unavailable external dependencies.** A sandbox has no internet and no
  production credentials, by design;
- **unknown hidden dependencies.** An unmodelled dependency is not reproduced;
- **insufficient instrumentation.** A service that emits no telemetry produces no
  observations to compare.

A failed reproduction is therefore **not** proof that the hypothesis is wrong. It
is an observation that this experiment, under these conditions, did not show the
expected behaviour — and the record says which of the above applied.

Phase 5 also cannot: reproduce across projects, modify source code, generate or
apply a patch, remediate a production system, deploy, roll back, or self-heal.
Those remain later phases, and were deliberately not implemented.

---

## 20. Demo scenarios

The seeded demo (**ARGUS Demo Commerce**: checkout → inventory → datastore)
supports all three (§58–§61), and the pytest suite drives them with real
sandboxes — nothing is hard-coded:

1. **Successful reproduction.** The datastore hypothesis is planned as a
   `LATENCY` fault at the magnitude the incident showed; the sandbox must
   actually produce `datastore → inventory → checkout` failures and match the
   incident's sequence. Verdict: `SUPPORTED`.
2. **Counterexample.** A deployment hypothesis is planned as an *un-injected
   baseline*: nothing is forced, and the expected failure must **not** appear.
   Verdict: `NOT_SUPPORTED` / `INCONCLUSIVE` — derived from the experiment.
3. **Intermittent.** A probabilistic `HTTP_5XX` fault is injected at
   `intensity 0.25` over several repetitions; ARGUS reports
   `INTERMITTENT`-style behaviour with the observed rate and states plainly that
   it is not a probability.
