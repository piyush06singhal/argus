# API reference

ARGUS exposes one HTTP API. The web console is a client of it, so **anything the
UI does, your tooling can do** — there is no privileged back door.

* Base URL: `http://<host>:8000`
* Versioned prefix: `/api/v1`
* Interactive docs (development only): `http://<host>:8000/docs`
* OpenAPI document: `http://<host>:8000/openapi.json`

The document is generated from the running code, so it is always the current
truth. This page explains the conventions it does not express: credentials,
scoping, limits and error shapes.

---

## 1. Authentication

Every `/api/v1` route requires a **bearer token**, except the five public paths:

```
/                      /health/live      /health/ready
/health/dependencies   /metrics
```

```bash
curl -sS http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer argus_xxxxxxxx…"
```

### Token lifecycle

Tokens are random 256-bit secrets (`argus_` + 32 url-safe characters). Only a
**SHA-256 hash** is stored, so a token is displayed exactly once — at creation.
Losing it means minting another, never recovering it.

| Where | What it does |
| --- | --- |
| `POST /api/v1/auth/tokens` | Mint a token (ADMIN only). Returns the secret once. |
| `GET /api/v1/auth/tokens` | List token *metadata* (never secrets) with pagination |
| `DELETE /api/v1/auth/tokens/{id}` | Revoke a token immediately |
| `GET /api/v1/auth/whoami` | The caller's role, name, and effective project scope |
| CLI `python -m app.cli …` | Bootstrap/rotate when you no longer hold a token |

`GET /api/v1/auth/whoami` is the first thing to run when a request is refused:

```bash
curl -sS http://localhost:8000/api/v1/auth/whoami -H "Authorization: Bearer $TOKEN" | jq .
# { "token_id": "…", "name": "ci", "role": "OPERATOR",
#   "project_ids": ["…"], "unscoped": false, "capabilities": {...} }
```

### Roles

| Role | Read | Write (create/update/ingest) | Admin (tokens, policy, destructive) |
| --- | --- | --- | --- |
| `VIEWER` | ✅ | ❌ | ❌ |
| `OPERATOR` | ✅ | ✅ | ❌ |
| `ADMIN` | ✅ | ✅ | ✅ |

### Project scoping

A token may be **scoped to specific projects**. A scoped token:

* can read and write only those projects;
* is refused (`403`) on any other project, including one it can see in a list —
  list endpoints filter to the grant instead of leaking the rest;
* cannot create projects outside its grant.

Mint a scoped token for CI, one per environment, rather than sharing a root
token:

```bash
docker compose exec api python -m app.cli create-token \
  --name ci-payments --role OPERATOR --project <project_uuid>
```

### Where enforcement happens

Authentication is a single middleware funnel, not a decorator each route must
remember. A route added tomorrow is authenticated by default; a test enumerates
the OpenAPI document and fails if any non-public path is reachable without a
token. This is deliberate: the common way auth breaks is a new route that forgot
to opt in.

---

## 2. Ingestion credentials

Telemetry is a **separate credential space** from API tokens. An ingest token
(`argus_ing_…`) can only write telemetry, only for one registered source, so an
OTel collector in a less-trusted network never holds an admin secret.

Three ways to authenticate ingestion, in increasing strictness:

| Method | Header | Use |
| --- | --- | --- |
| API token | `Authorization: Bearer argus_…` | Native JSON ingest from trusted tooling |
| Ingest token | `Authorization: Bearer argus_ing_…` or `X-Argus-Ingest-Token: argus_ing_…` | OTLP from collectors |
| HMAC webhook | `X-Argus-Signature` + `X-Argus-Timestamp` | Third-party webhooks; mandatory when a webhook secret is configured |

### Registering a source and minting its token

