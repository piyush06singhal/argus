# ARGUS Production Readiness

What has actually been verified, how to reproduce it, what was broken and is now
fixed, and what remains a limitation. Nothing in this document is aspirational:
every number was produced by a command in §2, run against the stack shipped in
this repository.

**Status: ready to operate.** With the limitations in §7 stated plainly and the
deployment guidance in §6 followed. Where a number is missing, it is missing on
purpose rather than estimated.

---

## 1. What "ready" means here

ARGUS is a reliability-intelligence platform, so "production ready" is defined as
five properties, each with evidence:

| Property | Claim | Evidence |
| --- | --- | --- |
| **Correct** | the platform behaves as documented | 2,187 backend tests, 263 web tests, 19 live gates |
| **Isolated** | one project cannot see or affect another | live HTTP gate asserting cross-project refusal |
| **Safe by default** | autonomous action is inert until a policy exists (a missing policy resolves to `OBSERVE_ONLY`) and is policy-gated | Phase 9 refusal suite + live gate |
| **Honest** | no output claims more than its evidence | epistemic tests in every phase suite |
| **Operable** | bounded queries, bounded queues, backups, rollback | capacity report + ops procedures |

The hardening pass that produced this document found and fixed **eight classes of
real defect** — including one authorization bypass that the entire unit suite
could not see (§4). That is the most important thing on this page: the platform
is ready because its failures were found and closed, not because none were
looked for.

---

## 2. Validation matrix

Reproduce everything with `bash infrastructure/verify-all.sh` (or the individual
commands below).

### Backend

| Suite | Command | Result |
| --- | --- | --- |
| Unit + integration (developer default, SQLite) | `cd apps/api && ./.venv/bin/python -m pytest tests/ -q` | **2,173 passed, 15 skipped** |
| Unit + integration (PostgreSQL, as CI runs it) | same, with `ARGUS_TEST_DB=postgresql+asyncpg://…` (the compose Postgres is on **5433**) | **2,187 passed, 1 skipped** |
| Concurrency proofs (PostgreSQL only) | `./.venv/bin/python -m pytest tests/test_project_lock_postgres.py -q` | 5 passed — the per-project mutex serialises writers, emits `FOR UPDATE` on the production dialect, and four concurrent detect+correlate passes lose nothing |
| Lint | `./.venv/bin/ruff check app tests` | clean |
| Types | `./.venv/bin/mypy app` | clean (239 files) |
| Migrations (real Postgres) | `./.venv/bin/python -m pytest tests/test_migrations_postgres.py -q` | 4 passed |

The skips are environment-conditional (each carries its reason): a PostgreSQL-only
suite has nothing to prove on SQLite, and the reverse. Nothing is skipped
"because it was failing" — CI runs the PostgreSQL configuration, so every
row above is executed somewhere on every pull request.

### Frontend

| Suite | Command | Result |
| --- | --- | --- |
| Unit | `cd apps/web && npx vitest run` | **263 passed (14 files)** |
| Types | `npx tsc --noEmit` | clean |
| Build | `npm run build` | succeeds |
| Browser (Chromium) | `npx playwright test` | **5 passed** |

### Live gates (against a running stack)

Each gate drives the **real** HTTP API and the real database — no mocks, no
fixtures standing in for the platform.

