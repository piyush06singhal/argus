# Troubleshooting

Symptom → cause → fix. Commands assume the compose stack; on a different
deployment, substitute your own service names.

**First three commands to run for anything:**

```bash
docker compose ps                          # is everything healthy?
docker compose logs --tail=100 api         # startup errors live here
curl -sS localhost:8000/health/dependencies | jq .   # postgres + redis reachable?
```

---

## Startup

| Symptom | Cause | Fix |
| --- | --- | --- |
| `api` restarts in a loop | Migration failed, DB unreachable, or bad setting | `docker compose logs api`. A config error prints its reason; `AUTH_DISABLED=true` with `API_ENVIRONMENT=production` is refused by design. |
| `api` exits with `AUTH_DISABLED=true is not allowed…` | You enabled the dev bypass in production | Remove `AUTH_DISABLED` (or set it to `false`) and mint tokens instead. |
| `seeding demo data (idempotent)` then immediate exit | Postgres was not ready | Compose health-gates it; if you run the API outside compose, wait for `pg_isready` first. |
| `unhealthy` API but logs are clean | Health probe cannot reach the API | Check the port mapping and `API_HOST`/`API_PORT`. |
| Web loads, API unreachable | Wrong `API_PROXY_TARGET` / `NEXT_PUBLIC_API_URL` | Both must resolve *from inside the container* (`http://api:8000`), not `localhost`. |
| CORS errors in the browser | `CORS_ORIGINS` does not include your console's origin | Set it explicitly, e.g. `CORS_ORIGINS='["https://argus.example.com"]'`. |
| `web` build fails with a missing env var | Build-time arg not passed | `NEXT_PUBLIC_*` values are inlined at build time; rebuild the web image. |

---

## Authentication and authorization

| Symptom | Cause | Fix |
| --- | --- | --- |
| Console shows a red "not connected" badge | No token stored, or the stored token is invalid | Open `/connect` and paste a valid token; `curl localhost:8000/api/v1/auth/whoami -H "Authorization: Bearer $TOKEN"` confirms it from the shell. |
| `401` with `WWW-Authenticate: Bearer` | Missing, malformed, expired, or revoked token | Re-mint one (`docker compose exec api python -m app.cli bootstrap-token`). |
| `403` "requires the ADMIN role" | `VIEWER`/`OPERATOR` on an admin route (token management, policy) | Use an ADMIN token for administration; keep the day-to-day console on a narrower one. |
| `403` on one project but the list shows it | Token is project-scoped and this project is not in its grant | `whoami` shows the effective scope. Re-mint with the project granted, or use an unscoped token. |
| `GET /api/v1/projects` returns fewer items than exist | Working as intended: a scoped token only sees its grant | Use an ADMIN/unscoped token to see everything. |
| Lost the bootstrap token | It is hash-only; it cannot be recovered | `docker compose exec api python -m app.cli bootstrap-token` (prints a new one once). |
| A token works in dev, fails in production | Production refuses the auth bypass | Expected: `AUTH_DISABLED` is dev-only. Issue real tokens. |
| A leaked token must be stopped | — | `DELETE /api/v1/auth/tokens/{id}` (immediate), then review the auth audit trail for its use. |

---

## Ingestion

| Symptom | Cause | Fix |
| --- | --- | --- |
| `404` on `/api/v1/otlp/v1/*` | Unknown source, or wrong URL | Use `/api/v1/otlp/v1/traces` (the `v1` is part of ARGUS's path *and* OTLP's), and register the source first. |
| `401` on OTLP | Ingest token missing/rotated, or an API token used where an ingest token is required for that source | Check the header: `Authorization: Bearer argus_ing_…` or `X-Argus-Ingest-Token`. **Ingest tokens cannot be used outside ingestion, and API tokens cannot write as a specific source.** |
| `403` (not 401) on OTLP | Credential valid, but its scope does not cover the body's `project_id` | The credential decides the tenant; align `project_id` with the source's project. |
| `401` on webhooks after upgrading | Signature now required | Send `X-Argus-Timestamp` + `X-Argus-Signature: sha256=…` over `"{timestamp}.{raw_body}"`. |
| `413 REQUEST_TOO_LARGE` | Body above `MAX_REQUEST_BODY_BYTES` | Batch/split the payload, or raise the limit deliberately. |
| `422` on ingest | Payload limits exceeded (message length, label counts, key/value lengths) | The error names the offending field; fix the producer rather than raising limits blindly. |
| `429 RATE_LIMITED` | Limiter (per worker!) | Back off with jitter. See [operations.md](operations.md) §6 for sizing. |
| `accepted: 0, duplicates: N` | Replay of data already stored — content-addressed dedupe | Expected and safe: retries do not inflate your data. |
| Nothing arrives in the UI | Worker disabled, or queue unhealthy | Check `INGESTION_WORKER_ENABLED`, `GET /api/v1/ingestion/stats`, `GET /api/v1/ingestion/dead-letter`. |
| Events rejected silently? | They are not: rejected events land in the dead-letter list with a reason | `GET /api/v1/ingestion/dead-letter` |

---

## Detection, incidents and analysis

