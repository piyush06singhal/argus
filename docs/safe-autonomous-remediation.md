# Safe Autonomous Remediation

Phase 9 gives ARGUS the ability to *act* — and almost everything in it exists to
make that ability safe rather than to make it powerful.

ARGUS could already observe, detect, correlate, explain, reproduce, locate a
fault, generate and verify a fix, and forecast risk. Phase 9 closes the loop:

```text
Problem
   ↓
Evidence
   ↓
Diagnosis
   ↓
Remediation proposal          ← evidence, not invention
   ↓
Risk assessment               ← blast radius, reversibility
   ↓
Policy decision               ← allow / escalate / deny
   ↓
Authorization                 ← a human, or policy inside a ceiling
   ↓
Precondition validation       ← re-checked at execution time
   ↓
Controlled execution          ← a registered action, never a command
   ↓
Verification                  ← observed telemetry, not an exit code
   ↓
   ┌───────────────┐
SUCCESS         FAILURE
   │               │
Continue       Rollback → verified reversal
   │               │
   └───────┬───────┘
           ↓
     Post-analysis → Audit
```

The system must always preserve **human control over consequential actions**.

## 1. The boundary this phase will not cross

Nine principles are enforced as code, not stated as policy:

| Principle | Where it is enforced |
| :--- | :--- |
| An AI recommendation is not an authorization | `remediation_service.assess` → `evaluate_policy` → `decide`; nothing reaches execution without all three |
| Default deny | `resolve_policy` returns a restrictive `OBSERVE_ONLY` fallback when no row exists, and the engine returns `DENY` before any other rule |
| Production is protected | Actions are a closed registry; an action outside the allow-list is refused before a handler is selected |
| Every action is reversible where possible | `ActionDefinition.rollback_strategy` is declared up front; `NONE` forces human approval unconditionally |
| Verification is mandatory | `remediation_verification` reads telemetry; `INCONCLUSIVE` is a real verdict and never rounds to success |
| No blind retries | Bounded attempts, exponential backoff, per-scope budgets, circuit breakers, expiry |
| Blast radius is bounded | `BlastRadiusScope` per action, an operator ceiling per policy, and a process ceiling above both |
| Autonomous execution is policy-controlled | `PolicyDecision` and `RemediationExecutionMode`; `AUTONOMOUS` is only considered in a non-production scope, only for eligible actions, only under the risk ceiling |
| AI does not control infrastructure | This package contains no `subprocess`, no shell, no HTTP adapter, no credential path |

That last row is checkable, and it is checked: `grep -rn "subprocess" app/services/remediation_*.py`
returns nothing, and there is no endpoint anywhere in the API that accepts a
command, a script or a URL.

## 2. The action registry

`remediation_registry.py` is the closed set of things ARGUS can ever do. Every
definition declares its required parameters, allowed risk, permissions,
verification plan, rollback strategy, maximum blast radius and whether a human is
required:

| Action | Risk | Reversible | Autonomous | Adapter |
| :--- | :--- | :--- | :--- | :--- |
| `RESTART_SERVICE` | MEDIUM | no (runtime state is gone) | no | external |
| `RESTART_INSTANCE` | MEDIUM | no | no | external |
| `SCALE_SERVICE_WITHIN_LIMIT` | MEDIUM | yes (inverse scale) | no | external |
| `DISABLE_FEATURE_FLAG` | LOW | yes (enable) | yes | control plane |
| `ENABLE_FEATURE_FLAG` | MEDIUM | yes (disable) | no | control plane |
| `PAUSE_BACKGROUND_JOB` | LOW | yes (resume) | yes | control plane |
| `RESUME_BACKGROUND_JOB` | LOW | yes (pause) | yes | control plane |
| `DISABLE_DEGRADED_DEPENDENCY` | MEDIUM | yes (suppression revert) | no | control plane |
| `ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE` | HIGH | yes | no | external |
| `ROLLBACK_DEPLOYMENT` | HIGH | yes | no | external |
| `ROLLBACK_CONFIGURATION` | HIGH | yes | no | external |
| `APPLY_VERIFIED_PATCH` | HIGH | no | no | workspace |

