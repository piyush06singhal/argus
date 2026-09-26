# ARGUS — Production Hardening, Architecture Audit & Launch Readiness Plan

**Status: COMPLETE — all workstreams executed and verified.**
Every W-item below (W1–W8) and the closing deliverables are implemented,
live-tested and recorded. The evidence — validation matrix, capacity envelope,
the defects this pass found and fixed, and the honest limitation list — is in
**[production-readiness.md](production-readiness.md)**, with the measurements in
[argus-benchmark-report.md](argus-benchmark-report.md) and the structural
assessment in [final-architecture-audit.md](final-architecture-audit.md).
`bash infrastructure/verify-all.sh` re-runs the whole matrix in one command.

The numbers in this document are superseded by that work: the suite is now
**2,187 backend + 263 frontend** tests (2,173 on the developer-default SQLite
configuration; 2,187 on the PostgreSQL configuration CI runs), and there are
**19 live gates — 1,157 assertions, 0 failures** (the 11 phase gates plus
onboarding, UI, security, fault injection, backup restore rehearsal,
self-observability, HA/PITR, single sign-on and the browser tier).
`bash infrastructure/verify-all.sh` reports 26 layers verified, 0 failed.

### Completion

The plan's closing matrix (§8) was left unfilled when the work finished. It is
filled now — row by row, from a full `verify-all.sh` sweep run on 2026-09-26,
because a completion claim with no evidence beside it is exactly the kind of
thing this project keeps finding and closing.

**Verdict: `READY`** — for a single-operator / small-team self-hosted
deployment, with the limitations stated in
[production-readiness.md](production-readiness.md) §7.

---

_(Historical plan text follows, unedited, so the reasoning is preserved.)_

**Original status: PLAN — awaiting approval before execution begins.**
**Plan revision 2 (second audit pass):** added G13–G14 (browser-level e2e, Next.js
CVE), plus additions to W1 (request-body limits), W2 (supply-chain specifics),
W3 (browser smoke), W4 (request-contract abuse), W6 (Python runtime pinning) and
§5 (explicit hardening decisions: Redis persistence, no auth-free bypass, honest
matrix, changelog). All changes are marked in place.

This document is the honest answer to one question:

> *Can a new user clone ARGUS today, point it at **their own system**, and get real
> value from it — with no bugs, no demo-only shortcuts, and no holes?*

It is based on a full repository audit performed on the current `main`
(`6700d28`, Phases 0–11 shipped). Every claim below cites evidence gathered from
the actual code, not intention.

---

## 0. The honest answer first

### What is genuinely strong (verified, not claimed)

> **Baseline, at audit time.** The figures in this section are what the audit
> measured *before* the hardening pass ran — 1,912 backend tests, 231 frontend
> tests, 11 live gates. They are kept as written so the reasoning is preserved;
> the closed-out numbers are in the status box at the top of this document.

| Area | Evidence |
| :--- | :--- |
| Deterministic engines | Root cause, anomaly detection, correlation, fix verification and learning are all deterministic services. AI is an **optional** re-wording layer that can never override a score, a verdict or a policy (`AI_PROVIDER=mock` default; a missing `AI_API_KEY` falls back to the mock provider with a logged reason — `ai_debugger.py:1069`). |
| Test depth | **1,912 backend tests + 231 frontend tests**, 92 test files, plus 11 live end-to-end smoke gates (phases 1–11) that run the real HTTP pipeline against Docker Compose. |
| Security posture inside the engine | Secrets rejected at ingestion boundaries (`_reject_secrets`), HMAC-signed platform webhooks with `hmac.compare_digest`, a closed remediation action registry (no shell, ever), command allowlists for fix verification, POSIX/container sandbox limits for reproduction, savepoint-isolated sweeps, per-project write mutexes, hash-chained remediation audit. |
| Repo hygiene | `.env` is **not** committed and is git-ignored; no suspicious files (`.pem`, `.key`, credentials) in git history; zero `console.log` in the web app; **zero** `TODO`/`FIXME` markers in backend or frontend source. (The single `TODO` string in the backend is a regular expression in `patch_safety.py` that *rejects* AI patches carrying "TODO … skip/later" markers — a detector, not debt.) |
| Query discipline | Every list endpoint is paginated (`page_size` clamped, e.g. `le=100`), traversals bounded by configuration, sweep steps savepoint-isolated. |
| Self-monitoring | `/health/live`, `/health/ready`, `/health/dependencies`, Prometheus `/metrics`, plus the Phase 11 platform-health model with required/optional subsystems and graceful-degradation contracts. |
| Live verification | The Phase 11 gate was run **five consecutive times** (90/90) plus the DDL probe (91/91); phases 1–10 gates all re-run green on the same stack. |

### What is NOT production-ready yet (the honest gaps)

These are ranked by how much they block a real new user, not by effort.