| Gate | Command | Result |
| --- | --- | --- |
| Phase 0/1 — foundation & ingestion | `bash infrastructure/e2e-smoke-phase1.sh` | 46 passed |
| Phase 2 — knowledge graph | `…-phase2.sh` | 28 passed |
| Phase 3 — anomaly & incident intel | `…-phase3.sh` | 103 passed |
| Phase 4 — causal analysis | `…-phase4.sh` | 70 passed |
| Phase 5 — reproduction | `…-phase5.sh` | 104 passed |
| Phase 6 — AI debugger | `…-phase6.sh` | 159 passed |
| Phase 7 — fix generation | `…-phase7.sh` | 90 passed |
| Phase 8 — predictive reliability | `…-phase8.sh` | 42 passed |
| Phase 9 — safe remediation | `…-phase9.sh` | 69 passed |
| Phase 10 — reliability intelligence | `…-phase10.sh` | 87 passed |
| Phase 11 — unified platform | `…-phase11.sh` | 90 passed |
| Onboarding — connect a system | `…-onboarding.sh` | 45 passed |
| UI — every route renders | `…-ui-smoke.sh` | 73 passed |
| **Security — auth & isolation** | `…-hardening.sh` | **15 passed** |
| **Faults — Redis outage recovery** | `…-faults.sh` | **10 passed** |
| **Backup — restore rehearsal** | `…-backup.sh` | **15 passed** |
| **Observability — alerts & dashboard** | `…-observability.sh` | **22 passed** |
| **HA — replication & PITR** | `…-ha.sh` | **19 passed** |
| **SSO — single sign-on end to end** | `…-sso.sh` | **65 passed** |
| Browser — click paths | `cd apps/web && npx playwright test` | 5 passed |

**Total: 1,157 live assertions across 19 gates.**

---

## 3. Capacity envelope

Measured, not estimated. Full method and per-scenario tables are in
[argus-benchmark-report.md](argus-benchmark-report.md).

| Scenario | Throughput | p50 | p95 | p99 | 5xx |
| --- | --- | --- | --- | --- | --- |
| `ingest` (async enqueue) | 171 req/s | 42 ms | 62 ms | 194 ms | 0 |
| `ingest-sync` (full pipeline) | 313 req/s | 22 ms | 34 ms | 124 ms | 0 |
| `metric` (single sample) | 274 req/s | 26 ms | 39 ms | 140 ms | 0 |
| `read` (dashboard mix) | 249 req/s | 27 ms | 46 ms | 115 ms | 0 |
| `mixed` (realistic shape) | 294 req/s | 24 ms | 36 ms | 131 ms | 0 |

Measured on Apple Silicon (6 CPU), single-container topology, 8 concurrent
clients, with the edge limiter lifted to expose the pipeline ceiling. **These are
tested-on-this-hardware figures, not a capacity promise** — run
`infrastructure/load-soak.py` on your hardware before sizing anything.

With the shipped defaults the edge limiter (600 req/min sustained, 120 burst per
token) is the binding constraint, and it holds: a deliberate 19,319-request
burst produced 19,051 `429`s and **zero `5xx`** (re-confirmed in this pass: a
300-request burst at the shipped defaults returned 192 `200` / 108 `429`, and no
`5xx`).

**Where the ceiling is.** The same scenario run at four concurrency levels
(limiter lifted, 15 s windows) shows the stack saturating at roughly 8 clients —
more concurrency buys latency, not throughput:

| Concurrency | `read` throughput | `read` p50 | `read` p99 | `metric` throughput | `metric` p50 | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 243 req/s | 28 ms | 112 ms | 215 req/s | 32 ms | 0 |
| 16 | 182 req/s | 78 ms | 264 ms | 194 req/s | 75 ms | 0 |
| 32 | 190 req/s | 148 ms | 357 ms | 172 req/s | 165 ms | 0 |
| 64 | 198 req/s | 231 ms | 1,156 ms | 186 req/s | 283 ms | 0 |

The useful signal is the last column: **zero `5xx`, zero timeouts at every
level, including 64 concurrent clients.** The stack degrades into latency rather
than failure. The plateau belongs to the shared host (API + Postgres + Redis +
sweeps on six CPUs), not to the architecture — [argus-benchmark-report.md](argus-benchmark-report.md) §4b has the
full method and per-scenario tables.

Backlog behaviour: a burst of ~9,000 queued jobs drains to **0** with no
intervention and no lost work.

**Backup & restore rehearsal.** `bash infrastructure/backup.sh drill` restores
the newest dump into a scratch database with `--exit-on-error`, requires every
counted table to be present, and compares row counts against a floor recorded
before the dump, allowing only the tables' own churn (2%). Verified in this pass
against the live 344,861-row database: a real archive restores completely, a
truncated copy is refused by **both** `verify` and `drill`, an inflated floor is
refused by `drill` (so the comparison is load-bearing rather than decorative),
the drill still passes after real telemetry is ingested behind the dump, and no
scratch database is left behind. `infrastructure/e2e-smoke-backup.sh` is the
gate: **15 assertions**, green (`ONBOARDING RESULT`, `hardening gate` and
`backup gate` all report 0 failed through `verify-all.sh`).

