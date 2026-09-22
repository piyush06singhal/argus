# Phase 9 Implementation Report — Safe Autonomous Remediation

**Status: complete — implemented, tested, documented, validated live.**

Phase 9 gives ARGUS the ability to act on what it has diagnosed: propose a
bounded remediation from stored evidence, run it through validation, safety,
policy and authority, execute only what those gates allowed, verify the effect
against telemetry, and roll back what made things worse — leaving a hash-chained
record of every decision. It does not give a model control of infrastructure.

---

## 1. Executive summary

ARGUS could observe, detect, correlate, explain, reproduce, locate a fault,
generate and verify a fix, and forecast risk — and stop. Phase 9 is the layer
where stopping becomes a decision rather than a limitation:

* A **remediation domain** — proposals, actions, assessments, policy decisions,
  approvals, executions, verifications, rollbacks, audit events, circuit breakers
  and controls, across twelve tables and one migration.
* A **closed action registry** of twelve registered actions with declared
  parameters, risk, reversibility, verification plans and blast-radius limits.
* A **planner** that is a rule engine, not a model: seven strategies over stored
  evidence, a confidence floor, a per-run cap, and no proposal without evidence.
* A **safety engine** whose verdict the policy engine may not override, and a
  **policy engine** with an ordered rule list whose every firing is recorded.
* **Authorization** that distinguishes an autonomous policy decision from a named
  human's, with append-only approval records and expiry.
* **Controlled execution** with three adapters, a deterministic idempotency key,
  a real `effect_applied` flag, and a re-check of every gate at execution time.
* **Verification** against telemetry, with `NOT_OBSERVABLE` as a first-class
  result and `INCONCLUSIVE` never rounded up to success.
* **Rollback** that reverses the effect and verifies the reversal, refusing to
  fake one that cannot be performed.
* A **control plane** that is genuinely real: seven background jobs consult it,
  per scope.
* **Detection and containment** — budgets, cooldowns, concurrency limits,
  breakers, backoff, expiry and a sweep that closes what a dead process left.
* A **hash-chained audit trail** whose integrity is recomputable on demand.
* An **API surface** (25 endpoints), a scheduled sweep wired into the app
  lifespan, queue and worker integration, retention.
* A **UI** — console, action detail with the whole evidence chain, and a policy
  page with the emergency stop.

## 2. Architecture

Phase 9 introduces no duplicate infrastructure: it reuses the Redis queue, the
worker runner, the sweep idiom, `project_id` scope enforcement, the audit-event
convention and the retention sweeper.

```text
stored evidence (Phases 1–8)
        ↓
RemediationPlanner ─► ProposalDraft (evidence, preconditions, plans, limits)
        ↓
RemediationService.propose  ─► remediation_proposals + remediation_actions (PROPOSED)
        ↓
SafetyEngine.assess         ─► PASSED / PASSED_WITH_WARNINGS / FAILED   (not overridable)
        ↓
PolicyEngine.evaluate       ─► ALLOW / ALLOW_WITH_CANARY / REQUIRE_APPROVAL / DENY
        ↓
Authorization               ─► human approval (named) or autonomous policy decision
        ↓
RemediationExecutor         ─► adapter applies a real, bounded effect
        ↓
RemediationVerifier         ─► VERIFIED / PARTIALLY_VERIFIED / FAILED / INCONCLUSIVE
        ↓
Rollback + post-analysis    ─► verified reversal, then a re-examination of the evidence
        ↓
Hash-chained audit          ─► every state change and gate decision
```

Modules: `remediation_registry`, `_state`, `_clock`, `_controls`, `_policy`,
`_safety`, `_evidence`, `_planner`, `_executor`, `_verification`, `_rollback`,
`_audit`, `_service`, `_sweep` — 205 lines of clock to 1,792 of model, and a
shared service that the API, the worker and the sweep all call so the pipeline
cannot diverge between the three.

## 3. Action registry