| Symptom | Cause | Fix |
| --- | --- | --- |
| No anomalies | Fewer samples than the rule's `min_samples` in its window, or no enabled rule matches the metric | Send more samples; check `GET /api/v1/anomaly-rules` is `enabled` and the `metric_name` matches exactly. |
| Anomaly fires constantly | Threshold or window is wrong for this service | Use `Z_SCORE`/`BASELINE_DEVIATION` with a rolling baseline, raise `min_samples`, or suppress during a maintenance window (`/api/v1/maintenance-windows`). |
| No incident although anomalies exist | Correlation window too narrow, or severity below the incident floor | Widen `CORRELATION_WINDOW_SECONDS` or lower the floor consciously. |
| Causal analysis returns `UNKNOWN`/low confidence | Thin or contradictory evidence — this is the honest answer | Verify the incident has evidence and traces (`GET /api/v1/incidents/{id}/evidence`); ingest traces, not just metrics. |
| "The deployment caused it" is not claimed | By design: a change near onset is temporal context, not proof | Read the candidates and their `limitations`; reproduction is how a hypothesis is tested. |
| Timeline looks shifted | Clock skew between producers | Send NTP-synchronised timestamps; ARGUS stores UTC and orders events on recorded time. |

---

## Reproduction

| Symptom | Cause | Fix |
| --- | --- | --- |
| Experiment stays `PENDING`/`QUEUED` | Worker/sweep disabled or busy | Check `REPRO_SWEEP_ENABLED`, `GET /api/v1/reproductions/metrics`. |
| `TIMED_OUT` | Sandbox exceeded its deadline | Inspect `GET /api/v1/reproductions/{id}/status` and logs; retry with a longer timeout if legitimate. |
| `SandboxError: Unknown sandbox template` | Template name unknown | Shipped template is `http_service_chain` (`demo_commerce` remains a working alias). List `apps/api/reproduction/templates/`. |
| Sandbox provisioning refuses | Isolation backend unavailable | Intentional: no silent fallback. Configure the isolation mode you intend. |
| Leaked sandboxes | Reaper disabled | `REPRO_SWEEP_ENABLED=true`; artifacts survive teardown by design (`REPRO_ARTIFACT_ROOT`). |
| Disk filling with artifacts | Artifacts are retained per `RETENTION_REPRODUCTIONS` | Prune the artifact root; artifacts are derived and re-creatable. |

---

## Code intelligence, debugger, fixes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `403` registering a repository | Path outside `CODE_ALLOWED_ROOTS` | Allowed roots are an explicit allowlist (default `/repos`). Mount your repo read-only and register a path inside it. |
| Repository registered but no symbols | Not indexed yet | Trigger an index (UI: **Index repository**; API: the code-intelligence endpoints). Check `CODE_SWEEP_ENABLED`. |
| `no readable repository` on a debug session | The deployment's revision has no snapshot | Index the revision first, or point the session at a commit that was indexed. |
| Traces map to no code | Missing service-name/route mapping to a component | Ensure `service.name` in telemetry matches the component registry (aliases are matched, but a name truly absent cannot map). |
| A generated patch is refused | Safety validator: out of scope, sensitive file, dependency/CI change, or an introduced secret | The refusal lists the reason. Scope comes from the hypothesis's file allowlist — narrow the hypothesis, or fix the real cause. |
| Verification inconclusive | The failure was not reproduced on baseline | Inconclusive is a real outcome and is reported as such; check the reproduction verdict first. |

---

## Reliability, remediation, learning

| Symptom | Cause | Fix |
| --- | --- | --- |
| Forecasts are `UNKNOWN`/low confidence | Insufficient history or stale features | Ensure telemetry is flowing; `UNKNOWN` means "not enough evidence", never "healthy". |
| Drift flagged but nothing happened | By design: drift flags for review, never retrains or activates automatically | A human decides; review in the Reliability workspace. |
| Remediation refuses every action | Default deny, or `OBSERVE_ONLY` | Check the policy (`/api/v1/remediation/policy`) and the decision record on the action — it states which gate refused. |
| Action stuck `PENDING_APPROVAL` | Policy requires a human | Approve in the UI or API. A failed safety assessment cannot be approved away. |
| Rollback unavailable | Adapter cannot undo this effect | Only reversible actions are offered as automatable; the adapter says so rather than pretending. |
| No learned patterns | Not enough completed incidents | The miner needs samples; a pattern from two examples would be superstition. |

---

## Performance

| Symptom | Cause | Fix |
| --- | --- | --- |
| Slow list endpoints | Very large `page_size`, or an unbounded filter | Use pagination; nested collections are capped by design. |
| API latency spikes with ingestion bursts | Worker competing with request handling | Split roles (HTTP vs worker) — [operations.md](operations.md) §5. |
| Postgres slow | Working set larger than RAM, or retention never run | Run a retention sweep; check `pg_stat_activity`; keep the data directory on fast local storage. |
| `429`s under legitimate load | Limiter sized per worker | Raise `RATE_LIMIT_PER_MINUTE` deliberately, or enforce a global limit at your proxy. |
| Everything is slow after a long run | Unbounded growth somewhere | `GET /api/v1/platform/data-quality` and `/api/v1/platform/metrics` surface staleness and volume; review retention. |

---

## When you need to escalate

Collect this before opening an issue — it turns a guess into a diagnosis:

```bash
docker compose ps
docker compose logs --tail=300 api > api.log
docker compose logs --tail=100 postgres > postgres.log
curl -sS localhost:8000/health/dependencies | jq . > deps.json
curl -sS localhost:8000/api/v1/auth/whoami -H "Authorization: Bearer $TOKEN" | jq . > whoami.json
git rev-parse HEAD            # the exact revision
```

Redact tokens before posting. See [CONTRIBUTING.md](../CONTRIBUTING.md) for how
to report a bug, and [SECURITY.md](../SECURITY.md) for vulnerabilities — never a
public issue for those.