**Single sign-on, driven end to end.** The OIDC unit suite runs the real client
against an in-process provider over a `MockTransport`, which proves the client
and nothing about the assembled system — no socket, no browser redirect, and no
running middleware. `infrastructure/e2e-smoke-sso.sh` closes that gap: it starts
a **strict stub identity provider** (discovery + JWKS, S256 PKCE verified
against the challenge, client authentication, one-shot authorization codes) and
a second API process with `OIDC_ENABLED=true` on their own ports, then drives
login → authorization redirect → callback → session, and asserts that the
session the callback minted is **accepted by the edge** on an ADMIN-only route.
The refusals are asserted the same way: a replayed state, a forged state, an
unverified email, a disabled identity and a provider-side revocation all have to
produce their stable error codes. **65 assertions**, green; the gate tears both
processes down and removes every row it created, so it is re-runnable.

---

## 4. Defects found and fixed in this pass

Recorded because the pattern matters more than the list: every one of these was
invisible to the test suite that was "passing".

### 4.1 Authorization was a no-op in the live server *(critical)*

**Symptom.** With authentication enforced, a token granted exactly one project
could read and list **every** project, and a per-source ingest token could write
telemetry into **any** project.

**Cause.** `AuthMiddleware` was a Starlette `BaseHTTPMiddleware`. That class
starts the downstream app in a task whose context is captured *before*
`dispatch` runs, so the `ContextVar` holding the caller's identity never reached
the endpoint. Every authorization guard read that variable through
`get_auth_context()`, and every guard was written as `if auth is not None: …` —
so a missing context silently meant "allowed".

**Why 45 security tests passed anyway.** `TestClient` runs the endpoint in the
caller's own context, where the variable *is* visible. The suite was testing a
code path the server does not take.

**Fix, three parts.**
1. `AuthMiddleware` is now **pure ASGI**, so the context reaches the endpoint
   wherever it runs.
2. Guards go through `require_auth_context()`, which **fails closed** — a missing
   context is a `401`, never a skipped check.
3. A structural test asserts the middleware is *not* a `BaseHTTPMiddleware`, and
   `infrastructure/e2e-smoke-hardening.sh` asserts the refusals over real HTTP.

### 4.2 Project grants were enforced only where a route remembered

34 routes carry a `project_id` path parameter. The grant check lived in a helper
those routes had to call, and several (`environments`, for one) validated only
that the project *existed* — a scoped token could **create** resources in a
foreign project (confirmed live, `201 Created`).

**Fix:** `enforce_path_scope` is attached to the v1 router itself, so every
project-scoped route inherits the check — including routes added later. An
`environment_id` path resolves through its environment's own project, so it
cannot be used as a side door.

### 4.3 A stock OpenTelemetry exporter could not ingest at all

The OTLP request schema required a top-level `projectId`, which no OTel exporter
or Collector can inject into an `ExportTraceServiceRequest`. The documented
"point your collector here" path was therefore impossible.

**Fix:** `projectId` is optional; when absent the project is resolved **from the
credential** (an ingest token or single-grant token implies its project; an
unscoped or multi-grant token must name one and otherwise gets a `400`). This is
stricter, not looser: the caller no longer names its own destination.

### 4.4 Detection and correlation silently read no telemetry

Both applied the *run's* environment to a per-project sweep, so a project-wide
detect pass matched nothing. Now partitioned per environment, with a regression
suite.

### 4.5 Boot-blocking defects

* an Alembic revision cycle;
* a `CHAR`/`UUID` foreign-key type mismatch;
* missing `server_default` on new tables (every insert violated `NOT NULL`);
* `bootstrap_admin_token` raised when a second admin token existed, so minting a
  recovery token made the API unable to restart.