External actions are registered, proposed, assessed, approved and **refused with
`ADAPTER_UNAVAILABLE`** until an operator configures an adapter. That is default
deny applied to integrations rather than announced in a README: ARGUS will not
pretend it can restart a service it holds no credentials for. What it *can* do
honestly is record that a human did it (`POST …/record-execution`) and then
verify the claim from telemetry like any other attempt.

The control plane is ARGUS's own runtime — its queues, sweeps and feature gates.
Acting on that is genuinely safe, genuinely reversible and genuinely verifiable,
which is why it is the one place a native effect is applied.

## 3. State machine

`remediation_state.py` is the single source of truth for legal transitions,
shared by the API and the manager. The UI can therefore only offer transitions
the backend accepts.

```text
PROPOSED → VALIDATING → POLICY_REVIEW → AWAITING_APPROVAL → AUTHORIZED
        ↘ VALIDATION_FAILED                    ↓                ↓
                                          REJECTED/CANCELLED  EXECUTING
                                                                 ↓
                                                             VERIFYING
                                                       ┌──────┴───────┐
                                                   VERIFIED         FAILED
                                                                      ↓
                                                                 ROLLING_BACK
                                                                      ↓
                                                                 ROLLED_BACK
```

`EXPIRED` and `BLOCKED` are reachable from the pre-execution states; rejection is
terminal (a rejected remediation must be re-proposed); `FAILED` is *not* terminal
because a bounded retry or a rollback follows it. No transition can be skipped
silently: an illegal one raises.

## 4. The pipeline, gate by gate

**Validation and safety** (`remediation_safety.py`) answers "may this action
proceed at all?" — a question an operator cannot override. It checks that the
action references stored evidence, that its target still exists and belongs to
this project, that its parameters satisfy the registry, and that its
preconditions hold *now*. A failed assessment is recorded with each check's
result, and the policy engine refuses to override it. It also classifies the
scope, and that classification is shared with the policy engine so the two
cannot disagree: a scope is non-production only when the environment's name is
in the configured allow-list **and** its declared `environment_type` is not
`PRODUCTION`. One signal alone is a guess — a naming convention can be wrong,
and a declaration can be stale — so an unknown or missing environment is treated
as production.

**Policy** (`remediation_policy.py`) runs an ordered rule list and records every
rule that fired: emergency stop → process kill switch → regime → containment →
allow-lists → registry executability → circuit breaker → safety verdict → blast
radius → budget/cooldown/concurrency → authority. Configuration only ever
*narrows*: `REMEDIATION_HARD_*` ceilings are applied on top of whatever a stored
policy says, and a stored value that exceeds one is reported in `clamped`.

**Authorization** distinguishes `ALLOW`, `ALLOW_WITH_CANARY`,
`REQUIRE_APPROVAL` and `DENY`, and records who decided. An autonomous
authorization is recorded as `actor_type=AUTONOMOUS_POLICY`; a human one names
the person. Approvals are append-only evidence with an expiry, and a stale one
expires rather than lingering.

**Execution** (`remediation_executor.py`) re-checks everything at execution time,
because authorization is not a token that can be replayed. It consults the
process kill switch, the mode's `allows_live_effect`, the safety engine and the
mode itself, computes a deterministic idempotency key so a retried attempt cannot
apply twice, and writes an `effect_applied` flag that separates "the handler ran"
from "something changed".

**Verification** (`remediation_verification.py`) reads telemetry over a window
and produces `VERIFIED`, `PARTIALLY_VERIFIED`, `FAILED`, `INCONCLUSIVE` or
`NOT_EXECUTED`. A check whose data is missing is `NOT_OBSERVABLE`, never a pass —
the single most dangerous possible bug in a verification engine.