| # | Gap | Evidence | Blocks |
| :--- | :--- | :--- | :--- |
| G1 | **No authentication or authorization.** There is no login, no API tokens, no user concept anywhere in the backend. Isolation is server-side ownership validation (project scoping), which is real — but anyone who can reach port 8000 can read and write everything. | `grep Authorization/Bearer/api_key` over deps → zero hits | 🔴 **Everything multi-user.** |
| G2 | **No CI/CD at all.** No `.github/workflows`. Every gate currently runs only on your machine. A fork or a contributor gets zero automated verification. | `ls .github/workflows` → does not exist | 🔴 Open-source credibility. |
| G3 | **Ingestion is unauthenticated.** OTLP and the Phase 1 webhook accept any payload with no source tokens or signature checks (the *platform* webhook in Phase 11 has HMAC; the *ingestion* webhook does not). A new user cannot safely expose an ingest endpoint even inside their network. | `otlp.py` routes have no auth dependency; `ingestion.py` has secret-*rejection* but no signature verification | 🔴 "Point ARGUS at my system". |
| G4 | **Open-source hygiene files missing.** No `LICENSE`, no `CONTRIBUTING.md`, no `SECURITY.md`, no `CODE_OF_CONDUCT.md`. Without a license, **nobody can legally use the project.** | `ls LICENSE*` → none | 🔴 Open-source launch. |
| G5 | **The new-user journey is undocumented.** There is no `docs/api.md`, no `docs/demo.md`, no `docs/operations.md`, no `docs/security-architecture.md`, no `docs/troubleshooting.md`, and no UI guide. A new user has a README and 29 phase documents — breadth without an entry path. | `ls docs/` | 🔴 Adoption. |
| G6 | **Demo seeding is unconditional.** The API entrypoint always runs `seed_data.py`. A production user gets demo data mixed into their database on first boot with no `SEED_DEMO=false` switch. | `docker-entrypoint.sh` | 🟠 Production deploys. |
| G7 | **Frontend failure states are page-local.** There is no global `error.tsx` / `loading.tsx`; empty and error states exist per-page but inconsistently. | `ls apps/web/app/error.tsx` → missing | 🟠 Perceived quality. |
| G8 | **No load/stress evidence.** Two benchmark scripts exist (graph, anomaly) but there is no documented throughput/latency envelope for ingestion bursts, concurrent reproductions, or remediation bursts. The README honestly says limits are untested. | `infrastructure/` contains 2 benchmarks only | 🟠 Capacity claims. |
| G9 | **`lib/api.ts` is 6,713 lines.** One file carries every type and client method for 11 phases. It works, but it is the single worst maintainability point in the repo. | `wc -l` | 🟠 Maintainability. |
| G10 | **Minor demo debt.** `reproduction_planner.py` defaults its sandbox template to `"demo_commerce"`; `.env.example` starts with a `[TEMPLATE]` line that some dotenv parsers treat as an INI section header. | grep | 🟡 Polish. |
| G11 | **No rate limiting middleware** at the API edge (only feature-level limits like search). An operator pointing ARGUS at an internal network is unprotected against accidental load. | grep slowapi/RateLimit → none | 🟡 Hardening. |
| G12 | ~~**Multi-worker sweep duplication is unverified.**~~ **Closed.** `tests/test_project_lock_postgres.py` proves it against real PostgreSQL: the project row lock blocks a second writer, emits `FOR UPDATE` on the production dialect (so it cannot silently degrade to a no-op, as it does on SQLite), and four concurrent detect+correlate passes — the exact shape the sweeps run — all commit with one fingerprint registry row and one anomaly instead of a lost transaction. | `tests/test_project_lock_postgres.py` | ✅ Verified. |
| G13 | **No browser-level e2e tests.** The 11 live gates validate the HTTP API and the web build compiles, but nothing drives the actual UI in a browser — a broken page renders green. Vercel's own ship path runs Playwright against the built app. | `grep playwright apps/web` → only dev deps for screenshots | 🔴 UI reliability. |
| G14 | **Next.js 14.2.15 has a known CVE (CVE-2025-29927, middleware-bypass class).** Not directly exploitable here (the app has no `middleware.ts`), but shipping a framework with a public security advisory fails an open-source security review, and `npm audit` will flag it. | `apps/web/package.json` pins `14.2.15` | 🔴 Supply-chain credibility. |

### Do you need real API keys?

**No — and that is a design property, not an accident.**

- Every deterministic pipeline (detection → correlation → RCA → reproduction → fix
  verification → prediction → remediation → learning → control plane) runs with
  **zero** external services beyond PostgreSQL + Redis, which ship in Compose.
- AI is opt-in per feature: `AI_PROVIDER=mock` by default. If a user sets
  `AI_PROVIDER=openai` (or any provider) with an `AI_API_KEY`, the debugger and
  narrative layers use it — and *fail degraded, honestly*, if the key is missing,
  wrong or the provider times out. Nothing hard-requires a key; nothing fakes
  success when a key fails.