All four are now covered by `test_migrations_postgres.py`, which runs the whole
migration chain against a throwaway PostgreSQL database and then writes through
the ORM — the check that would have caught them the first time.

### 4.6 An app-wide confirmation-loss bug

11 components showed a post-action confirmation that `router.refresh()` then
discarded, so the user never saw it. Fixed with a store that lives outside React,
pinned by tests, and verified in a live browser.

### 4.7 A page that answered `200` with nothing in it

`/system-map` used raw `fetch` with no `Authorization` header; with auth enforced
it rendered an empty shell while returning `200`. Health checks that only look at
status codes would have called that healthy.

---

## 5. Security posture

| Control | State |
| --- | --- |
| Authentication | required on every route except `/`, health, `/metrics` — asserted by OpenAPI introspection |
| Credential types | admin / scoped API token (role + project grants) / per-source ingest token |
| Project isolation | enforced structurally at the router; asserted live |
| Role floor | writes require `OPERATOR`+; token management requires `ADMIN` |
| Secrets at rest | tokens stored as SHA-256 hashes; raw value shown once |
| Rotation & revocation | per-source rotate/revoke endpoints; audit rows for both |
| Webhook integrity | HMAC signature over the raw body, replay-refused |
| Rate limiting | token bucket per credential (600/min, burst 120), **shared through Redis** so the ceiling is global across replicas, with an alerted per-process fallback; `429` + `Retry-After` |
| Identity federation | optional OIDC (authorization-code + PKCE, JWKS ID-token verification, claim→role/grant mapping, single-use state); inert until `OIDC_ENABLED` |
| Body limits | declared-oversize rejected with `413` before being read |
| Audit trail | authentication attempts recorded; remediation actions hash-chained |
| Fail-closed guards | a missing auth context is a refusal, never a bypass |

Full detail: [security-architecture.md](security-architecture.md).

---

## 6. Deployment guidance

**Topology.** For a team-scale deployment the default single container is
sufficient. For sustained writes above ~100 req/s, split the roles so sweeps are
off the request path:

```bash
BACKGROUND_JOBS_ENABLED=false docker compose up -d api
docker compose --profile worker up -d worker
```

**Scaling the worker.** Sweeps are gated twice, and the two gates answer
different questions. `BACKGROUND_JOBS_ENABLED=false` keeps the *HTTP* processes
out of the sweep business entirely; the **sweep lease** then decides *which*
worker runs each pass, so worker replicas can be added without duplicating work:

```bash
# three workers, one sweep per interval between them
docker compose --profile worker up -d --scale worker=3
```

The lease is a PostgreSQL advisory lock named per sweep (`app/services/
sweep_leader.py`), taken for the duration of a pass and released on exit — and,
because it is session-scoped, released by PostgreSQL when a worker dies. There is
no heartbeat to tune and no stale leader to reap: a crashed worker costs one
interval, not the sweep. The losers skip the tick quietly, which is the intended
outcome rather than an error. Proved against real PostgreSQL by
`tests/test_sweep_leader_postgres.py` — including a 4-way race that must produce
exactly one leader, and a killed session whose lease must free itself.

**Tunable limits** (all env, none baked into images): `RATE_LIMIT_ENABLED`,
`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST`, `RATE_LIMIT_BACKEND`,
`MAX_REQUEST_BODY_BYTES`, `MAX_JSON_DEPTH`, `SEED_DEMO`, `API_ENVIRONMENT`,
`BACKGROUND_JOBS_ENABLED`, the `OIDC_*` federation settings, and
`INSTANCE_ID`/`INSTANCE_REGION` for distinguishing replicas in a dashboard.

**Backup & restore.** `infrastructure/backup.sh` dumps the database, proves the
archive is complete (table of contents *and* full decompression), and rehearses a
restore with `drill` — which restores into a scratch database, requires every
counted table, and compares against a floor recorded before the dump. Procedures,
rollback steps and the reasoning behind the count tolerance are in
[operations.md](operations.md) §4.