```bash
curl -sS -X POST http://localhost:8000/api/v1/ingestion/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"project_id":"<uuid>","name":"otel-collector","source_type":"OTEL"}' | jq .

curl -sS -X POST http://localhost:8000/api/v1/ingestion/sources/<source_id>/rotate-token \
  -H "Authorization: Bearer $TOKEN" | jq -r .ingest_token     # shown once

curl -sS -X DELETE http://localhost:8000/api/v1/ingestion/sources/<source_id>/ingest-token \
  -H "Authorization: Bearer $TOKEN"                          # closes it now
```

Rotation is additive and auditable: the new secret is returned once, the old one
stops working immediately, and each event is written to the auth audit trail
with the acting token.

### OTLP

```
POST /api/v1/otlp/v1/traces
POST /api/v1/otlp/v1/logs
POST /api/v1/otlp/v1/metrics
```

Standard OTLP/JSON bodies. `project_id` (and optional `environment_id`) identify
the tenant — an ARGUS extension to the OTLP envelope; both `resourceSpans`
(protojson camelCase, what collectors emit) and `resource_spans` are accepted.
The credential's scope must cover `project_id`, so a body cannot write into
another tenant's project.

Responses are the OTLP ingest summary:

```json
{ "accepted": 12, "duplicates": 0, "failed": 0 }
```

`duplicates` counts events already stored (content-addressed dedupe) — replays
are safe, and a retrying collector does not inflate your data.

### Native JSON ingest

```
POST /api/v1/observability/{metrics,logs,traces,events}
POST /api/v1/observability/traces/spans
```

Zero-dependency alternative when OTLP is not available. Payload limits are
enforced and rejected loudly, never truncated silently: message length, label
counts, label key/value lengths, and total body size.

### Webhooks

```
POST /api/v1/ingestion/webhook              # generic, source-type agnostic
POST /api/v1/ingestion/webhooks/{source_id} # source-bound; signature required
```

When `PLATFORM_WEBHOOK_SECRET` is configured, deliveries must carry:

```
X-Argus-Timestamp: <unix seconds>
X-Argus-Signature: sha256=<hex hmac of "{timestamp}.{raw_body}">
```

Signatures outside the tolerance window are rejected, which makes replay of a
captured delivery useless. An unsigned or mis-signed request is refused with
`401` — the platform never falls back to trusting the body.

---

## 3. Conventions

### Errors

Errors share one shape, so a client can branch on `error_code` and still show
`detail` to a human:

```json
{ "detail": "Rate limit exceeded; slow down", "error_code": "RATE_LIMITED" }
```

| Status | Meaning | Notable `error_code` |
| --- | --- | --- |
| `400` | Malformed request | — |
| `401` | Missing/invalid/expired/revoked credential | `WWW-Authenticate: Bearer` |
| `403` | Authenticated but not allowed (role or project scope) | — |
| `404` | Not found **or** not visible to this credential | — |
| `409` | Conflicts with current state (illegal transition, duplicate) | — |
| `413` | Body above `MAX_REQUEST_BODY_BYTES` | `REQUEST_TOO_LARGE` |
| `422` | Schema validation failed (field-level detail) | — |
| `429` | Rate limit exceeded | `RATE_LIMITED` |
| `5xx` | Unexpected; `detail` stays generic, the log has the trace | `INTERNAL_ERROR` |

Unhandled exceptions never leak a stack trace to the client.

### Pagination

List endpoints return the same envelope:

```json
{ "items": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 0 }
```

`page` (≥1) and `page_size` (bounded) are query parameters. Nested collections in
detail responses are bounded too — an unbounded telemetry dump is not an API.

### Limits

| Limit | Setting | Response |
| --- | --- | --- |
| Body size | `MAX_REQUEST_BODY_BYTES` (10 MiB) | `413`, checked before reading the body |
| Requests/min | `RATE_LIMIT_PER_MINUTE` (600), `RATE_LIMIT_BURST` (120) | `429` |
| Payload field sizes | `MAX_LOG_MESSAGE_LENGTH`, `MAX_METRIC_LABELS`, … | `422` |

