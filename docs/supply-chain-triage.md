# Supply-Chain Triage

Living record of dependency-audit findings and why any remaining advisory is
(or is not) applicable to ARGUS. Nothing here is silently suppressed: CI fails
on new `pip-audit` findings and on `npm audit --omit=dev` findings at
`high`+ severity; this document explains the residue.

**Last reviewed: 2026-09-26.** (`node scripts/audit-gate.mjs` re-run: Next
14.2.35 passes the floor check; **12 high/critical production advisories, all 12
triaged**; `pip-audit -r apps/api/requirements.txt` reports no known
vulnerabilities.)

## Backend (pip-audit: clean)

`pip-audit -r apps/api/requirements.txt` reports **no known vulnerabilities**.

History of this pass:

| Package | Was | Now | Why |
| --- | --- | --- | --- |
| fastapi | 0.115.6 | 0.141.1 | pulled patched starlette; matches the version the full suite was already verified against |
| starlette | 0.41.3 | 1.6.0 | 8 advisories (PYSEC-2026-161/-248/-249/-1941/-1942/-2280/-2281) fixed |
| pytest | 8.3.4 | 9.1.1 | PYSEC-2026-1845 fixed |
| pytest-asyncio | 0.24.0 | 1.4.0 | required for pytest 9 compatibility |
| python-dotenv | 1.0.1 | 1.2.3 | PYSEC-2026-2270 fixed |
| aiosqlite (dev) | 0.20.0 | 0.21.0 | was pinned older than requirements.txt (0.21.0); reconciled |

## Frontend (npm audit --omit=dev: high/critical Next.js advisories, all triaged)

Next.js 14.2.x receives security backports but npm attributes advisories to the
whole `next 9.3.4 – 16.3.0` range, so they remain *listed* even though the
fixed 14.2.35 build is installed. Upgrading to Next 16 (where the advisories
close in npm's model) is a breaking migration (React 19, Async Request APIs)
and is tracked as future work, **not** silently deferred. Each advisory was
checked against what ARGUS actually uses:

| Advisory class | Applicable to ARGUS? | Evidence |
| --- | --- | --- |
| Image Optimizer DoS / AVIF RCE / disk-cache growth (`next/image`) | **No** — the app never imports `next/image` and ships no `images` config | `grep -r next/image app components lib` → 0 hits |
| Server Actions SSRF/DoS/RCE (`use server`) | **No** — no server actions anywhere | `grep -r "use server"` → 0 hits |
| Middleware bypass / redirect cache-poisoning / i18n bypass | **No** — no `middleware.ts`, no i18n config | `ls middleware.ts` → absent |
| WebSocket-upgrade SSRF | **No** — no WS upgrade endpoints | rewrites proxy plain HTTP only |
| RCE on Windows-hosted servers | **No** — deployment targets are Linux containers | `node:20-alpine` images |
| Unescaped `</style>` XSS (postcss build-time) | **No** — build-time only, postcss not in the runtime image | multi-stage build, `npm ci --omit=dev` |
| Rewrite request-smuggling / RSC cache poisoning / cache confusion | **Mitigated** — the single rewrite (`/api/:path*` → ARGUS API) forwards to a private-network service, and every non-public API route requires a bearer token (auth lands with the W1 work of this hardening pass); the browser never talks to upstreams directly | `next.config.mjs` + route auth dependencies |

**Residual risk acceptance**: the rewrite-proxy advisories are mitigated by
authentication plus network placement (ARGUS is designed to run inside your
network, not exposed directly to the internet — see `SECURITY.md`). This
acceptance is recorded here and re-reviewed on every bump of `next`.

## Deferred: the Next.js 16 upgrade (tracked, not forgotten)

Upgrading to Next 16 is the only fix npm offers for the advisories above, and it
is a breaking migration. It is **deferred on purpose** — decided 2026-09-26 when
the rest of the hardening work closed out — and recorded here in full, so that
closing it is planned work instead of a surprise the week a new advisory lands.

| | |
| --- | --- |
| **Status** | **Deferred / tracked.** Residual risk accepted for the current deployment shape: self-hosted, single host, inside a private network. |
| **Accepted by** | the operator of the deployment — the exposure model this relies on is [SECURITY.md](../SECURITY.md). |
| **What the upgrade needs** | `next` 14 → 16 with `react`/`react-dom` 18 → 19 and their type packages; Node ≥ 20.9; the Async Request APIs (`params`, `searchParams`, `cookies()`, `headers()`) are Promises from 15 onward and the synchronous fallback is gone; `next lint` is replaced by running ESLint directly; the Vitest setup and the Playwright tier must be re-verified against the new runtime. |
| **Why it is not blocking** | every advisory is inapplicable or mitigated for this shape (table above), and ARGUS is documented to run inside your network rather than exposed to the internet. |
| **Exit criteria** | `npx tsc --noEmit`, `npm run lint`, `npx vitest run`, `npm run build` and `npm run test:e2e` all green on Next 16; the `next` floor check in `ci.yml` raised to the new minimum; the `next` and `postcss` entries deleted from `scripts/audit-allowlist.json` once npm stops reporting them. |
| **Rough size** | days — the app uses no `next/image`, no server actions and no `middleware.ts`, which removes the three largest migration surfaces. |

Until those criteria are met, this section is the answer to "is the frontend
exposure addressed?": **triaged and accepted, not eliminated** — re-reviewed on
every bump of `next`, and on any new high/critical advisory for it.

## CI enforcement

- `ci.yml` → fails if `next` < 14.2.25 (CVE-2025-29927) or if a `.env` is committed.
- `supply-chain-audit.yml` → weekly `pip-audit --strict` and
  `npm audit --omit=dev --audit-level=high`.
- Any *new* finding must be either patched (pin bump verified against the full
  test suite) or triaged in this file with applicability evidence.