**Upgrades.** `alembic upgrade head` runs before uvicorn binds the port, and the
revision graph is checked against a real database in CI. The rollback story
(downgrade per revision, and restore-from-backup as the fallback) is documented
in [operations.md](operations.md).

**Do not** run `API_ENVIRONMENT=production` with docs enabled or with
`AUTH_DISABLED` set — the config guard refuses to boot in the latter case.

---

## 7. Limitations

Stated deliberately. Each is a real boundary, not a to-do disguised as one.

1. **gRPC is not served.** OTLP/HTTP is fully supported in both encodings — Protobuf (a stock collector's default) and JSON — but the gRPC listener on 4317 is not exposed. Every collector and SDK can target OTLP/HTTP, so this is a deployment choice rather than a gap.
2. **Write-heavy load above 8 concurrent clients is unmeasured.** The §3
   sweep characterises `read` and `metric` to 64 clients; `ingest`,
   `ingest-sync` and `mixed` are characterised only at 8. What is known at 64 —
   no `5xx`, no timeouts — is the part that matters, but do not read a
   throughput figure for the write path at that concurrency.
3. **Load was synthetic and single-host.** Postgres, Redis and the API shared one
   machine, so disk contention is inside the measured latency.
4. **Model quality is bounded by your history.** With little telemetry, forecasts
   report `UNKNOWN`/`LOW` confidence and say so in their limitations rather than
   guessing; calibration stays `UNKNOWN` until outcomes accumulate.
5. **Reproduction needs a runnable environment.** Without a matching sandbox
   image, experiments fail honestly (`ENVIRONMENT_UNAVAILABLE`) instead of
   pretending to have reproduced anything.
6. **Code intelligence needs a registered repository.** Telemetry alone gives
   incidents and causal analysis, not source-level debugging.
7. **Remediation is inert until a policy exists.** The master switch
   (`REMEDIATION_EXECUTION_ENABLED`) defaults to on as the operator kill switch, but
   a project with no policy row resolves to `OBSERVE_ONLY`, so nothing executes
   without a deliberate policy — and the shipped action set covers only ARGUS's own
   control plane. This is a design decision, not an unfinished feature.
8. **HA is shipped; multi-region failover is a topology, not a feature.** A
   streaming replica and continuous WAL archiving/point-in-time recovery ship in
   `docker-compose.ha.yml` (gated by `e2e-smoke-ha.sh`), but there is **no
   automatic failover** — promotion is a deliberate operator action. Reads are
   not routed to replicas, and Redis is a single node, so a region that loses its
   Redis falls back to per-process rate limiting and a region that loses its host
   is a failover decision. [high-availability.md](high-availability.md) states
   exactly what a multi-region topology adds and what stays the operator's.
9. **The frontend runtime carries triaged — not eliminated — advisories.**
   Next.js 14.2.35 is reported by `npm audit` with 12 high/critical advisories.
   Each was checked against what ARGUS actually uses — no `next/image`, no server
   actions, no `middleware.ts`, Linux containers, and an authenticated rewrite
   proxy — and the applicability evidence is in
   [supply-chain-triage.md](supply-chain-triage.md). The only remediation npm
   offers is the breaking Next 16 upgrade, which is **deferred and tracked**
   there with its migration surface and exit criteria. The residual risk is
   accepted for a self-hosted deployment inside a private network (see
   [SECURITY.md](../SECURITY.md)), it is re-reviewed on every `next` bump, and
   `ci.yml` fails on any new untriaged high/critical finding.

---

## 8. Recommended first week

1. `docker compose up -d` and sign in with the bootstrap token (§quickstart).
2. Run `bash infrastructure/verify-all.sh` — see 1,157 live assertions pass on
   your machine before you trust the platform with your telemetry.
3. Connect one non-critical service ([onboarding.md](onboarding.md)) and watch
   `/system-map` populate.
4. Run the load harness against your hardware and record the envelope
   ([argus-benchmark-report.md](argus-benchmark-report.md)).
5. Only then decide whether remediation should be enabled, and for which
   actions ([safe-autonomous-remediation.md](safe-autonomous-remediation.md)).