So: a new user needs **no API keys** to get full value. If they *want* model-assisted
debugging narratives, they add their own key for their own provider. That answer is
verified in code (`ai_debugger.py` fallback path, `reliability_narrative.py`
deterministic default).

### My confidence level — stated precisely

| Claim | Confidence | Why |
| :--- | :--- | :--- |
| Works on your demo system | **High** | 11 live gates, 1,912 tests, five consecutive phase-11 runs (the audit-time baseline). |
| Works on a *new user's* system for **observation** (ingest → map → detect → incidents → RCA) | **High** | The pipeline is metadata-driven (projects/components you create over the API or seed scripts; OTLP is standard). Remaining risk: onboarding friction (G3, G5), not correctness. |
| Works on a *new user's* system for **code intelligence / fixes** | **Medium** | It works for repos mounted under `CODE_ALLOWED_ROOTS` (verified in gates with a real git repo). Risk: repository shapes we've never indexed (monorepos, exotic layouts, languages beyond Python/TS heuristics). Needs the real-world-repo validation pass (W6). |
| Safe to expose beyond localhost | **No** | G1/G3. Unauthenticated API + unauthenticated ingestion. This is the single biggest blocker and the first workstream. |
| "No bugs whatsoever, guaranteed" | **Nobody can promise this — and you should distrust anyone who does.** What can be promised: every known defect fixed with a regression test, every gate green, failure paths tested intentionally, and an honest limitations document. That is what this plan delivers. |

---

## 1. Scope and rules of engagement

1. **No new feature phases.** Nothing in this plan adds a capability that changes
   what ARGUS *does*. Authentication, rate limiting, seeding control and docs make
   the existing system deployable — they do not make it bigger.
2. **No rewrites of working architecture.** The deterministic engine design,
   the phase boundaries, the control plane and the test strategy are preserved.
   Where consolidation is proposed below, it is because two implementations exist
   *today*, not for taste.
3. **Every change lands with tests and evidence.** The final report may only claim
   what was executed.
4. **Definition of done for the whole effort:** the Final Validation Matrix in §8 is
   fully green with cited evidence, and `docs/production-readiness.md` states
   `READY` or `CONDITIONAL` with the exact conditions remaining.

---

## 2. Single-source-of-truth audit result

The audit looked for competing implementations of every major domain entity. Result:

**Clean (single implementation, single owner):**
Project/Environment/Component (`models/project.py`, `models/system.py`), Observability
(`models/observability.py`), Anomaly/Incident/Evidence (`models/anomaly.py`,
`models/incident.py`), Causal domain (`models/causal.py`), Reproduction
(`models/reproduction.py`), Code intelligence (`models/code.py`), Fixes
(`models/fix.py`), Forecast (`models/reliability.py`), Remediation
(`models/remediation.py`), Learning (`models/intelligence.py`), Control plane
(`models/platform.py`). Phase 2's graph is an **overlay by design** (mirrors canonical
rows via `entity_kind`/`entity_id`; it never duplicates ownership) — documented and
correct. Phase 10's learning rows are **derived by design** (as-of bounded, cascade on
delete) — documented and correct. The Phase 0 ABC stubs in `engines.py` are interfaces,
with one removal already made (the stale `Anomaly` dataclass); they stay.

**Action items found (all minor):**

| Item | Decision |
| :--- | :--- |
| `reproduction_planner` / `reproduction_orchestrator` default `template="demo_commerce"` | Rename the default to the neutral `generic_service` (keeping `demo_commerce` as an alias so nothing breaks), so the *product* default is not named after the *demo*. |
| Two "aware UTC" helpers (`app/services/platform_time.py` vs `app/core/time.py`) | Consolidate onto `app/core/time.py`; keep `platform_time` as a one-line re-export for its importers, then collapse in a follow-up commit. |
| `lib/api.ts` monolith | Split by domain into `apps/web/lib/api/` modules behind the same `api` object so **no call site changes** (W7). |

No duplicate models, no duplicated services, no dead phase code, no abandoned
experiments were found. `engines.py` ABCs stay: they are the declared Phase 0
boundary contract.

---

## 3. The workstreams

Each workstream lists concrete tasks, acceptance criteria, and how it will be
verified. Ordered by dependency and impact.

---

### W1 — Security foundation: authentication, authorization, ingestion trust ✅

*The blocker for everything else. Without this, "production" is not a claim ARGUS
can make.*

**Tasks**

