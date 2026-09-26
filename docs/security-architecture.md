# Security architecture

ARGUS reads production telemetry, reads source code, runs sandboxes, generates
patches, and — optionally — acts on live systems. That combination demands an
explicit trust model rather than a list of good intentions. This document states
the model, where each guarantee is enforced in code, and what is deliberately
**not** provided.

> Threat model in one sentence: **ARGUS assumes the network in front of it may be
> hostile, the telemetry it ingests may be malformed or forged, the code it reads
> may contain secrets, and any credential it holds is worth stealing — so each
> boundary authenticates, authorizes, bounds and records independently.**

---

## 1. Trust boundaries

```
        untrusted                         authenticated                 policy-bound
┌──────────────────────┐        ┌───────────────────────────┐      ┌───────────────────┐
│ OTel collectors      │        │ Console / operator        │      │ Live systems      │
│ Webhooks             │        │ CI pipelines              │      │ (via adapters)    │
│ Anything on the LAN  │        │ External tooling          │      │                   │
└──────────┬───────────┘        └────────────┬──────────────┘      └─────────▲─────────┘
           │ ingest token / HMAC             │ API token                    │ bounded action
           ▼                                 ▼                              │
     ┌──────────────────────────────────────────────────────────────────────────────┐
     │ FastAPI edge: body limit → rate limit → authenticate → authorize → route    │
     └──────────────────────────────────────────────────────────────────────────────┘
           │                                 │                              │
           ▼                                 ▼                              │
   Tenant-scoped storage            Policy + safety engine  ────────────────┘
   (project grant enforced)         (default deny, evidence required)
```

| Boundary | Credential | Enforced in |
| --- | --- | --- |
| API surface | Bearer API token, role + project grants | `app/core/edge.py` (middleware), `app/api/v1/deps.py` (project choke point) |
| Telemetry | Per-source ingest token, or API token with grant | `app/services/ingest_trust.py`, `enforce_ingest_scope` |
| Webhooks | `X-Argus-Signature` HMAC over raw body + timestamp | `verify_webhook_signature` |
| Repository reads | Path allowlist (`CODE_ALLOWED_ROOTS`), provider validation | `app/services/repository_provider.py` |
| Sandboxes | Declared isolation backend, no silent fallback, resource limits | `app/services/reproduction_sandbox.py` |
| Code changes | Patch parser → safety validator → sandbox verification | `patch_generator`, `patch_safety`, `fix_service` |
| Live action | Action registry → policy → safety → approval → execution | `remediation_*` services |

---

## 2. Authentication and authorization

**Single funnel, default deny.** `AuthMiddleware` authenticates every request
before routing. Only five paths are public (`/`, three health probes, `/metrics`).
A test enumerates the OpenAPI document and fails if any other path is reachable
without a credential — so a route added in a future phase cannot silently ship
open, which is the failure mode that actually happens in practice.

**Roles are enumerable and minimal.** `VIEWER` (read), `OPERATOR` (read + write),
`ADMIN` (tokens, policy, destructive operations). Write floors are applied by
HTTP method centrally, so `POST`/`PUT`/`PATCH`/`DELETE` cannot accidentally be
read-only. `admin_required` guards the small set of genuinely privileged routes.

**Project scoping is a choke point, not a convention.** Every project-scoped
route resolves through the same dependency, which refuses (`403`) when the
caller's grant does not cover the project. List endpoints filter to the grant
rather than returning everything: a scoped token cannot enumerate tenants it has
no business knowing about, and a `404`/`403` on a foreign id never confirms
existence.

**Tokens are hash-only.** 256 bits of entropy, SHA-256 at rest (a random secret
has nothing to brute-force, and lookup must stay indexable). The raw value is
returned exactly once, at creation. Revocation and expiry are checked on every
request, not cached.

**Ingest credentials are a separate space.** Prefixes are disjoint
(`argus_` vs `argus_ing_`), and each space is refused outside its own surface: an
API token cannot be used as an ingest token and vice versa. An ingest token is
bound to one source, therefore one project — the OTLP body's `project_id` must
match it, so a compromised collector cannot write into another tenant.

**Webhooks are signed and replay-resistant.** When a webhook secret is
configured, the delivery must carry an HMAC over `"{timestamp}.{raw_body}"`
within a tolerance window; the raw body is read before parsing, so a signature
covers exactly the bytes that are interpreted. Unsigned deliveries are refused —
there is no "trusted if unverifiable" fallback.

---

## 3. Bounding hostile or broken input

| Control | Setting | Behaviour |
| --- | --- | --- |
| Request body size | `MAX_REQUEST_BODY_BYTES` | `413` before the body is read into memory |
| Rate limit | `RATE_LIMIT_PER_MINUTE` / `_BURST` | `429`, token bucket keyed by credential |
| Field limits | `MAX_LOG_MESSAGE_LENGTH`, `MAX_METADATA_LENGTH`, `MAX_METRIC_LABELS`, … | `422`, never silent truncation |
| Nested collections | per-endpoint caps (`_MAX_ITEMS`, `_MAX_HISTORY`) | bounded responses; an unbounded dump is not an API |
| Traversal depth | `CAUSAL_MAX_DEPENDENCY_HOPS`, correlation windows, sweep batch caps | bounded work per request and per sweep |

Secrets inside telemetry are **redacted at the ingestion boundary**
(`app/services/redaction.py`, `source_redaction.py`): known secret keys are
stripped from source configuration, and source configuration containing secrets
is rejected outright with a clear error rather than stored.

---