The rate limiter is an in-memory token bucket keyed by token (falling back to
client IP). With *N* API workers the effective ceiling is *N* × the configured
values; see [operations.md](operations.md) for sizing.

---

## 4. Resource map

`GET /openapi.json` is authoritative. This is the shape of the surface:

| Prefix | What lives there |
| --- | --- |
| `/auth` | Token lifecycle, `whoami` |
| `/projects` | Projects, environments, components, dependency edges, deployments, per-project graph views |
| `/observability` | Logs, metrics, traces, spans, events (query + ingest) |
| `/ingestion` | Sources, source health, queue, dead-letter, bulk ingest, webhooks, retention, trace validation |
| `/otlp/v1/*` | OTLP traces/logs/metrics |
| `/anomaly-rules`, `/anomaly-suppressions`, `/anomalies` | Detection configuration and results |
| `/incidents` | Incident lifecycle, evidence, timeline, correlation |
| `/api/v1/incidents/{id}/causal-analysis` | Root-cause hypotheses with evidence and confidence |
| `/reproductions`, `/debug-sessions`, `/patches`, `/fixes` | Reproduction experiments, code investigation, generated patches |
| `/reliability` | Forecasts, models, accuracy, backtests, drift |
| `/remediation` | Action registry, proposals, approvals, execution, policy, rollback |
| `/intelligence` | Learned knowledge, patterns, experiences, recommendations, learning runs |
| `/platform` | Unified reliability state, cases, SLOs, data quality, governance, reports, search |
| `/graph`, `/snapshots` | Knowledge-graph queries and versioned snapshots |

Guarded, policy-bound resources (remediation, patches) refuse to act without:
a registered action type, stored evidence, a passing safety assessment, a
policy that permits it, and an approval when the policy demands one. There is no
API call that skips those gates — including for an ADMIN token.

---

## 5. Worked example: incident to explanation

```bash
export API=http://localhost:8000/api/v1
export H="Authorization: Bearer $TOKEN"

# 1. What is open right now?
curl -sS "$API/incidents?page_size=20" -H "$H" | jq '.items[] | {id,title,severity,status}'

# 2. What happened, in order, and what is the stored evidence?
curl -sS "$API/incidents/<id>" -H "$H" | jq '{title,severity,status,detected_at}'
curl -sS "$API/incidents/<id>/timeline" -H "$H" | jq '.items[] | {at,event_type,description}'
curl -sS "$API/incidents/<id>/evidence" -H "$H" | jq '.items[] | {evidence_type,summary}'

# 3. Ask why, then read the ranked hypotheses with their uncertainty
curl -sS -X POST "$API/incidents/<id>/causal-analysis/analyze" -H "$H" | jq .
curl -sS "$API/incidents/<id>/causal-analysis" -H "$H" | jq '{
  confidence, limitations,
  candidates: [.candidates[] | {candidate_type, confidence, summary}]
}'
curl -sS "$API/incidents/<id>/causal-graph"     -H "$H" | jq .   # nodes + directed edges
curl -sS "$API/incidents/<id>/causal-chain"     -H "$H" | jq .   # the ordered chain
curl -sS "$API/incidents/<id>/root-causes"      -H "$H" | jq .   # candidates alone

# 4. Can the hypothesis be reproduced in a sandbox?
curl -sS -X POST "$API/incidents/<id>/reproductions" -H "$H" -d '{"repetitions":1}' | jq .
curl -sS -X POST "$API/reproductions/<rid>/start"      -H "$H" | jq .
curl -sS "$API/reproductions/<rid>/status"             -H "$H" | jq .
curl -sS "$API/reproductions/<rid>/comparison"         -H "$H" | jq .
curl -sS "$API/reproductions/<rid>/validation"         -H "$H" | jq .   # hypothesis verdict
```

Every response carries its own epistemic status: confidence buckets, the
evidence counts behind them, and an explicit `limitations` block. ARGUS never
returns a bare probability or a single confident "root cause" — if the evidence
is thin, the response says so.
