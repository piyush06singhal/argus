# ARGUS Changelog

All notable, user-visible changes to ARGUS are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Hardening (production-readiness pass)

#### Operational readiness

- **Single sign-on (OIDC).** Optional OpenID Connect login alongside API
  tokens: authorization-code flow with PKCE, ID-token verification against the
  provider's JWKS, a claim→role/grant mapping, and database-backed single-use
  `state`. Every attempt — success or refusal, with a stable reason code — is
  written to the authentication audit trail. Inert until `OIDC_ENABLED=true`, and
  a half-configured provider is refused at boot. Console: SSO on `/connect` and a
  session/identity view at `/settings/identities`.  Proved end to end by
  `infrastructure/e2e-smoke-sso.sh` (65 assertions): a strict stub identity
  provider and a second OIDC-enabled API process over a real socket, driving
  login → authorization redirect → callback → a session the edge then accepts,
  plus every refusal (replayed state, forged state, unverified email, disabled
  identity, provider-side revocation).
- **Alert rules and dashboards for ARGUS itself.** 20 Prometheus alert rules and
  a 22-panel Grafana overview ship under `infrastructure/observability/`, opt-in
  via the `observability` compose profile. Every rule carries a severity, a
  summary and a runbook link into `docs/operations.md`, and the live gate
  `infrastructure/e2e-smoke-observability.sh` (22 assertions) fails if a rule or
  panel references a series the live scrape does not export.
- **Scheduled backup.** `infrastructure/backup-scheduler.sh` (the `backup`
  compose profile) dumps on an interval, rehearses a restore on a longer
  interval, and prunes old dumps; each run records its outcome in Postgres and
  freshness is alerted on (`ArgusBackupStale`, `ArgusBackupNeverSucceeded`,
  `ArgusBackupDrillStale`).
- **PostgreSQL high availability and point-in-time recovery.**
  `docker-compose.ha.yml` adds continuous WAL archiving and a streaming replica
  on host port 5434. `infrastructure/e2e-smoke-ha.sh` (19 assertions) proves the
  replica streams, refuses writes and catches up, and **rehearses a real recovery
  to a chosen moment** (with a no-target control run). Failover remains a
  deliberate operator action — see `docs/high-availability.md`.
- **Shared, Redis-backed rate limiting.** `RATE_LIMIT_BACKEND`
  (`auto`/`redis`/`memory`) makes the ceiling global across workers and hosts
  instead of per process, with an honest per-process fallback that is reported
  (`argus_rate_limit_backend`, `argus_rate_limit_fallbacks_total`) and alerted on.
- **Self-observability metrics.** `/metrics` now carries per-process series that
  only exist inside a running process — instance identity and region
  (`argus_instance_info`), the rate-limit backend in use, and backup/drill
  freshness.
- **Documentation-consistency guard** (`apps/api/tests/test_docs_consistency.py`):
  runbook links must resolve, alert names in prose must be real, every live gate
  must be named in the README **and wired into `verify-all.sh`**, advertised
  alert-rule and live-gate counts must match the files, and no doc may deny a
  capability the code ships — the drift a final audit found is now a failed
  build.

#### Security

- **Added authentication and authorization.** Every non-public API route now
  requires a bearer token. On first boot ARGUS generates a root admin token
  and prints it to the container log (and writes it to a root-only file).
  Roles: `ADMIN`, `OPERATOR`, `VIEWER`, with per-project grants on tokens.
  Set `AUTH_DISABLED=true` only in development — production refuses to
  boot with auth disabled.
- **Ingestion is no longer open.** OTLP endpoints require a per-source ingest
  token; the ingestion webhook verifies HMAC signatures (replay-resistant).
  Rotate tokens via `POST /api/v1/ingestion/sources/{id}/rotate-token`.
- **Rate limiting** at the API edge (token bucket, per credential then client
  IP, `429` with `Retry-After`), always on, configuration-driven, and **shared
  through Redis** by default so the ceiling is global across replicas.
- **Request-body limits** (`413` above `MAX_REQUEST_BODY_BYTES`) protect the
  API from oversized payloads.
- Dependency vulnerabilities resolved: FastAPI 0.141.1, Starlette 1.6.0,
  Next.js 14.2.35, pytest 9.1.1 — `pip-audit` and `npm audit` are clean and
  enforced in CI.

#### Fixed