## 4. What ARGUS is allowed to *do*

Read-only analysis (ingest → detect → correlate → analyze) is the default and
requires no special authorization beyond a token.

Anything that changes state is guarded by independent gates, in this order:

1. **Action registry** — only enumerated action types exist. There is no
   command-string action type, no shell, and no path from a request body to an
   implementation: the adapter is selected from the registry's own declaration.
2. **Evidence requirement** — an action that references no stored evidence is
   refused. ARGUS does not act on a hunch.
3. **Safety assessment** — targets must exist, belong to the same project, be in
   an actable state, and pass risk checks. A failed assessment cannot be
   overridden by an approval.
4. **Policy** — `OBSERVE_ONLY` authorizes nothing; `DRY_RUN`/`SHADOW` validate
   without touching anything; `HUMAN_APPROVAL` and `AUTONOMOUS` are the live
   regimes, and "autonomous" means *a written policy permitted this*, never *an
   AI decided*.
5. **Approval** — when the policy requires human approval, the action waits;
   approvals are recorded with the approving identity.
6. **Execution in bounded scope** — e.g. ARGUS-native controls (pause ingestion,
   disable a feature) applied through the control plane the platform itself
   reads.
7. **Rollback + verification** — every native action is reversible; rollback is
   itself an audited action, and verification records whether the effect landed.

A **kill switch** exists at both the database level (`EMERGENCY_STOP`) and the
process level (`REMEDIATION_KILL_SWITCH`), and the process one denies
independently of the database — a control that lives only in the database is not
a kill switch.

**Patches never touch your tree.** Generated changes are parsed, validated
against the hypothesis's own file allowlist (with refusals for traversal,
sensitive files, dependency manifests, CI configuration and introduced secrets),
then applied and tested inside an isolated workspace. ARGUS does not commit,
push, deploy, or modify production systems — those are human actions by design.

**Sandboxes fail loudly.** If the configured isolation backend is unavailable,
provisioning fails rather than silently degrading to a weaker mode: an operator's
isolation choice must never be undermined by a fallback.

---

## 5. Auditability

* **Remediation audit events form a hash chain** per action (`prev_hash` →
  `entry_hash`). Removing or editing a row breaks the chain, which is what makes
  the log evidence rather than a log.
* **Authentication events are recorded** (created / used / failed / revoked) with
  the acting token, so a stolen credential's use is visible and attributable.
  When sign-in is federated (`OIDC_ENABLED`), the session is linked to a
  provisioned `external_identities` row (provider + subject) and the audit reason
  carries the person, so an SSO session is attributable to a human and
  disabling that identity revokes every session it holds.
* **Platform webhooks** are HMAC-signed on delivery (signature is a *field*,
  verifiable by the receiver, not an implicit trust).

---

## 6. Deployment assumptions

* **TLS terminates in front of ARGUS.** The API speaks HTTP; exposing it to a
  network without a reverse proxy that terminates TLS means bearer tokens travel
  in clear text. This is a deployment requirement, not an optional one.
* **`API_ENVIRONMENT=production`** turns on production semantics: auth bypass is
  *refused* (the process will not start), and the demo dataset is not seeded
  unless explicitly requested.
* **Postgres and Redis are trusted infrastructure.** They are not exposed by the
  compose file's defaults beyond the host; back them (see
  [operations.md](operations.md)) and treat their credentials as secrets.
* **The console's token lives in the browser you use.** It is held by the
  browser (not written to server logs), and the API is called with it directly.
  Treat the machine as you would any machine holding an operator credential.
* **Federated sign-in is optional and fail-closed.** With `OIDC_ENABLED` the
  provider's JWKS is the trust anchor (`https` is required in production), the
  ID token's algorithm must be asymmetric, and role/grant decisions come from an
  explicit claim allowlist — a claim value nobody listed gets the configured
  floor, never `ADMIN`. `infrastructure/e2e-smoke-sso.sh` drives the whole flow
  against a stub provider and asserts it.
* **A response means the work is committed.** The request's database session is
  committed just before the response is sent
  (`CommitBeforeResponseMiddleware`), so a caller can always observe its own
  write on the next request. This is a security property, not only a
  convenience: "disable this identity" or "revoke this token" must take effect
  for the next request rather than a few milliseconds later, and a deletion an
  operator is told succeeded must not still be readable. It is also what makes
  an automated verifier's assertions meaningful — see
  `tests/test_hardening_commit_order.py`.
* **Ship only what you need**: run with a `VIEWER`/`OPERATOR` token for the
  console and keep `ADMIN` for administration.

---

## 7. What is deliberately not provided

Honesty about scope is part of security. ARGUS does **not** currently ship:

* Local password accounts — there is no ARGUS-managed user database with
  passwords. Credentials are tokens (role + project grants), or a federated
  session when `OIDC_ENABLED` is set.
* Per-user attribution for **locally issued** tokens — an audit entry names the
  token, not the human who created it. A federated session is the exception
  (see §5).
* Network-level isolation between tenants inside one database — isolation is
  enforced at the API and query layer (every query is project-scoped), so a
  database-level breach is out of scope by construction.
* Secrets management integration (Vault/KMS) — configuration comes from the
  environment; supply it from your secret store.
* An outbound sandbox runtime — reproduction runs in the same container
  namespace model you configure; see
  [operations.md](operations.md) for isolation choices.

---

## 8. Reporting a vulnerability

See [SECURITY.md](../SECURITY.md) at the repository root for the private
disclosure process. Please do not open a public issue for a suspected
vulnerability.