Twelve actions, each declaring `required_parameters`, `risk_level`,
`required_permissions`, `verification_strategy`, `rollback_strategy`,
`maximum_blast_radius`, `requires_human_approval` and
`supports_autonomous_execution`. The full table with all twelve rows is in
[`docs/safe-autonomous-remediation.md`](safe-autonomous-remediation.md#2-the-action-registry).

The registry is what makes "default deny" structural: an action type that is not
registered has no parameters to validate, no adapter to select and no plan to
execute, and `validate_parameters` treats an unrecognised *parameter* as an error
rather than ignoring it. `REMEDIATION_ENABLED_ACTION_TYPES` narrows the
executable subset further, and a refusal names the reason.

Five actions are executable in this build (`PAUSE_BACKGROUND_JOB`,
`RESUME_BACKGROUND_JOB`, `DISABLE_FEATURE_FLAG`, `ENABLE_FEATURE_FLAG`,
`DISABLE_DEGRADED_DEPENDENCY`) because they act on ARGUS's own runtime through
the control plane. The rest are registered, proposed and refused with
`ADAPTER_UNAVAILABLE` — an honest refusal instead of a simulated success.

## 4. Policy engine

One ordered rule list, every firing recorded:

```text
emergency_stop → kill_switch → regime → containment → allow_lists →
registry_executable → circuit_breaker → safety_assessment →
blast_radius → budget → cooldown → concurrency → authority
```

The engine can only ever be *narrowed*: `REMEDIATION_HARD_*` ceilings are applied
on top of a stored policy, and anything they overrode appears in `clamped` so the
console can say a stored value is not the effective one. A missing policy row is
not a permissive default — the engine materialises a restrictive `OBSERVE_ONLY`
fallback and records that it did.

## 5. Authorization

| Decision | Meaning | Recorded as |
| :--- | :--- | :--- |
| `ALLOW` | Every rule passed and the authority question is answered | `actor_type=AUTONOMOUS_POLICY` or a named human |
| `ALLOW_WITH_CANARY` | Allowed, but the first step is bounded | `canary_stage=CANARY` |
| `REQUIRE_APPROVAL` | Permitted; a person decides | a `PENDING` approval with an expiry |
| `DENY` | Refused | a reason, in the audit trail |

`human_approved` satisfies the authority rule and *nothing else*: every rule above
it has already fired, so a person can authorize a remediation but cannot
authorize one the platform refused. Autonomous authorization requires a
non-production scope, an action that supports it, and a risk level inside the
policy ceiling, under a process ceiling no database row can raise.

## 6. Execution

Execution re-checks rather than trusts: the process kill switch, the mode's
`allows_live_effect`, the safety engine and the current policy are all consulted
again, and a resumed attempt reuses its idempotency key so it cannot apply twice.
`effect_applied` distinguishes the handler having run from something having
changed, and only the latter can lead to verification. Every attempt records what
it did in structured steps — never a raw command line, because there is no
command line.

## 7. Blast radius

Declared on the action (`SINGLE_INSTANCE` → `PROJECT_WIDE`), capped by the policy
ceiling, capped again by the process ceiling, and for percentage-scoped actions
carrying the percentage itself. The narrower of the three always wins, and the
narrowing is recorded.

## 8. Canary execution

When the regime is `AUTONOMOUS` and the action supports it, the policy requires a
canary first. The action records `canary_required`, `canary_stage` and
`canary_percent`; the broader step needs a further decision. ARGUS never widens a
canary by itself, and a policy can disable canaries where an operator judges them
inappropriate.

## 9. Verification

Five verdicts, and the important one is `INCONCLUSIVE`: an action whose effect
cannot be observed has not been verified, and is never reported as one. Each
check records `PASS`, `FAIL`, `NOT_OBSERVABLE` or `SKIPPED`, and
`NOT_OBSERVABLE` — "we have no data" — can never be silently treated as a pass.
Verification runs over a real observation window
(`REMEDIATION_VERIFICATION_WINDOW_SECONDS` + grace), and a window that has not
filled produces `INCONCLUSIVE` plus a retry while attempts remain.

## 10. Rollback

A rollback is declared *before* execution, never discovered after. Four
strategies: `INVERSE_ACTION` (pause↔resume, disable↔enable),
`RESTORE_PREVIOUS_STATE` (restore the captured previous value), `REVERT_WORKSPACE`
(reset to the base revision) and `MANUAL` (only a human, outside ARGUS — recorded
and then verified, not attempted). `NONE` means irreversible, which forces human
approval unconditionally.

Rollback is itself verified: an attempted reversal is not a completed one, and
rolling back something that was never applied is refused rather than faked.

## 11. Safety

* No `subprocess`, shell, SSH, filesystem-write, cloud-API or credential path
  exists anywhere in the package; there is no endpoint that accepts a command, a
  script or a URL.
* The action set is closed and parameter-validated.
* Every read requires a `project_id`; a foreign id answers `404`.
* Prompt-injection surface: telemetry text reaches the planner only as stored
  values, and an optional LLM narrative cannot reach a field that carries a
  claim.
* The emergency stop denies every action in a scope before any other rule runs,
  and engaging or releasing it is itself audited.
* Trust boundary stated plainly: ARGUS can genuinely change *its own* runtime, and
  can only propose changes to systems it does not own.

## 12. Auditability

One hash-chained event per state change and gate decision, each binding its
predecessor (`entry_hash`, `prev_hash`, `sequence`). `GET …/audit` returns the
trail; `GET …/audit/verify` recomputes the chain and reports the first break with
its index and reason. Attribution is explicit: `actor_type` distinguishes a human
from an autonomous policy decision, and every approval names its actor.

## 13. Testing

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend suite | `pytest -q` | **1487 passed, 1 skipped** |
| Phase 9 backend tests | the ten `test_phase9_*.py` files | **254 collected** (253 passed, 1 conditional skip) |
| Lint | `ruff check app tests` | clean |
| Format | `ruff format --check app tests` | clean |
| Types | `mypy app` | clean (178 modules) |
| Frontend tests | `vitest run` | **161 passed** (29 for `lib/remediation.ts`) |
| Frontend type check | `tsc --noEmit` | clean |
| Frontend build | `next build` | succeeds, 39 routes incl. 3 remediation routes |
| Phase 9 live gate | `bash infrastructure/e2e-smoke-phase9.sh` | **69/69** |
| Phase 9 gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase9.sh` | **70/70** (the Phase 9 migration downgrades and re-applies) |
| Phases 1–8 live gates | `bash infrastructure/e2e-smoke-phase{1,2,3,4,5,6,7,8}.sh` | **46 · 28 · 103 · 70 · 104 · 159 · 90 · 42** (all re-run against this revision) |
| Fresh-database bootstrap | empty database → `alembic upgrade head` → `seed_data.py` | 13 migrations apply from zero into 98 tables (12 for Phase 9); the demo dataset derives its incidents, analyses, reproduction and forecasts |

Phase 9 test areas, by file:

| File | Tests | Covers |
| :--- | :--- | :--- |
| `test_phase9_registry.py` | 31 | Parameter validation, reversibility, authority flags, the closed set |
| `test_phase9_safety.py` | 28 | Unevidenced actions, deleted/foreign targets, duplicate races, preconditions |
| `test_phase9_policy.py` | 31 | Every rule, default deny, ceilings, budgets, breakers, the scope classifier, kill switch |
| `test_phase9_execution.py` | 18 | Real effects, re-checked gates, idempotency, dry runs, mode refusals |
| `test_phase9_verification.py` | 18 | Verdicts, `NOT_OBSERVABLE`, harmful effects, rollback, reversal verification |
| `test_phase9_audit.py` | 19 | Chain integrity, tamper detection, attribution |
| `test_phase9_api.py` | 46 | Scope, shape, refusals, status codes, the full decision flow over HTTP |
| `test_phase9_demo.py` | 21 | The eight §107 demo scenarios end to end |
| `test_phase9_planner.py` | 29 | Candidate generation, provenance, bounds, and the sweep's refusals and deadlines |
| `test_phase9_control_gate.py` | 13 | A pause actually stopping the job it names, per scope |

Both success and failure paths are exercised for all six regimes, and no test
monkeypatches a gate: reaching `AUTHORIZED` requires configuring a policy an
operator could have configured.

### Defects found by validation and fixed

1. **The remediation sweep was never scheduled.** `sweep_remediations_forever`
   existed and the API could trigger it, but nothing in the app lifespan started
   it, so in production an action that policy authorized and no worker picked up
   would stall in `AUTHORIZED` forever — and verification windows would never
   close. Wired into the lifespan, gated on the execution and sweep switches.
2. **A `PAUSE_BACKGROUND_JOB` action was nominal for six of the seven jobs it
   could name.** Only the reliability sweep consulted the control plane; the
   detection sweep, the code, fix and reproduction reapers, and the remediation
   sweep itself ignored it — so an action could report `VERIFIED` while the job
   it paused carried on working. Every sweep now honours the pause, per scope,
   with `paused_scope_ids` so a pause costs one query per pass, and 13 tests pin
   it.
3. **A pause for one project would have stopped the platform.** The reapers have
   no project loop, so the first implementation's obvious fix — skip the whole
   pass — would have let one project's pause withhold reaping from every other,
   a far wider effect than the action's own blast radius. The pause is applied
   per row instead.
4. **A component-scoped forecast that outlived its component became a nameless
   heatmap cell** (carried over from Phase 8 and re-checked here, since Phase 9
   consumes those forecasts).
5. **`mypy` rejected the control-plane query builder** — the first element of the
   condition list is a `BinaryExpression` while a disjunction is a
   `ColumnElement`, so the narrower inferred type rejected the `or_` clauses.
6. **The manual-execution docstring described a `MANUAL` execution mode that does
   not exist.** The implementation records `HUMAN_APPROVAL` with
   `adapter_name="human_operator"` and says so in the record's own fields; the
   docstring had drifted. Documentation that describes behaviour the code does
   not have is exactly what this phase must not ship.
7. **A duplicate circuit-breaker row could silently mask an open breaker.** The
   breaker is read as a single row per scope and asked "is it open?", but nothing
   enforced one row per `(project, environment, action_type)`: the unique key was
   an ordinary index, and a policy evaluation racing another could insert a
   second row. The failure was found live — the gate opened a breaker and the
   policy still reported `CLOSED`, because an earlier evaluation had created a
   `CLOSED` row for the same scope and the read landed on it. A unique index now
   makes the scope the database's business (with a migration that collapses
   existing duplicates, keeping the most restrictive row — an `OPEN` breaker is
   never discarded), `get_breaker` inserts through a SAVEPOINT and adopts the row
   the other writer created, and two tests pin the uniqueness and the read.
   The index is `NULLS NOT DISTINCT` because a project-wide breaker has a NULL
   environment and the default SQL rule would treat two such rows as different.
8. **A stale action awaiting approval did not expire.** The sweep expired
   *approvals* and then expired actions in `PROPOSED`…`BLOCKED`, but not
   `AWAITING_APPROVAL` — the reasoning being that the approval's own (shorter)
   TTL gets there first. That is true by default and false in general: the
   action's own deadline has to hold on its own, or an action past its deadline
   with a still-pending approval could be approved and executed later, which is
   the §107 guarantee inverted. `AWAITING_APPROVAL` is now in the list, the
   action is `EXPIRED`/`STALE_ACTION`, and the approval it leaves behind is
   closed with it so nobody is offered a button that can no longer work.
9. **The environment classifier trusted the name alone.** Autonomous execution
   was permitted wherever the environment's *name* appeared in
   `REMEDIATION_NON_PRODUCTION_ENVIRONMENT_NAMES`, so a production environment
   named `staging` was treated as non-production — a naming convention treated as
   a fact. Found live: the gate's autonomous checks escalated with "the scope is
   treated as production", and a `staging`-named environment whose declared
   `environment_type` was `PRODUCTION` was authorized anyway. The classifier now
   requires both signals — an allow-listed name **and** a declaration that is not
   `PRODUCTION` — is shared by the safety engine and the policy engine so the two
   cannot diverge, and is asserted live with a deliberately misleading scope
   (allow-listed name, `PRODUCTION` declaration) that must escalate.

Items 7 and 9 are the pair worth dwelling on. Item 7 is a safety mechanism
silently not working; item 9 is a safety decision resting on a string. Both were
found by asking the implementation what it actually does — *is the open breaker
the one you read?* and *does this scope pass your test?* — rather than by reading
the tests, which were green throughout.

## 14. Demo

`bash infrastructure/e2e-smoke-phase9.sh` drives the whole pipeline over the real
HTTP API against the compose stack, and each of the eight §107 scenarios is
exercised in both its success and its refusal path:

* **human-approved remediation** — propose, assess, policy-review, approve as a
  named human, execute, verify; and the refusal when no policy row exists;
* **autonomous low-risk remediation** — authorized by policy in a non-production
  scope, plus the escalation of an action the registry will not run
  autonomously, the refusal in a production scope, and the refusal in an
  environment whose allow-listed *name* disagrees with its `PRODUCTION`
  declaration;
* **canary execution** — the canary step is required, recorded and bounded, on an
  action that declares `supports_canary` (a control-plane pause is binary, so
  asking it to canary would be asking for nothing);
* **failed verification** — an action whose telemetry does not improve ends
  `FAILED`/`INCONCLUSIVE` and is never reported as verified;
* **rollback** — a harmful effect rolls back, the reversal is verified, and a
  rollback of something never applied is refused;
* **policy denial** — a denied action records `POLICY_DENIED` with the rule that
  fired, and the denial is what the API returns;
* **stale action** — an action past its own deadline is `EXPIRED`/`STALE_ACTION`
  even while its approval is still pending, so it cannot execute a day later
  against a system that has moved on;
* **action loop protection** — the breaker opens after repeated failures, further
  attempts are refused while it is open, and the budget cap refuses the next
  action in the same window.

On the emergency stop the expectation is deliberate: the action is `BLOCKED`, not
`REJECTED`. A stop is a condition the project can leave, so the action stays
re-evaluable instead of being terminally refused — what the gate asserts is that
it did not execute, that nothing authorized it, and that the refusal names the
stop as the rule that fired.

Plus: the six regimes are verified individually, a pause is proven to actually
stop the job it names, the audit chain is recomputed, cross-project isolation is
checked, the twelve tables and their indexes are confirmed to exist, and the
breaker scope is proven unique in the database — including for a project-wide
scope, where a second row must be refused for a `NULL` environment to be
comparable to itself.

## 15. Limitations

* **Only ARGUS-native actions apply a real effect.** External actions are refused
  with `ADAPTER_UNAVAILABLE` until an operator configures an adapter.
* **Verification observes a window.** A recurrence after the window is missed by
  that verification; the post-analysis pass and the breaker exist for the rest.
* **A rollback is not a time machine.** A restart cannot restore in-memory state,
  and the registry says so.
* **The diagnosis bounds the outcome.** A correctly executed, correctly verified
  remediation of the wrong target is still wrong — which is why every proposal
  carries its evidence, confidence and limitations.
* **Environment classification needs configuration to be right.** A scope counts
  as non-production only when its name is in
  `REMEDIATION_NON_PRODUCTION_ENVIRONMENT_NAMES` and its declared
  `environment_type` is not `PRODUCTION`; an unknown or missing environment is
  production. That is two signals rather than one, but it is still a policy
  decision an operator configures — ARGUS cannot know that an environment typed
  `DEVELOPMENT` is in fact carrying customer traffic.
* **The autonomous ceiling rule is a guard, not a live path.** No action in this
  registry is both autonomous-eligible and above `LOW` risk, so the
  `autonomous_ceiling` rule cannot fire today; it is pinned by unit tests and
  exists for the first action that is. What the live gate proves is the reachable
  half: an action the registry will not run autonomously escalates, and the
  refusal names the authority rule.
* **Policy is configuration.** A permissive policy is a deliberate operator
  decision; the metrics expose the pattern of autonomous authorizations so it can
  be reviewed.
* **Autonomous execution carries residual risk.** It is bounded, scoped,
  reversible where the action allows, breaker-limited, audited and revocable by
  one call — and it is still an action taken without a person in the loop, which
  is why it ships disabled.

## 16. Next phase

**Phase 10 — Reliability Intelligence & Autonomous Learning.** Phase 10 will learn
from what Phase 9 executes: correlating incidents, forecasts, remediation
outcomes and verified fixes to improve ARGUS's own reliability intelligence —
which action types actually work for which failure shapes, how accurate the
predictions turned out to be, and where its own analysis was wrong.

Phase 9 deliberately builds none of that. There is no self-modification, no
automatic policy change, no model retraining or activation, and no unbounded
decision loop: `remediation_drift`-style learning does not exist yet, and a
breaker record has no code path to anything that changes behaviour on its own.
That boundary is the point of this phase.