**Rollback** (`remediation_rollback.py`) reverses an applied effect and verifies
the reversal. A rollback of something that was never applied is refused, not
faked. A `MANUAL` rollback is recorded and the human's claim is verified; a
`NONE` rollback fails loudly.

**Audit** (`remediation_audit.py`) writes one hash-chained event per state change
and gate decision, so tampering with history is detectable rather than merely
discouraged.

## 5. Blast radius and canary

Blast radius is declared on the action, capped by the policy, and capped again by
a process ceiling the database cannot raise. `LIMITED_PERCENT` carries the
percentage it actually affects.

A canary is a required first step for actions that support it when the regime is
`AUTONOMOUS`: the policy returns `ALLOW_WITH_CANARY`, the action records
`canary_stage` and `canary_percent`, and the broader step needs a second
decision. ARGUS never widens a canary on its own.

`supports_canary` is a declaration, not a default. It is set on the actions that
can be staged — disabling a feature flag (`DISABLE_FEATURE_FLAG`) and restarting
a service (`RESTART_SERVICE`, refused until an adapter exists) — and left off the
binary ones: pausing a background job is on or off, so "canary" would be a word
with nothing behind it.

## 6. Failure containment

* **Retries** are bounded by `REMEDIATION_MAX_EXECUTION_ATTEMPTS` with a backoff,
  and reuse the idempotency key.
* **Budgets** are per scope per window (`REMEDIATION_ACTION_WINDOW_SECONDS`), and
  exhaustion is a recorded refusal with a reason, not silence.
* **Cooldown** stops two actions in the same scope landing together.
* **Concurrency** caps in-flight actions per scope.
* **Circuit breakers** are keyed on `(project, environment, action_type)`: one
  action type failing repeatedly stops being attempted without freezing
  everything else. They cool to `HALF_OPEN` so exactly one probe is allowed. The
  scope is **unique in the database** (and `NULLS NOT DISTINCT`, so a
  project-wide breaker with no environment is one row too): the engine reads a
  single row and asks "is it open?", and a second, `CLOSED` row beside an `OPEN`
  one would answer that question wrongly. The insert is race-safe rather than
  create-if-absent, and a migration collapses any pre-existing duplicates,
  keeping the most restrictive row.
* **Expiry** ends anything nobody acted on, so a forgotten proposal cannot
  execute a day later against a system that has moved on. Every action carries
  its own deadline, and the sweep enforces it in every non-terminal state —
  including `AWAITING_APPROVAL`, whose approval has a shorter TTL of its own. A
  decision on a deadline that has passed is not a decision: the action expires
  and the pending approval it leaves behind is closed with it.
* **The sweep** closes attempts a dead process abandoned, retries verification
  while attempts remain, and retires controls past their own deadline.

## 7. The control plane is real

A remediation is only real if the job it names obeys it. `is_paused` /
`feature_enabled` are consulted by:

| Job | Consulted by |
| :--- | :--- |
| `ingestion_worker` | `worker_runner` before processing an `event` job |
| `anomaly_sweep` | the detection sweep, per project × environment |
| `reliability_sweep` | the forecast sweep, per project |
| `code_sweep` | the code-intelligence reaper, per row |
| `fix_sweep` | the fix-workspace reaper, per row |
| `reproduction_sweep` | the reproduction reaper, per row |
| `remediation_sweep` | the remediation sweep, per project |

Two properties matter as much as the gating itself. **A pause is scoped**: a
pause applied for one project does not stop the job for every other project,
which would be a far wider effect than the action's own blast radius claimed.
And **expiry is honoured on read**: a control past its deadline holds nothing
down even before housekeeping has retired the row, because a pause that outlives
its own deadline because a sweeper was busy is how a temporary intervention
becomes a permanent outage.

## 8. What Phase 9 deliberately does not do

* No shell, no SSH, no Kubernetes administration, no cloud API, no credential
  access — from an AI-generated action or from anywhere else in the package.
* No automatic policy modification: a policy change is a human action and is
  audited as one.
* No decision to widen a canary, retry indefinitely, or re-propose something a
  human rejected.