- **The license was stated three different ways.** The `LICENSE` file is MIT and
  this changelog said MIT, the README badge said Apache 2.0 and its License
  section said no license was present at all. Everything now points at the MIT
  license that actually ships.
- **The README documentation index was incomplete.** The Phase 7 report, the
  telemetry reference and the production-hardening plan were reachable only by
  guessing a filename; every document is now listed, and every relative link
  resolves.
- **A leaked restore-drill scratch database was never reaped once two had
  accumulated.** The stale-scratch reaper read the leftover names through
  `tr -d '[:space:]'`, which also deletes the newline *separators* — so two
  leftovers arrived as one unparseable `argus_drill_Aargus_drill_B` token and
  neither was dropped. Each leaked scratch database is a full copy of the dump,
  so the leak grew the disk silently and relied on `docker compose down -v` to
  ever clear. The live backup gate had the same defect in its own leftover
  detection (and mismatched pre-existing names in its cleanup); both now read
  one name per line, and the gate's reaping assertion creates *two* stale
  databases on purpose, so the regression cannot return unnoticed.
- **A doc claimed OTLP/Protobuf was unsupported when it ships.** The onboarding
  guide twice said "Protobuf is not accepted"; the edge middleware decodes it.
  Corrected, and `test_docs_consistency.py` now fails if any doc denies a
  capability the code provides.
- **A response did not mean the write was committed.** FastAPI runs a
  dependency's teardown *after* the response has been sent, and `get_db`
  committed only there — so a `DELETE` could return `204` while the very next
  `GET` still saw the row. Measured against the running stack, **ten of twelve**
  immediate reads after a delete saw the deleted project. It was not a
  hypothetical: it is why the phase-10 and phase-11 gates and the
  project-delete cascade check failed intermittently while asserting something
  else, and why the SLA "0 failures" the harness printed could not be
  reproduced by hand. `CommitBeforeResponseMiddleware` now commits the
  request's session immediately before `http.response.start` is forwarded; the
  error path is untouched (an unexpected exception never reaches that point, so
  `get_db` still rolls back) and refusals still leave their audit trail. After
  the fix, **zero of ten** immediate reads are stale, and the new
  `tests/test_hardening_commit_order.py` asserts the ordering contract directly
  — the in-process client drains a response, so an end-to-end test alone would
  have passed with or without the fix.
- **The verification harness misread three of its own gates.** Phases 4, 5 and 6
  print `passed: N` / `failed: 0`, a shape `verify-all.sh` did not match, so a
  fully green gate was reported as `no summary line (did the gate run?)` and
  counted as a failure. The advertised "0 failures" was therefore false for the
  one command the docs tell an operator to run. The summary pattern now covers
  that shape and failure detection covers its `failed: N` line.
- **The HA gate failed a working replica on timing.** It required
  `pg_stat_replication.state = streaming` within 60 s of bootstrap, but a
  standby with a `restore_command` replays the archive before it connects its
  walreceiver — minutes, on a database this size. The next assertion (that a
  write propagates) passed immediately afterwards, which is how the gate was
  failing a healthy replica. The wait is now bounded at 300 s with progress, and
  the gate is green against a **freshly bootstrapped** replica.
- **The observability gate's assertion count moved.** Its deferred-series check
  only asserted when there were deferred series, so the same gate reported 21 or
  22 assertions depending on what data existed — and a number that moves is a
  number no documented total can be checked against. Both branches now assert.
- **The security architecture doc listed SSO as not provided after it shipped.**
  `docs/security-architecture.md` said under "what is deliberately not provided"
  that ARGUS ships "SSO/OIDC or user accounts" — while `app/services/oidc.py` and
  the `/auth/oidc` routes shipped the full authorization-code flow. It also
  claimed authentication "attributes a token, not a person", which stopped being
  true for federated sessions (`external_identities`, and the person's identity
  in the audit reason). Both corrected, and the docs guard now fails on a denial
  of SSO for the same reason it fails on the Protobuf one.

#### Deferred (accepted risk)

- **The Next.js 16 upgrade.** `npm audit --omit=dev` reports 12 high/critical
  advisories against the pinned Next.js 14.2.35. None is reachable in ARGUS's
  shape — no `next/image`, no server actions, no `middleware.ts`, Linux
  containers only, and an authenticated rewrite proxy — and each is triaged with
  applicability evidence in `docs/supply-chain-triage.md`. The only remediation
  npm offers is the **breaking** Next 16 upgrade. Rather than leave that
  implied, the deferral is recorded explicitly: the residual risk, who accepts
  it, what the migration involves (React 19, Async Request APIs, `next lint`
  removal) and the exit criteria that close it. Accepted and visible, not
  silently deferred — and re-reviewed on every bump of `next`.