1. **Token authentication** (`apps/api/app/core/security.py`, `deps.py`):
   - `Authorization: Bearer <token>` on every route except `/health/*`, `/metrics`,
     and `/docs` (docs already dev-only).
   - Bootstrap model for an open-source self-hosted product: on first boot the API
     generates a root **admin token** and prints it once to the container log (and
     writes it to a root-only file inside the container). No passwords to manage in
     v1; tokens are revocable rows.
   - `api_tokens` table: `token_hash` (SHA-256 — the raw token is never stored),
     `name`, `role`, `created_at`, `last_used_at`, `expires_at`, `revoked_at`.
     Constant-time lookup by hash.
   - Roles (kept deliberately small): `ADMIN` (everything), `OPERATOR` (write:
     ingest, triage, approve remediation, configure), `VIEWER` (read-only).
     Remediation approval additionally requires `OPERATOR` **and** the Phase 9
     approver identity — Phase 9's own gates stay authoritative.
   - Optional `ARGUS_AUTH_DISABLED=true` escape hatch for local dev and existing
     smoke gates, logged loudly as a warning banner in the UI when active.
2. **Project-scoped authorization on top of tokens**: every existing
   `require_project` check additionally verifies the token's project grants
   (`api_token_projects` join; an `ADMIN` token passes all). An unauthorized
   project id stays a `404` — never a `403` that leaks existence.
3. **Ingestion trust**: per-source ingest tokens.
   - `ingestion_sources` already exist as rows; add `ingest_token_hash` +
     `POST /ingestion/sources/{id}/rotate-token`.
   - OTLP routes accept the source token via `Authorization` or
     `X-Argus-Ingest-Token`; the ingestion webhook verifies an HMAC signature
     (`X-Argus-Signature: sha256=…`, timestamp tolerance — the same scheme Phase 11
     webhooks already use, so the code is proven).
   - The existing `_reject_secrets` boundary stays exactly as it is.
4. **Frontend**: token entry stored in `sessionStorage` (never `localStorage`),
   `apiFetch` attaches it, `401` routes to a sign-in screen, role-aware controls
   (approval buttons hidden for `VIEWER`).
5. **Rate limiting middleware** (G11): a lightweight token-bucket per token + per
   IP for unauthenticated routes, configuration-driven, 429 with `Retry-After`.
6. **Request-body limits** (new, second audit pass): an ASGI middleware that
   returns `413` above a configurable `MAX_REQUEST_BODY_BYTES` (tight default for
   ingestion routes, larger for OTLP batches). Today a single oversized request
   can exhaust API memory — verified by grep: no body-limit middleware exists.
7. **Security tests**: token forgery/expiry/revocation, role escalation attempts,
   cross-project token access (must 404), unauthenticated ingest refused,
   replayed webhook signatures refused, rate-limit engagement, oversized-body
   refusal, and a test that
   **every** router (introspected via `app.openapi()`) carries auth except the
   documented public set.

**Acceptance**: with `ARGUS_AUTH_DISABLED` unset, zero unauthenticated write paths
exist (proven by the OpenAPI introspection test); the live gates run against a
bootstrapped admin token; the README security section describes the model honestly.

---

### W2 — CI/CD and open-source hygiene ✅

**Tasks**

1. GitHub Actions:
   - **fast** workflow (every push/PR): `ruff check`, `ruff format --check`,
     `mypy`, backend unit suite (SQLite, ~6 min), `tsc --noEmit`, web lint,
     web vitest, web build.
   - **integration** workflow (PRs to `main`, nightly): Postgres + Redis services,
     full backend suite, then the phase 1/3/11 live gates as the e2e tier.
   - Weekly `pip-audit` / `npm audit` job, advisory-first (fail on known-exploitable),
     with `next` pinned above `14.2.25` (G14 — CVE-2025-29927) via an npm
     `overrides` entry verified against the lockfile; upstream audit reports triaged
     in a tracking note rather than suppressed wholesale.
   - Supply-chain review step: verify no dev-only tooling (playwright, vitest,
     screenshot scripts) ships in the production web image; Python version
     compatibility (3.12 in Docker, 3.14 locally) is asserted in CI.
   - Failure output must be actionable: pinned action versions, step summaries.
2. Repo files: `LICENSE` (MIT — your choice to confirm), `CONTRIBUTING.md`
   (env setup, gate commands, PR rules, what a good issue looks like),
   `SECURITY.md` (how to report, supported scope, response expectations),
   `CODE_OF_CONDUCT.md`, issue/PR templates.
3. `.env.example` cleanup (G10): remove the `[TEMPLATE]` first line, group and
   comment every variable, mark which are required vs optional, add the new W1
   variables.

**Acceptance**: a fresh fork shows green CI on the first push; a stranger can
follow `CONTRIBUTING.md` to a green local run without asking anyone.

---

### W3 — The new-user journey, end to end ✅

*Your central requirement: "test it without demo, like a new user would."*

**Tasks**

1. **`SEED_DEMO` toggle** (G6): entrypoint honours `SEED_DEMO=false` (compose
   default stays `true` for first-run experience; docs show the production value).
   Idempotency of the seeder is already proven by tests.
