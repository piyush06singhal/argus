# ARGUS Final Architecture Audit

An assessment of the system as built — what it is made of, which invariants hold
it together, where the seams are, and what the audit changed. Written after the
hardening pass, so it reports findings rather than intentions.

---

## 1. Scale and shape

| | |
| --- | --- |
| Backend | 133,699 lines across `apps/api/app` |
| Backend tests | 108 test modules, 2,187 passing on the PostgreSQL configuration (2,173 on SQLite) |
| Frontend | 42,282 lines across `apps/web/app` and `apps/web/lib`, 68 pages |
| Domain modules | 157 services, 20 model modules, 22 route modules |
| Migrations | 23 revisions (126 `create_table` calls; 127 tables from zero, including `alembic_version`) |
| Background sweeps | 8 (`anomaly`, `code`, `fix`, `intelligence`, `platform`, `reliability`, `remediation`, `reproduction`) |
| Live gates | 19, 1,157 assertions (incl. fault injection, backup restore rehearsal and SSO) |

The shape is a **modular monolith**: one deployable API, one deployable web app,
one PostgreSQL, one Redis. That is a deliberate fit for the problem — every phase
shares the same incident, component and evidence vocabulary, and splitting those
into services would have replaced in-process joins with distributed ones for no
reliability gain.

---

## 2. Layer map

```text
┌──────────────────────────────────────────────────────────────┐
│ apps/web — Next.js App Router (68 pages)                     │
│   server components fetch as the *caller* (cookie token)      │
└───────────────────────────┬──────────────────────────────────┘
                            │  HTTP + Bearer / ingest token
┌───────────────────────────▼──────────────────────────────────┐
│ app/core/edge.py — the edge, in order                         │
│   BodySizeLimit → JsonDepthLimit → RateLimit → Auth(ASGI)     │
│   Auth binds the caller's AuthContext for the whole stack     │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ api/v1/routes (22)  ── mounted under one router with           │
│   enforce_path_scope: every project-scoped path is grant-checked│
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ services (157) — the phase engines, each pure where it can be  │
│   ingestion · graph · detection · correlation · causal ·       │
│   reproduction · code intelligence · fixes · forecasting ·     │
│   remediation · learning · platform                            │
└───────────────────────────┬──────────────────────────────────┘
                            │  SQLAlchemy async
┌───────────────────────────▼──────────────────────────────────┐
│ PostgreSQL (23 revisions)          Redis (queues + buckets)    │
└──────────────────────────────────────────────────────────────┘
          ▲                                        ▲
          │ sweeps (8), gated on BACKGROUND_JOBS_ENABLED
          └──────────── worker container ───────────┘
```

**Async work is one-directional.** Routes enqueue; sweeps consume; nothing in a
request path waits on a sweep. The post-ingest boundary (`enqueue_graph_extract`,
`enqueue_anomaly_detect`) is the only coupling between ingestion and analysis, and
it is a queue write, so a slow analyser degrades freshness rather than latency.

---

## 3. The invariants that matter

These are the properties the architecture exists to protect. Each is enforced in
code and asserted by tests, not left to convention.

### 3.1 Evidence grounding

Every derived row keeps a pointer to the stored rows it came from. A root-cause
candidate names its evidence; a forecast names its feature snapshot; a learned
pattern names its experiences; a remediation action names the incident that
justified it. Consequences:

* **Nothing can be fabricated.** There is no code path that writes a conclusion
  without a provenance column, so an LLM cannot invent a stack frame — there is
  no field for one.
* **Deleting history cascades.** Remove a project and the learning derived from
  it goes too, so the derived tables can never outlive their sources. This is
  asserted live (`phase10`, `phase11` gates).

### 3.2 Epistemic honesty

No output presents a guess as a fact. Confidence is always a bucket with the
evidence counts and an explicit `limitations` list; `UNKNOWN` is a first-class
state meaning "insufficient evidence", never "healthy"; the causal engine's
strongest label is `SUSPICIOUS_CODE_PATH`; a prediction is a band, not a
verdict; `before` is never rendered as `caused`.

This is the most unusual property of the codebase and the easiest to lose, so it
is tested per phase rather than once: the refusal to over-claim is an assertion in
the suite (e.g. "the incident page still declines to claim a root cause").

### 3.3 Tenancy isolation

Enforced at three levels, deliberately redundantly:

1. **Structurally** — `enforce_path_scope` on the v1 router resolves the grant for
   every `project_id`/`environment_id` path parameter. A route added tomorrow
   inherits it.
2. **Per route** — `require_project` remains at the choke points the phase code
   calls directly.
3. **Live** — `e2e-smoke-hardening.sh` asserts refusals over real HTTP, because
   level 1 and 2 were both green in unit tests while the live server was wide
   open (§5).

### 3.4 Fail closed

Authorization decisions read the caller's context through
`require_auth_context()`, which raises when the context is absent. No guard skips
itself. "I could not determine who you are" resolves to `401`, never to "carry
on".

### 3.5 Bounded everything

Every list endpoint is paginated and capped; sweep batches are sized by config;
queue depth is observable (`argus_ingestion_queue_depth`) and drains to zero;
retention sweeps remove what has aged out. There is no unbounded `select` in a
request path.

### 3.6 Reversibility for actions

Autonomous actions ship disabled; each requires a policy that explicitly permits
it; each records a rollback plan *before* it executes; each is hash-chained in an
audit log; and a global emergency stop short-circuits any of them. The control
plane is consulted by the workers and sweeps themselves, so pausing is real
rather than cosmetic.

---

## 4. Where the seams are

Honest assessment of the coupling that exists:

| Seam | Nature | Assessment |
| --- | --- | --- |
| ingestion → graph/detection | Redis queue | clean; one-directional, no shared transaction |
| detection → correlation → incident | in-process services, shared rows | tight by design; these are one reasoning chain |
| incident → causal → reproduction → fix | sequential reads of stored evidence | clean; each phase reads rows, none calls the next directly |
| forecasting → feature snapshots | persisted snapshot per forecast | clean, and the reason forecasts are reproducible |
| learning → all phases | read-only over stored rows | clean; learning never mutates its sources |
| remediation → control plane | DB-backed switches read by workers | the only place a phase reaches into another's execution |

The one coupling worth watching is **detection → correlation**: they share the
event vocabulary and are tuned against each other. They are correctly colocated;
splitting them would be a mistake.

### Central dependency: `app/core/`

`config.py`, `edge.py`, `security.py`, `database.py` and `queue.py` are depended
on by everything. That is appropriate for cross-cutting concerns, but it means a
defect there is a defect everywhere — which is exactly what §5 was.

---

## 5. Audit findings

The hardening pass audited the codebase against the plan and found eight classes
of real defect. The pattern in every case was a **gap between what the tests
exercised and what the server does**.

| # | Finding | Severity | Resolution |
| --- | --- | --- | --- |
| 1 | Authorization was a no-op under uvicorn: `BaseHTTPMiddleware` did not propagate the auth `ContextVar`, and guards skipped when it was absent | **Critical** — cross-tenant read *and* write | pure-ASGI middleware; `require_auth_context()` fails closed; structural test + live gate |
| 2 | Grants enforced only in routes that remembered to call the helper; `environments` validated mere existence, so a scoped token could create in a foreign project | **High** — cross-tenant write | router-level `enforce_path_scope` |
| 3 | A stock OTel exporter could not ingest (top-level `projectId` is impossible for it to send) | **High** — the documented onboarding path was dead | `projectId` optional, resolved from the credential |
| 4 | Detection and correlation applied the run's environment to a project-wide sweep, reading no telemetry | **High** — silent no-op | partitioned per environment + regression suite |
| 5 | Alembic revision cycle | Boot-blocking | fixed; migration chain now tested against real Postgres |
| 6 | `CHAR`/`UUID` FK mismatch; missing `server_default`; `bootstrap_admin_token` crash on a second admin token | Boot-blocking | all three fixed and pinned |
| 7 | 11 components lost their post-action confirmation to `router.refresh()` | Medium — UX, app-wide | store outside React; browser-verified |
| 8 | `/system-map` returned `200` with an empty shell (raw `fetch`, no auth header) | Medium — a status-code health check would call it healthy | fixed; UI gate asserts real content |

**The systemic lesson**, recorded because it should shape future work: tests that
call the ASGI app in-process do not exercise the runtime the product ships on.
That is why `e2e-smoke-hardening.sh` exists, and why it asserts over real HTTP
against a real database.

---

## 6. What the architecture does well

* **The phase chain is genuinely linear.** Incident → causal → reproduction →
  fix → verification reads stored evidence at each step. No phase reaches
  forward, so a failure in one degrades that phase rather than the chain.
* **Derived data is disposable.** Every learning/forecast/analysis table can be
  dropped and rebuilt from the phase 0–3 rows. Nothing irreplaceable is derived.
* **Purity where it pays.** The temporal, dependency, change and trace analyzers
  are deterministic functions of stored rows, which is why their tests can assert
  exact orderings.
* **One vocabulary.** Component, environment, incident, evidence and confidence
  mean the same thing in every phase, so cross-phase views (the unified platform,
  the case timeline) compose without translation layers.
* **Observability is first-class.** Metrics, health probes and queue depth exist
  for the operational questions an SRE actually asks.

## 7. Residual risks

Not defects — accepted positions, each with a reason:

1. **Background throughput is not the scaling axis.** Sweeps are gated on
   `BACKGROUND_JOBS_ENABLED` and, since the hardening pass, elected through an
   advisory lock (`app/services/sweep_leader.py`): one worker runs a given sweep
   and a second stands down instead of duplicating it. The cost is that sweep
   throughput still does not scale with worker count.
2. **Single-region, and failover is manual.** `docker-compose.ha.yml` adds a
   streaming replica plus WAL archiving for point-in-time recovery, but reads are
   not routed to the replica, promotion is a deliberate operator action, and there
   is no cross-region replication story. The `pg_dump` artifact remains the
   portable one.
3. **Detection tuning is heuristic.** Rules are declared and bounded, and their
   limits are surfaced, but the thresholds need real traffic to tune properly.
4. **The learning layer is only as good as history.** Cold-start behaviour is
   honest (`UNKNOWN`), not impressive.
5. **Model deployment is manual.** Drift flags for review; nothing auto-retrains
   or auto-activates. That is a deliberate safety property, and it means model
   improvement is an operator task.

---

## 8. Verdict

The architecture is coherent and the phase boundaries are in the right places.
The system's distinguishing property — that it refuses to over-claim — is
implemented rather than advertised, and is enforced by tests across every phase.

The most serious problem found was not in any phase engine: it was in the shared
edge, where a framework detail silently disabled authorization while every test
passed. That has been fixed with a fail-closed design and a live gate that makes
the class of defect visible next time.

**Recommended next work, in order of value:**

1. Run the load harness at higher concurrency and record a real envelope
   (the measured table stops at 8 clients).
2. Instrument sweep runtime, so background throughput has a number too.
3. Take the deferred Next.js 16 upgrade recorded in
   [supply-chain-triage.md](supply-chain-triage.md) — the advisories are triaged
   against the features ARGUS uses, not eliminated.
4. Mark the live-gate CI jobs as required status checks on `main`. The jobs and
   the gates they run already exist (`.github/workflows/integration.yml`);
   requiring them is a repository setting, not code.