#### Added

- CI/CD: fast checks (lint, typecheck, unit tests, web build) on every push;
  integration tests on PRs to `main`; weekly supply-chain audits.
- `SEED_DEMO` toggle: set `SEED_DEMO=false` for a clean production database
  (the first-boot demo seed is now opt-in-by-default only in development
  compose profiles).
- `CHANGELOG.md`, `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, issue and pull-request templates.
- Live onboarding gate (`infrastructure/e2e-smoke-onboarding.sh`) that proves
  the "new user's first hour" against a non-demo project.
- **Fault-injection gate** (`infrastructure/e2e-smoke-faults.sh`): stops Redis
  mid-flight and proves over real HTTP and Postgres that the outage is reported
  as degraded (never silent), telemetry ingests through the synchronous fallback
  with no data dropped, health recovers, queues drain, and zero rows are lost.
  Wired into `verify-all.sh`.
- `/api/v1/ingestion/sources` is paginated like every other collection
  endpoint (`page`/`page_size`); the response shape is unchanged.
- **OTLP/Protobuf ingestion.** Both OTLP/HTTP encodings are now accepted —
  Protobuf, which is what a stock OpenTelemetry collector sends by default, and
  JSON. The Protobuf body is decoded into the same event shape the JSON path
  uses, so no route, schema or validation changed; `opentelemetry-proto` supplies
  the descriptors.
- **Sweep lease (leader election) so worker replicas can scale.** Each background
  sweep takes a PostgreSQL advisory lock named per sweep for the duration of a
  pass. With three worker replicas, one runs each interval and the others skip
  the tick quietly, so replicas no longer duplicate every sweep pass. Proved
  against real PostgreSQL (`tests/test_sweep_leader_postgres.py`), including the
  crash case: because the lock is session-scoped, PostgreSQL releases it when a
  worker dies, so a crashed worker costs one interval rather than the sweep.
- **Backup restore-rehearsal gate** (`infrastructure/e2e-smoke-backup.sh`, 15
  assertions) and a `drill` subcommand in `infrastructure/backup.sh`: dump →
  verify (table of contents *and* full decompression) → restore into a scratch
  database with `--exit-on-error` → compare per-table counts against a floor
  recorded before the dump. The gate falsifies itself: a truncated archive must be
  refused by both `verify` and `drill`, and an inflated floor must be refused, so
  the assertions are known to be load-bearing. Wired into `verify-all.sh` and CI.
- **Concurrency characterisation to 64 clients**
  ([argus-benchmark-report.md](docs/argus-benchmark-report.md) §4b). The stack
  saturates at roughly 8 concurrent clients (~180-240 req/s on a 6-CPU host);
  beyond that, concurrency buys latency rather than throughput, and **zero `5xx`,
  zero timeouts, zero errors** were observed at every level including 64.
- **Concurrency proofs against real PostgreSQL**
  (`apps/api/tests/test_project_lock_postgres.py`). The per-project write mutex —
  the fix for a genuine production deadlock — is now proved rather than asserted:
  a second writer really blocks while the lock is held, the lock really emits
  `FOR UPDATE` on the production dialect (so it cannot silently become a no-op,
  as it does on SQLite), and four simultaneous detect+correlate passes all commit
  with one fingerprint registry row instead of losing a transaction. Skipped on
  SQLite, where a row lock cannot exist; CI runs it against PostgreSQL.
- Accessibility: programmatic labels on the interactive controls that had none
  (graph, snapshot and environment selectors, SLO and configuration form fields),
  and system-map graph nodes are now keyboard-reachable (`Tab`, `Enter`/`Space`,
  with a visible focus ring) instead of pointer-only.
- Browser smoke gate (Playwright, 5 tests / 24 assertions) validating the real UI
  against the live stack: token boundary, the navigation click path, an incident
  through to its causal and remediation surfaces, live panels, and sign-out.
- Backup/restore script for Postgres and the documented upgrade procedure.

#### Documentation (final audit)

- **The README was rewritten to open-source standard.** It was 1,066 lines of
  accumulated phase narration with seven HTML-escaped ampersands, a Quick start table
  that sent readers to the wrong Postgres port, a Roadmap table listing already
  shipped phases (twice), and a limitations section claiming *"No authentication
  yet"* — which was false. It is now 561 lines: what it is, why, five Mermaid
  diagrams (system overview, telemetry path, investigation pipeline, evidence
  model, deployment topologies), a capabilities table, quick start, configuration
  table, the verification matrix with all 17 gates, project layout, security
  model, design principles, honest limitations, a genuine roadmap, and a complete
  grouped index of every document. All five diagrams were validated against
  mermaid's own parser (v11), not eyeballed.
- **The README told operators that `REMEDIATION_EXECUTION_ENABLED` defaults to
  `false`. It defaults to `true`.** What actually keeps remediation inert out of the
  box is the policy regime — a project with no policy row resolves to `OBSERVE_ONLY`,
  which records proposals and never authorizes them — and that is now stated
  precisely in the README, `safe-autonomous-remediation.md`,
  `production-readiness.md` and `onboarding.md`, with the master switch described as
  the operator kill switch it is.
- **Limitations are now separated from work.** The README's limitations section mixed
  design boundaries ("a reproduction is an experiment") with unclosed gaps ("no SSO, no
  HA, no alert rules") in one list. It now splits them: **inherent to the design** — they
  will not change — and **gaps we have not closed**, each of which appears in a new
  [Further upgrades](README.md#further-upgrades) section with why it matters and a
  rough size, plus an explicit "deliberately not on this list" list.
- **False claims corrected across the docs**, each checked against the artifact:
  `api.md` documented `page_size: 50` (the real default is 20, max 100); the
  hardening plan reported "exactly 1 TODO in backend source" (there are none — the
  single match is a regex that *rejects* patches carrying TODO markers), and its
  audit-time figures are now labelled as the pre-hardening baseline;
  `roadmap.md` is labelled a frozen phase contract rather than a to-do list.
- **A "Current limitations" section that still described the Phase 1 system.**
  `docs/architecture.md` §12 said there was "no authentication/authorization",
  "no anomaly detection, no root-cause analysis, no remediation" and that the AI
  trust model was "not enforced at runtime (no AI runs in the data path)". All of
  those shipped and are gated. The section now separates what is established —
  each claim with the gate that proves it — from what is genuinely open.
- **Every stale count corrected against the artifact it describes**: 21 revisions
  into 123 tables, 66 pages, 130,632 backend lines, 41,466 frontend lines, 156
  services, 2,093 / 2,079 backend tests, 248 web tests, 17 live gates with 1,049
  assertions, 232 type-checked modules, 5 browser tests.
- **`docs/development.md` advertised 168 tests.** The developer guide's test
  section was a Phase-0/1 snapshot; it now reports the real suite and points at
  the verification matrix.
- **The ten per-phase delivery reports** now open by saying they are snapshots
  from the run that shipped them, so a stale number in one of them cannot be
  mistaken for current state.
- **Five broken relative links** in `architecture.md`, `data-model.md` and
  `observability-model.md` (they pointed at `docs/x.md` from inside `docs/`).
  Every relative link in every markdown file now resolves.
- `.env.example` gained `BACKGROUND_JOBS_ENABLED`, `INGESTION_WORKER_ENABLED`,
  `AUTH_DISABLED` and the missing **Phase 6** and **Phase 7** sections, and no
  longer describes authentication as "future use" — the bearer-token model,
  roles, grants and the bootstrap-token lookup are stated where the legacy JWT
  fields used to imply nothing existed yet.

#### Changed

- Supported Python runtime pinned to **3.12** everywhere (venvs, Docker, CI);
  `pyproject.toml` declares `requires-python = ">=3.12,<3.13"`.
- Redis persistence configured with AOF (`everysec`) so queued jobs survive
  restarts.
- The API now returns `503` with a structured reason when required
  dependencies (database, Redis) are unavailable, instead of surfacing raw
  connection errors.
- Sweep steps run in isolated savepoints: one failing subsystem no longer
  aborts the remaining work of a pass.

#### Fixed

- **Authorization was a no-op in the running server.** `AuthMiddleware` was a
  Starlette `BaseHTTPMiddleware`, which runs the endpoint in a task whose
  context predates `dispatch` — so the credential a request authenticated as
  never reached the endpoint, and every guard that skipped on a missing context
  became a silent allow. Live, a token granted one project could read and list
  *every* project, and a per-source ingest token could write telemetry into any
  project. The middleware is now pure ASGI, guards fail closed through
  `require_auth_context()`, and `infrastructure/e2e-smoke-hardening.sh` asserts
  the refusals over real HTTP. **The unit suite could not catch this**: the test
  client runs the endpoint in the caller's context, which is exactly why it
  passed.
- **Project grants were enforced only where a route remembered to.** 34 routes
  take a `project_id` path parameter; several (environments, for one) validated
  only that the project existed, so a scoped token could create resources in a
  foreign project. `enforce_path_scope` is now attached to the v1 router, so
  every project-scoped route — including any added later — inherits the check,
  and an `environment_id` path resolves through its own project.
- **A stock OpenTelemetry exporter could not ingest at all.** The OTLP schema
  required a top-level `projectId`, which no OTel exporter can inject into an
  `ExportTraceServiceRequest`, so the documented onboarding path was dead.
  `projectId` is now optional and the project is resolved from the credential
  (stricter: the caller no longer names its own destination).
- Detection and correlation applied the *run's* environment to a project-wide
  sweep, so a manual detect pass silently matched no telemetry.
- `/system-map` answered `200` with an empty shell (raw `fetch` with no
  authorization header) — which a status-code health check would call healthy.
- 11 components showed a post-action confirmation that `router.refresh()` then
  discarded, so the user never saw it.
- Alembic revision cycle; `CHAR`/`UUID` foreign-key mismatch; missing
  `server_default` on new tables (every insert violated `NOT NULL`);
  `bootstrap_admin_token` crashed when a second admin token existed, so minting
  a recovery token made the API unable to restart. The whole migration chain is
  now run against a throwaway PostgreSQL database in CI.
- Unbounded request bodies could exhaust API memory (now `413`).
- Case references compared as strings (`CASE-9` > `CASE-10`); now numeric.
- Concurrent detection could deadlock under load; now serialized per project
  with a deterministic order.
- The `.env.example` `[TEMPLATE]` header broke strict dotenv parsers.
- **Spec-correct OTLP metrics were silently dropped.** Traces and logs descended
  into their scope level; metrics did not, so any conforming metrics payload —
  JSON *or* Protobuf — produced zero events while still answering `200`. The
  onboarding gate now asserts the *accepted count* rather than the status code,
  which is why this class of bug can no longer pass: a `200` with nothing behind
  it is not ingestion.
- **`backup.sh verify` accepted a truncated archive.** It listed the table of
  contents, which lives at the front of a `-Fc` archive, so a file cut off after
  its first megabyte was reported as verified while its data was gone. `verify`
  now decompresses the whole archive as well. A verification a truncated dump can
  pass is worse than none, because it is trusted.
- **The restore drill compared against a moving reference.** Counts were read
  from the live database around the dump, and the high-volume tables are revised
  continuously (`reliability_forecasts` alone deletes and re-inserts hundreds of
  thousands of rows per pass), so the drill reported healthy backups as broken —
  exactly how a real failure gets ignored. The reference is now a floor recorded
  *before* the dump, compared with a 2 % churn tolerance; the load-bearing checks
  are full decompression and `--exit-on-error`.
- **A killed drill leaked its scratch database.** `drill` cleans up in an EXIT
  trap, which a SIGINT, a dropped terminal or a container stop never runs — a real
  leak of 73 MB per interrupted rehearsal. A drill now reaps scratch databases
  older than an hour before creating its own.
- **`backup.sh`'s defaults named a database this stack never creates** (`argus`),
  so the documented backup command failed on a first run.
- **The test suite could be pointed at the wrong database without saying so.**
  The PostgreSQL-gated modules read `ARGUS_TEST_DB`, and a DSN aimed at a
  different project's Postgres failed as an opaque connection error mid-run.
  `conftest.py` now prints the resolved target and warns that the suite deletes
  every row in every ARGUS table there. (The documented target is the compose
  Postgres, which is exposed on **5433**; 5432 is usually another project's.)
- **The API package disagreed about its own version.** `apps/api/pyproject.toml`
  declared `1.0.0` while `APP_VERSION`, `apps/web/package.json` and the published
  maturity all said `0.1.0` — three answers to "what version is this?". Aligned on
  `0.1.0`.