* No model in the decision path. The planner is a rule engine; the LLM narrative
  (where an operator configures one) can only add prose to a stored explanation,
  and cannot reach a field that carries a claim.
* No Phase 10. Learning from outcomes, retraining and model activation are not
  built here, and a drift record has no code path to a retrain.

## 9. Reading the API

```text
GET    /remediation/action-types              the registry, as data
GET    /remediation/policy                    the effective policy, clamped values included
PUT    /remediation/policy                    change a scope's policy
POST   /remediation/emergency-stop            engage/release the kill switch
GET    /remediation/controls                  what ARGUS is holding down
GET    /remediation/breakers                  per-action-type breaker state
POST   /remediation/actions/plan              ask the planner for proposals
POST   /remediation/actions/propose           a human proposes (still gated)
GET    /remediation/actions                   the queue
GET    /remediation/actions/{id}              the whole evidence chain
GET    /remediation/actions/{id}/audit        the hash-chained trail
GET    /remediation/actions/{id}/audit/verify recompute the chain
POST   /remediation/actions/{id}/assess       re-run safety
POST   /remediation/actions/{id}/evaluate     re-run policy
POST   /remediation/actions/{id}/approve      a named human authorizes
POST   /remediation/actions/{id}/reject       terminal rejection
POST   /remediation/actions/{id}/execute      run, then verify
POST   /remediation/actions/{id}/run          the whole pipeline in one call
POST   /remediation/actions/{id}/verify        verification pass
POST   /remediation/actions/{id}/rollback     reverse and verify
POST   /remediation/actions/{id}/cancel       cancel before execution
POST   /remediation/actions/{id}/record-execution  a human did it; ARGUS verifies the claim
GET    /remediation/incidents/{id}/actions    attempts for one incident
GET    /remediation/metrics                   activity, refusals included
POST   /remediation/sweep                     one sweep pass, now
```

Mutating requests require a `project_id` and prove ownership; an out-of-scope id
answers `404` rather than confirming existence.

## 10. Honest limitations

* **Only ARGUS-native actions apply a real effect today.** Every external action
  is proposed, assessed, approved and refused with `ADAPTER_UNAVAILABLE` until an
  adapter is configured. This is a deliberate scope boundary, not a missing
  feature — but it does mean a fresh installation can genuinely remediate its own
  runtime and nothing else.
* **Verification observes a window.** A failure that recurs after the window is
  not caught by that verification.
* **A rollback is not a time machine.** A restart cannot restore in-memory state;
  the registry says so, and the platform requires a human for it.
* **The diagnosis bounds everything.** A remediation of the wrong component can
  be correctly executed and correctly verified and still be the wrong action.
  That is why every proposal carries its evidence, confidence and limitations,
  and why a human can deny it.
* **Policy is configuration.** A permissive policy is a deliberate operator
  decision, and the pattern of autonomous authorizations is visible in the
  metrics precisely so it can be reviewed.
* **Environments differ.** Non-production classification depends on two things
  an operator controls — the name allow-list and the environment's declared
  `environment_type` — and both have to agree. An environment typed `DEVELOPMENT`
  that is in fact carrying customer traffic is still described as non-production
  here, and ARGUS has no way to know otherwise.
* **Only the autonomous ceiling is untested live.** No action in this registry is
  both autonomous-eligible and above `LOW` risk, so the `autonomous_ceiling`
  refusal cannot be reached end to end today; it is pinned by unit tests and
  waits for the first action that needs it.
* **Autonomous execution carries residual risk.** It is bounded, scoped,
  reversible where the action allows, breaker-limited, audited and instantly
  revocable by one emergency-stop call — and it is still an action taken without
  a person in the loop, which is why nothing executes until an operator writes a
  policy: a project with no policy row resolves to `OBSERVE_ONLY`. The master
  switch (`REMEDIATION_EXECUTION_ENABLED`) defaults to on and is the kill switch an
  operator flips to stop every action everywhere, independently of any policy.