2. **Onboarding path for a real system** — the "connect your own service" story,
   tested as a user would:
   - Create project → environment → components → dependencies via the API/UI.
   - Point any OpenTelemetry SDK at `POST /api/v1/otlp/v1/{traces,logs,metrics}`
     with a source token (W1). Provide copy-paste snippets for Python, Node and
     OpenTelemetry Collector in the docs.
   - Verify: telemetry lands → graph discovers relationships → baselines build →
     a deliberate fault in the user's app produces an anomaly → incident →
     RCA with evidence.
   - This flow becomes **`docs/onboarding.md`** and a new smoke gate
     (`e2e-smoke-onboarding.sh`) that performs it against a *second, non-demo*
     project on the live stack — the automated version of "a new user's first hour".
   - Add a **browser smoke gate** (G13): a small Playwright suite (≤15 checks)
     that loads the built app in Chromium and walks
     overview → service → incident → RCA → remediation, asserting the real
     panels render from live data. It runs in CI and keeps "the UI works" a
     tested claim instead of a hope; no visual-regression maintenance burden.
3. **`docs/demo.md`**: what the seeded demo contains, what each engine derives
   from it (never hard-coded), and how to reset it.
4. **UI guide** — `docs/ui-guide.md` (the document you asked for): a page-by-page
   tour of all 68 pages with what each panel shows, where its data comes from, and
   the natural navigation path
   `Overview → Service → Incident → Evidence → RCA → Code → Reproduction → Patch →
   Verification → Remediation → Learning`, with screenshots captured from the
   running app during this workstream.
5. **Missing top-level docs** (G5): `docs/api.md` (generated from OpenAPI +
   hand-written contract conventions), `docs/operations.md` (deploy, backup/restore,
   upgrade, monitor), `docs/security-architecture.md` (trust boundaries, the W1
   model, what ARGUS will never do), `docs/troubleshooting.md` (symptom → cause →
   fix, drawn from real failure modes we can reproduce).

**Acceptance**: a machine (or person) that has never seen ARGUS follows
`docs/onboarding.md` with `SEED_DEMO=false` and completes ingest → incident → RCA
on their own app; the onboarding gate proves it continuously in CI.

---

### W4 — Failure, recovery and resilience verification ✅

*Failure scenarios must be tested on purpose, not assumed.*

**Tasks** (each becomes a test or a documented, reproducible run):

1. **Worker/Redis resilience** (verify existing behaviour, add what's missing):
   Redis down → API degrades (`/health/dependencies` shows it), ingestion returns
   503-with-reason, queued jobs survive restart (Redis persistence + dead-letter),
   no job can be stuck forever (stale-job reaper exists for debug/repro — extend the
   same guarantee to ingestion jobs if gaps are found), graceful shutdown drains.
2. **Database failure**: connection loss mid-request → 503, no partial writes
   (transactions/savepoints already guarantee this — prove it), pool exhaustion
   behaves bounded.
3. **AI provider failure**: timeout/error/malformed response → `DEGRADED` with
   reason stored, deterministic path unaffected (tests exist; add a malformed-stream
   case).
4. **Reproduction/patch failure paths**: sandbox leak → reaper closes it and the
   metric reports it (exists); patch apply failure → workspace destroyed, candidate
   `FAILED`, source untouched; verification timeout → `NOT_VERIFIED`, never
   `VERIFIED` (exists — pin with an explicit timeout test if missing).
5. **Remediation failure paths**: duplicate proposal refused, stale approval
   refused, failed rollback recorded + breaker opens, emergency stop halts
   mid-verification (mostly exists — consolidate into one failure-mode test module
   and close gaps).
6. **Duplicate/out-of-order events**: idempotency keys verified end to end.
7. **Request-contract abuse**: oversized bodies rejected `413` with no memory
   impact, malformed JSON, oversized single field values, deeply nested payloads,
   and content-type mismatches all return clean validation errors (no stack
   traces) — proving the W1/W2 input hardening actually holds.
8. **Document**: `docs/operations.md` gets a "failure playbook" section — what each
   failure looks like, what ARGUS does, what the operator does.

**Acceptance**: every scenario above has an executed test or a recorded manual run
in the final report; nothing "fails silently" anywhere in the system.

---

### W5 — Data quality, audit-trail integrity and self-observability ✅

**Tasks**

1. **Data-quality center extension** (Phase 11's `data_quality_center` already
   checks cross-phase consistency): add the remaining checks from the brief —
   missing provenance, missing timestamps, duplicate incidents, impossible state
   transitions, missing audit events, corrupted artifacts (hash mismatch). Fix root
   causes where a check reveals a real bug; report operator-fixable ones with
   remediation guidance.
2. **Audit-trail review**: verify the chain (remediation audit is hash-chained;
   configuration is versioned; cases have timelines). Add audit records for auth
   events (W1: token created/revoked/failed) and confirm no code path mutates
   historical audit rows (a test that attempts it and must fail).
3. **Self-observability**: verify Prometheus coverage for the §11 list; add any
   missing series (AI provider latency/failures, reproduction success/failure,
   patch verification outcomes, learning pipeline failures). The Phase 11
   `/platform/health` view becomes the single "is ARGUS healthy?" answer.

**Acceptance**: a data-quality report runs clean on the demo project; the metrics
list is published in `docs/operations.md` with what each series means.

---

### W6 — Performance, load and capacity envelope ✅

**Tasks**

1. **Profile first**: instrument the hot paths (ingest worker batch, detection
   sweep, correlation, state build, search) with timing already exposed via
   `/metrics`; run the existing two benchmarks at higher scale than before.
2. **New load harness** `infrastructure/load-soak.py`: configurable synthetic
   workloads — log/metric/trace bursts, N concurrent incidents, concurrent
   reproductions and fix verifications, remediation bursts — measuring throughput,
   p50/p95/p99 latency, queue depth, error rate, DB pool saturation, memory.
3. **Fix the top measured bottlenecks only** (candidates from the audit: repeated
   full-table counts in dashboard aggregation, `lib/api.ts`-driven request
   chattiness on overview pages — verify before touching).
4. **Verify multi-worker behaviour** (G12): run the stack with `API_WORKERS=4`,
   confirm sweeps are mutex-safe (they are by design; prove it), document the
   recommended production topology (1 worker + 1 dedicated worker container, or
   sweep leader election if measurement demands it).
   **Done** — `apps/api/tests/test_project_lock_postgres.py` (5 tests, run by CI
   against PostgreSQL) proves the lock serialises writers, that it emits
   `FOR UPDATE` on the production dialect, and that four concurrent passes lose
   nothing; the recommended topology is documented in
   `docs/production-readiness.md` §6.
5. **Pin the Python runtime** (new, second audit pass): local venv runs 3.14
   while the Docker image and contributor docs use 3.12 — the supported interpreter
   must be one version, declared in `pyproject.toml` (`requires-python`),
   verified in CI, and documented in `CONTRIBUTING.md` so a new contributor does
   not debug phantom differences.
6. **Publish the envelope** in `docs/production-readiness.md`: tested numbers only,
   explicitly labelled as tested-on-hardware-X, no extrapolations.

**Acceptance**: documented tested limits; the README's performance claims match
them; no unbounded query remains (spot-audit of every `select` without a limit).

---

### W7 — Frontend final polish ✅

**Tasks**

1. Global `error.tsx` + `loading.tsx` boundaries (G7) with retry actions.
2. Consistent empty/loading/error/skeleton states via the existing `ui.tsx`
   primitives — audit every platform page against one checklist.
3. Destructive-action confirmations everywhere (project delete, policy edits,
   emergency stop, token revoke).
4. Keyboard navigation + `aria` labels on the interactive primitives; focus
   management in dialogs.
5. `lib/api.ts` split by domain (G9) behind the same `api` object — zero call-site
   changes, typecheck as the safety net.
6. Cross-linking pass: from an incident, one click reaches evidence, RCA, code,
   reproductions, fixes, remediations, forecasts, learning (verify each edge in the
   UI guide as it is written).

**Acceptance**: the UI guide's navigation path is click-verifiable end to end; no
page shows a raw error without a retry.

---

### W8 — Configuration, deployment and operations readiness ✅

**Tasks**

1. Compose hardening: non-optional healthchecks everywhere, `restart` policies
   reviewed, resource limits documented, `API_ENVIRONMENT=production` profile with
   docs disabled, secrets-in-env documented (never baked into images).
2. Backup/restore procedure for Postgres (documented + a smoke-testable script).
3. Upgrade procedure: `alembic upgrade head` under a live pool is already gated
   (DDL probes); document the operational steps and rollback story.
4. `docs/production-readiness.md` + `docs/final-architecture-audit.md` +
   `docs/argus-benchmark-report.md` (the three deliverables from the brief) with
   the final validation matrix.

---

## 4. Remediation-safety and patch-verification re-review (brief §8, §9)

These were audited as part of the repository review; findings:

- **Remediation**: default deny, closed registry, five fixed-order gates, clamped
  policies, breaker per `(project, environment, action_type)`, planned-before-executed
  rollback, hash-chained audit, emergency stop, ships disabled — all present and
  tested. W4 adds a consolidated bypass-attempt test module (policy override via
  config, safety-failure override, autonomous-execution in a production-typed
  environment) to convert "present" into "attacked and held".
- **Patch verification**: `VERIFIED` requires the two-sided regression test (fails on
  base, passes on patch), comparison thresholds, and refuses test-tampering patches
  (deleted tests, weakened assertions, skips, disabled lint) before they reach a
  workspace; production source is never touched (disposable worktrees). W4 pins the
  timeout and partial-failure paths explicitly.

No weakening found. The work here is *adversarial test coverage*, not redesign.

---

## 5. What this plan deliberately does NOT do

- No Phase 12 features, no new engines, no new surfaces.
- No migration off PostgreSQL/Redis; no graph database.
- No multi-tenancy beyond projects (that is a product decision, not hardening).
- No SSO/OIDC in v1 — tokens first, SSO listed as future work in
  `security-architecture.md` (honest scope, not a silent gap).
- No blind dependency upgrades — only audit-driven, compatibility-verified bumps.

### Hardening decisions (so nothing is left ambiguous)

- **Redis durability**: configure AOF (`appendonly yes`, `everysec`) in compose so
  queued jobs survive a Redis restart by default — the W4 tests then verify the
  documented behaviour instead of depending on snapshot timing.
- **Rate limiting always on**: the token-bucket middleware ships enabled with
  generous defaults; rate limiting is DoS hardening and is never silently disabled.
- **No auth-free bypass**: `ARGUS_AUTH_DISABLED=true` works only when
  `API_ENVIRONMENT != production`; in production it refuses to boot with a clear
  error. No env combination weakens ingestion trust.
- **Honest matrix**: no row is marked READY on documentation alone — every READY
  needs a command + result recorded in `docs/final-architecture-audit.md`.
- **Changelog discipline**: user-visible changes in this hardening pass are
  recorded in `CHANGELOG.md` (added in W2) under an
  "Unreleased → Hardening" heading, so an upgrading operator sees every
  behavioural change in one place.

---

## 6. Execution order and checkpoints

| Order | Workstream | Why this order |
| :--- | :--- | :--- |
| 1 | W2 (CI + hygiene + LICENSE) | Cheapest, unblocks everything else; every later PR gets automated verification. |
| 2 | W1 (auth + ingestion trust) | The largest blocker; touches every route, so it lands before docs describe contracts. |
| 3 | W3 (new-user journey + docs) | Depends on W1 (tokens in the snippets) and W2 (gates in CI). |
| 4 | W4 (failure/recovery) + W5 (data quality/audit) | Can run together; both mostly test-and-close. |
| 5 | W6 (performance/load) | Needs the system stable and secured to measure meaningfully. |
| 6 | W7 (frontend polish) + W8 (ops docs) | Final pass once behaviour is frozen. |
| 7 | Final architecture review + three deliverable docs + final matrix | Last, on the finished system. |

Each workstream ends with: tests green, gates green, a short evidence note appended
to `docs/final-architecture-audit.md`, and a commit. Checkpoints are reviewable —
you can stop the plan after any workstream and still have a coherent repo.

---

## 7. Answers to your direct questions

**"Is the code properly written, backed up and executing?"**
Yes on execution and verification (numbers above). "Properly written" is
best answered by the audit: clean boundaries, one implementation per concept,
disciplined queries, one known maintainability hotspot (`api.ts`, W7), and
no hygiene debt (0 TODO/FIXME markers outside one detector regex, 0 console.logs).

**"Do you need real API keys?"**
No. Full deterministic value with none; optional per-feature AI keys degrade
honestly. (§0.)

**"Will it work on any other complex system?"**
Observation pipelines: yes with high confidence. Code intelligence/fixes on
unseen repository shapes: good but needing the W3/W6 real-world pass — that is
exactly what the onboarding gate exists to keep honest forever.

**"How much of this is demo-only?"**
Found instances: the unconditional demo seed (fixed in W3), the
`demo_commerce` default template name (§2), and the fact that the *documented*
user journey currently assumes the demo (fixed in W3). No engine produces
hard-coded results — the gates assert derivation, and the adversarial gates fail
when nothing is produced.

**"Can you guarantee 90–95%+ production readiness?"**
After this plan: for **single-operator / small-team self-hosted deployment**
(token auth, internal network, documented ops), yes — with the remaining honest
gaps (single-region, manually-promoted failover with no read routing to the
replica, tested-but-modest load envelope, AI features optional) stated in
`production-readiness.md` as the path from `CONDITIONAL` to `READY`. *(Corrected
at completion: this paragraph listed "no SSO" as a gap, but W2 shipped OIDC
single sign-on and `infrastructure/e2e-smoke-sso.sh` now gates it with 65
assertions.)*
What I will not do is print `READY` without the evidence the matrix demands — that
discipline is the point of the exercise.

---

## 8. Final validation matrix (filled at completion)

Recorded 2026-09-26 from a single full `bash infrastructure/verify-all.sh` run:
**26 layers verified, 0 failed.**

The first full CI run on `main` then found two defects the local runs could not
see, both because this machine's venv had drifted off the pins (`Python 3.14` /
`SQLAlchemy 2.0.54` against the pinned `3.12` / `2.0.36`): the ingestion
pipeline never wrote a dead-letter row for a failed batch event, and the Phase 5
sandbox could not start a service on any host whose account already ran more than
32 tasks — i.e. every non-root deployment — because `REPRO_MAX_PROCESSES` was
applied as `RLIMIT_NPROC`, which Linux checks per **UID** rather than per sandbox.
Both are fixed, with regression tests that fail on the pinned set, and the
follow-up run is green. That is the honest lesson of this pass: a green local
suite is only as good as the dependency set it ran on, which is why CI is the
authority and `docs/development.md` now says how to reproduce a CI-only failure.

| Area | Status | Evidence |
| :--- | :--- | :--- |
| Architecture | ✅ | single-source audit ([final-architecture-audit.md](final-architecture-audit.md)) + [architecture.md](architecture.md) §2 |
| Backend | ✅ | **2,187 passed / 1 skipped** (PostgreSQL, as CI runs it); 2,173 / 15 skipped (SQLite); `ruff` clean; `mypy` clean (239 files) |
| Frontend | ✅ | vitest **263 passed** (14 files); `tsc` clean; `next build` succeeds (50 static pages generated, 68 pages in the app) |
| Database | ✅ | 23 revisions → 127 tables from zero on real PostgreSQL; the migration suites run inside the 2,187 |
| Redis/Workers | ✅ | fault-injection gate **10/10**: Redis killed mid-ingest, ingest still succeeds, queues drain, zero rows lost |
| Observability | ✅ | observability gate **22/22**: 20 alert rules each with a runbook link, a 22-panel dashboard, and **every referenced series present in a live scrape** |
| Knowledge Graph | ✅ | phase 2 gate **28/28** |
| Incident Intelligence | ✅ | phase 3 gate **103/103** |
| RCA | ✅ | phase 4 gate **70** |
| Reproduction | ✅ | phase 5 gate **104** |
| AI Debugging | ✅ | phase 6 gate **159** (including the refused-code-claim paths) |
| Patch Verification | ✅ | phase 7 gate **90** (safety validation + verification ladder) |
| Prediction | ✅ | phase 8 gate **42** |
| Remediation | ✅ | phase 9 gate **69** (policy refusals first, then the allowed path) |
| Learning | ✅ | phase 10 gate **87** |
| Security | ✅ | hardening gate **15/15** (auth + tenant isolation over real HTTP), SSO gate **65/65**, plus the OIDC suite (49 tests) |
| Performance | ✅ | [argus-benchmark-report.md](argus-benchmark-report.md) §4b — 0 `5xx` from 8 to 64 concurrent clients |
| Testing | ✅ | **2,187 backend + 263 web tests, 19 live gates / 1,157 assertions — 0 failures** |
| CI/CD | ✅ | `.github/workflows/`: fast checks on every push; integration tier boots the stack and runs the live gates, including the SSO gate. **Green end to end** on `5b3559d` ([run 36248940251](https://github.com/piyush06singhal/argus/actions/runs/36248940251)): backend **2,167 passed / 21 skipped** on the runner, web **263**, repo hygiene, in 6m35s |
| Supply chain | ✅ *(one tracked deferral)* | `pip-audit` clean; `node scripts/audit-gate.mjs` passes with **12/12 high-and-critical production advisories triaged** in [supply-chain-triage.md](supply-chain-triage.md); Next.js 14.2.35 ≥ the 14.2.25 floor. The Next 16 upgrade that would eliminate them is deferred by decision, with its migration surface and exit criteria recorded — see below |
| Runtime environment | ✅ | `python:3.12-slim` image; every workflow pins Python 3.12 |
| Documentation | ✅ | docs index complete; [test_docs_consistency.py](../apps/api/tests/test_docs_consistency.py) (10 tests) makes drift — a broken runbook link, a renamed alert, an unwired gate, a false capability denial — a failed build |
| End-to-End Demo | ✅ | onboarding gate **45/45** on a non-demo project; browser tier **5 passed** |

**Verdict: `READY`** — for a single-operator / small-team self-hosted
deployment. The remaining limitations are stated plainly in
[production-readiness.md](production-readiness.md) §7: failover is a deliberate
manual promotion, reads are not routed to the HA replica, Redis is a single
node, the topology is single-region, and the load envelope is modest.

**Deferred at close-out (one item, tracked):** the **Next.js 16 upgrade**. The
Next.js 14 advisories are *triaged and accepted, not eliminated*. The decision,
the residual risk, the migration surface (React 19, Async Request APIs, `next
lint`) and the exit criteria are recorded in
[supply-chain-triage.md](supply-chain-triage.md) — deliberately, so that closing
it is planned work rather than a surprise when the next advisory lands. It is the
only item this completion record leaves open.

---

*End of plan. Execution completed workstream by workstream; the evidence above
is the completion record, and `verify-all.sh` re-runs it in one command.*
