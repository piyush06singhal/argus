# Operations guide

How to run, size, back up, upgrade and troubleshoot ARGUS in a real
deployment. Everything here refers to settings that exist in
`apps/api/app/core/config.py` and to endpoints that exist in the running API.

---

## 1. Topology

```text
                    ┌──────────────┐
   browser ────────▶│  web :3000   │  Next.js console (server-rendered)
                    └──────┬───────┘
                           │ HTTP (bearer token)
                    ┌──────▼───────┐        ┌────────────┐
   collectors ─────▶│  api :8000   │───────▶│ postgres   │  durable state
   (OTLP/ingest)    │              │        └────────────┘
                    │  workers ────┼───────▶┌────────────┐
                    │  sweeps      │        │ redis      │  queues + cache
                    └──────────────┘        └────────────┘
```

| Service | Role | Stateful |
| --- | --- | --- |
| `postgres` | All durable state | **Yes — back this up** |
| `redis` | Queues, dedupe cache, coordination | Yes for in-flight jobs only |
| `api` | HTTP API, background worker, periodic sweeps | No (rebuildable) |
| `web` | Console (SSR); no database access of its own | No |

The API process runs the queue worker *and* the periodic sweeps inside the same
container by default. That is deliberate for single-node deployments; the
trade-offs and the knobs are in §5.

---

## 2. Configuration

`.env` is read by compose; `apps/api/app/core/config.py` is the single source of
truth for what exists, including defaults. Categories that matter operationally:

### Production semantics

| Setting | Effect |
| --- | --- |
| `API_ENVIRONMENT=production` | Enables production semantics. `AUTH_DISABLED=true` is **refused** (process will not start). |
| `SEED_DEMO` | Unset = seeded outside production, skipped in production. Set `false` to always start empty. |
| `AUTH_DISABLED` | Unset = enforced everywhere except `test`. `true` = local bypass, refused in production. |

### Exposure and limits

| Setting | Default | Notes |
| --- | --- | --- |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | **Set this to your console's real origin.** |
| `MAX_REQUEST_BODY_BYTES` | 10 MiB | `413` above this, before the body is read |
| `RATE_LIMIT_ENABLED` / `_PER_MINUTE` / `_BURST` | on / 600 / 120 | Per worker — see §6 |
| `API_WORKERS` | 4 | Uvicorn workers |

### Data retention

Every derived dataset has a retention window; nothing grows unboundedly.

| Setting | Default (days) | Setting | Default (days) |
| --- | --- | --- | --- |
| `RETENTION_LOGS` | 90 | `RETENTION_REPRODUCTIONS` | 180 |
| `RETENTION_METRICS` | 90 | `RETENTION_CODE_SNAPSHOTS` | 365 |
| `RETENTION_TRACES` | 30 | `RETENTION_RELIABILITY_FORECASTS` | 180 |
| `RETENTION_EVENTS` | 90 | `RETENTION_RELIABILITY_EVALUATIONS` | 365 |
| `RETENTION_ANOMALIES` | 90 | `RETENTION_REMEDIATION_ACTIONS` | 730 |
| `RETENTION_INCIDENTS` | 365 | `RETENTION_LEARNING_EVENTS` | 180 |
| `RETENTION_CAUSAL_ANALYSES` | 365 | `RETENTION_PLATFORM_EVENTS_DAYS` | 365 |

Preview before you delete anything:

```bash
GET  /api/v1/ingestion/retention/policy      # effective policy
GET  /api/v1/ingestion/retention/preview     # what a sweep would remove
POST /api/v1/ingestion/retention/sweep       # do it
```

### Background work

| Setting | Default | Purpose |
| --- | --- | --- |
| `INGESTION_WORKER_ENABLED` | true | Run the queue drainer in this process |
| `ANOMALY_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 60 | Rolling-window detection |
| `REPRO_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 60 | Reap leaked/timeout sandboxes |
| `CODE_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 120 | Code-index incremental work |
| `FIX_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 120 | Patch verification progress |
| `RELIABILITY_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 300 | Forecast refresh, drift |
| `REMEDIATION_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 60 | Approvals, execution, rollback |
| `INTELLIGENCE_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 300 | Experience building, mining |
| `PLATFORM_SWEEP_ENABLED` / `_INTERVAL_SECONDS` | true / 120 | Cases, SLOs, data quality |

Every sweep is idempotent and bounded (batch sizes come from `*_BATCH` /
`*_LIMIT` settings), so an interrupted run is safe to repeat.

---

## 3. Health and observability

```bash
curl -sS localhost:8000/health/live          # process is up (no dependencies)
curl -sS localhost:8000/health/ready         # can serve traffic (checks the DB)
curl -sS localhost:8000/health/dependencies  # per-dependency status + latency
curl -sS localhost:8000/metrics              # Prometheus exposition
```

* `/health/live` is the right **liveness** probe (it must not fail because
  Postgres blinked — that would restart a healthy process).
* `/health/ready` is the right **readiness** probe.
* `/metrics` is unauthenticated by design (standard Prometheus convention:
  scrape it on a private interface). It covers ingestion, queue depth,
  detection, incidents, reproduction, fix verification, forecasts, remediation
  and learning. `GET /api/v1/metrics` and `GET /api/v1/platform/metrics` expose
  the same domains as JSON for dashboards that are not Prometheus.

The scrape also carries **self-observability** series about the process itself —
`argus_instance_info{instance, region, version}`, the rate-limit backend and its
fallback count, and the backup/drill freshness series — because some facts only
exist inside a running process. A **Prometheus + Grafana stack with 20 alert
rules and an overview dashboard ships in `infrastructure/observability/`** and is
brought up under the `observability` compose profile:

```bash
docker compose --profile observability up -d     # Prometheus :9090, Grafana :3001
```

Every rule carries a severity, a summary and a runbook link, and the live gate
`infrastructure/e2e-smoke-observability.sh` fails if a rule references a series
that the scrape does not actually export — a rule that can never fire reads as
coverage. §10 is the runbook those rules link to.

Logs are JSON (`LOG_FORMAT=json`) with `LOG_LEVEL` controlling verbosity. The
bootstrap token is printed **once** as a `WARNING` at first boot — capture it
from your log pipeline, then treat it as a secret.

---

## 4. Backup and restore

The only thing that cannot be regenerated is **Postgres**. Redis holds queues and
caches; code snapshots and reproduction artifacts are derived, though
reproducing them costs time.

```bash
# Backup (custom format, compressed, consistent snapshot)
docker compose exec -T postgres pg_dump -U "$DATABASE_USER" -Fc "$DATABASE_NAME" \
  > argus-$(date +%Y%m%d-%H%M).dump

# Restore into a fresh database
docker compose exec -T postgres pg_restore -U "$DATABASE_USER" -d "$DATABASE_NAME" \
  --clean --if-exists < argus-YYYYMMDD-HHMM.dump
```

`infrastructure/backup.sh` wraps both directions with safety checks — use it
rather than the raw `pg_dump`/`pg_restore` above:

```bash
bash infrastructure/backup.sh backup            # dump, then prove the archive
bash infrastructure/backup.sh verify DUMP       # table of contents + full decompression
bash infrastructure/backup.sh drill             # restore the newest dump into a scratch DB
bash infrastructure/backup.sh restore DUMP      # refuses a non-empty target unless --force
bash infrastructure/backup.sh list              # what is in the backup directory
```

**`backup` records a count floor before it starts dumping.** Those counts are a
reference for the drill, not a description of the archive: ARGUS writes while you
back up, and the high-volume tables (`reliability_forecasts`) are *revised* — rows
deleted and re-inserted — so any count taken around the dump wanders by a fraction
of a percent. A drill that demanded exact equality therefore failed on healthy
backups at random, which is how a real failure gets ignored. Instead the drill:

* restores with `pg_restore --exit-on-error` (a single unloadable object is a
  failure, because "restored, with warnings" is not a restore);
* requires every counted table to be present in the restore;
* fails any table under-restored beyond 2% of its floor (`DRIFT_TOLERANCE`,
  overridable), with a one-row slack for small tables so the check stays
  effectively exact where the data is small.

The load-bearing proofs are the **full decompression** in `verify` and
`--exit-on-error` in `drill`. The counts are the gross-loss detector — and they
are real: a truncated archive is refused by both commands, and an inflated floor
is refused by the drill (both pinned in `infrastructure/e2e-smoke-backup.sh`).

**Scheduling is shipped, not left to you.** The `backup` compose profile runs
`infrastructure/backup-scheduler.sh`, which takes a dump on an interval, runs a
restore **drill** on a longer interval, and prunes dumps older than the retention
window:

```bash
docker compose --profile backup up -d       # argus-backup, restart: unless-stopped
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `BACKUP_INTERVAL_HOURS` | 24 | how often to dump |
| `BACKUP_DRILL_INTERVAL_HOURS` | 168 | how often to rehearse a restore (weekly) |
| `BACKUP_RETENTION_DAYS` | 14 | how long dumps are kept locally |
| `BACKUP_DIR` | `/backups` | where dumps are written **inside the container** |

Each run records its outcome in Postgres, which is what makes the freshness
series real: `argus_backup_last_success_timestamp_seconds`,
`argus_backup_last_drill_timestamp_seconds`, `argus_backup_runs_*` and the last
dump's size/duration. A
backup that stops running must page someone, so the rules
`ArgusBackupStale`/`ArgusBackupNeverSucceeded`/`ArgusBackupDrillStale` key on
`absent(...)` as well as on age — a series that never appeared is the failure
mode a plain `time() - ts > x` check misses entirely.

The scheduler writes dumps to a **container-local** volume. Keep at least one
copy **off the host** (ship `BACKUP_DIR` to object storage), and keep in mind
that a `pg_dump`/scheduled backup is *not* a substitute for continuous WAL
archiving — a dump recovers to the moment it ran, while archived WAL recovers to
a moment you choose. See [high-availability.md](high-availability.md).

Credentials are not in the dump — you still need your `.env` (and the bootstrap
token, if you have not minted a replacement).

### What to do if Redis is lost

Nothing is permanently lost: in-flight jobs are re-derivable, because the
sweeps re-evaluate stored telemetry on their next pass. On restart ARGUS
reconciles and continues. Reproduction sandboxes that were mid-run are reaped as
timed out and can be retried from stored plans.

---

## 5. Scaling

### Vertical first

One API process with 2–4 workers handles a substantial single-team workload,
because the expensive work is batched and bounded. Increase `API_WORKERS` before
adding hosts.

### Splitting the roles

For heavier load, separate the HTTP server from the background work:

1. Run one process with `INGESTION_WORKER_ENABLED=false` and all
   `*_SWEEP_ENABLED=false` behind your load balancer (HTTP only).
2. Run a single process (or a small number) with the worker and sweeps enabled
   and **no** external traffic.

This matters because queue drainers should not compete with request latency on
the same worker, and because a `memory` rate-limit backend (§6) is per process —
a shared Redis backend keeps the ceiling global across the split.

### Database

Postgres is the bottleneck in every large deployment. Keep `postgres_data` on
fast local storage, ensure the connection pool is sized for `API_WORKERS`
(each worker holds its own pool), and if ingest volume is very high, move OTLP
collection to a collector that batches — ARGUS already dedupes replays, so
retries are cheap.

---

## 6. Rate limiting and capacity

The limiter is a token bucket keyed by credential (falling back to client IP).
`RATE_LIMIT_BACKEND` decides **where the bucket lives**:

* **`redis` (or `auto`, the default)** — the bucket is shared through
  `REDIS_URL`, so the ceiling is global across every API worker and host. This is
  what makes a multi-replica deployment enforce one number rather than
  *N × the number*.
* **`memory`** — the bucket lives in process memory. The effective ceiling is
  then `API_WORKERS × RATE_LIMIT_PER_MINUTE` per **host**. Use it only for a
  single-process deployment or a deliberately per-replica limit.

`auto` uses Redis when it is reachable and falls back to per-process buckets
when it is not — the designed degradation. Two facts make the fallback safe to
operate:

* `argus_rate_limit_backend` reports `1` when the shared backend is in use and
  `0` when this process is limiting locally;
* `argus_rate_limit_fallbacks_total` counts every fallback.

Both are alerted on (`ArgusRateLimitDegraded`, `ArgusRateLimitFallbacks`): a
replica silently limiting locally does **not** enforce the documented ceiling,
and nothing in Postgres would ever say so. Alerts and dashboard panels for the
whole self-observability surface are provisioned by
`infrastructure/observability/` (see §10).

* **`429` is honest**, not a bug: the response carries `error_code: RATE_LIMITED`
  and `Retry-After`-style guidance in its body. Clients should back off.

For a first capacity estimate, run the harness against your hardware rather than
trusting a number from someone else's:

```bash
bash infrastructure/load-benchmark.sh        # documented load profile + results
```

Publish your own numbers from it; see
[argus-benchmark-report.md](argus-benchmark-report.md) for the reference run
and its exact commands.

---

## 7. Upgrades

```bash
git pull
docker compose pull postgres redis     # if you pin new images
docker compose build api web
docker compose up -d                   # entrypoint runs alembic upgrade head
docker compose logs -f api             # verify migrations + startup
```

Rules that keep upgrades boring:

1. **Back up first** (§4). Schema migrations are forward-only.
2. **Read `CHANGELOG.md`** for behaviour changes and any required setting.
3. **Keep `.env`** — new settings have safe defaults, but read the release notes
   for the ones that change behaviour deliberately (e.g. auth becoming enforced).
4. **Roll back** by redeploying the previous image and restoring the dump; do
   not attempt a downgrade migration by hand.
5. **Health-gate the rollout**: wait for `/health/ready` before sending traffic.

Destructive schema changes are avoided; when a column must be dropped, it is
deprecated in one release and removed in the next.

---

## 8. Routine checks

| Cadence | Check |
| --- | --- |
| Daily | `/health/ready`, error rate, queue depth, dead-letter list (`GET /api/v1/ingestion/dead-letter`) |
| Daily | Sources reporting errors (`GET /api/v1/ingestion/sources-health`) |
| Weekly | Backup completed **and** a restore rehearsed on a scratch DB |
| Weekly | Platform data-quality centre (`/api/v1/platform/data-quality`) for gaps or staleness |
| Monthly | Retention preview, then sweep; review `CHANGELOG.md` |
| Monthly | Rotate ingest tokens that belong to contractors or retired collectors |
| Quarterly | Review remediation policy; confirm `OBSERVE_ONLY` is still what you intend |
| Quarterly | Re-run the load harness and compare against the published envelope |

---

## 9. Disaster recovery

| Scenario | Impact | Recovery |
| --- | --- | --- |
| API container dies | Console errors, ingest fails | Restart; sweeps resume and re-evaluate stored telemetry. No data loss. |
| Redis lost | Queues and caches empty | Restart Redis; the platform reconciles and continues. In-flight experiments are reaped as timed out and can be retried. During the outage telemetry ingestion continues through the synchronous fallback (verified live by `infrastructure/e2e-smoke-faults.sh`); the queue drain after recovery is part of that gate. |
| Postgres lost without backup | **Total loss of history** | Restore from backup. This is the only scenario with real loss — which is why §4 exists. |
| Postgres corrupt | Partial read failures | Restore to a new volume, point `DATABASE_URL` at it, restart API. |
| A credential leaked | Someone can act as that token | Revoke it (`DELETE /api/v1/auth/tokens/{id}`); review the auth audit trail for its use; rotate ingest tokens in the affected source. |
| Runaway remediation | Unwanted change | `EMERGENCY_STOP` at the database level, `REMEDIATION_KILL_SWITCH` at the process level, then roll back the affected actions (rollback is itself an audited action). |
| Disk full | Ingest fails, DB writes fail | Retention sweep, prune reproduction artifacts (`REPRO_ARTIFACT_ROOT`), expand the volume. |
| Whole host lost (multi-region) | Region offline | Fail over to the replica in another region: promote it and point `DATABASE_URL` at it. Requires the HA overlay and a rehearsed promotion — see [high-availability.md](high-availability.md). |

For a redundant database rather than a single one, apply the HA overlay
(`docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d`) and read
[high-availability.md](high-availability.md): streaming replica, continuous WAL
archiving, and the rehearsed point-in-time recovery that turns "restore the last
dump" into "restore to the moment you choose".

---

## 10. Alert runbook

Every alert rule in `infrastructure/observability/prometheus/alerts.yml` links a
section here. Read the alert's `summary` first, then the matching entry below.

### The API is down

`ArgusApiDown` = `/metrics` unreachable for 2 minutes; `ArgusApiTargetMissing` =
the scrape target vanished from service discovery entirely (usually a renamed or
removed container, or Prometheus running a stale config).

1. `docker compose ps` — is `api` running and healthy?
2. `docker compose logs --tail=100 api` — a boot failure is almost always a
   migration error or an unreachable database. `/health/live` distinguishes "the
   process is up" from "the dependencies are up".
3. If the target is missing, reload Prometheus: `docker compose restart prometheus`.

### Rate limiting degraded

`argus_rate_limit_backend == 0` (this process is limiting in memory), or
`argus_rate_limit_fallbacks_total` is climbing. The effective ceiling is no
longer global.

1. Check `REDIS_URL` reachability from the API container.
2. Confirm `RATE_LIMIT_BACKEND` is not set to `memory` on a multi-replica
   deployment.
3. When Redis returns, the backend counter returns to `1` and the ceiling is
   global again. See §6.

### Single sign-on failures

`ArgusSsoLoginFailures` — failed OIDC logins in a 30-minute window. A spike is
usually a provider-side change, a clock skew, or an expired client secret.

1. Read the authentication audit trail: every refusal is recorded with a stable
   reason (`oidc:`, `id_token_invalid`, `state_unknown`, `token_exchange_failed`).
2. `token_exchange_failed` → verify `OIDC_CLIENT_SECRET` and the redirect URI at
   the provider.
3. `id_token_invalid` → check clock skew (`OIDC_LEEWAY_SECONDS`) and that the
   provider's JWKS is reachable.
4. `ArgusSsoEnabledButSilent` means SSO is configured but nobody is logging in —
   check the login link is actually exposed on `/connect`.
5. To separate "the provider is broken" from "ARGUS is broken", run
   `bash infrastructure/e2e-smoke-sso.sh`: it drives the full flow against a
   strict stub provider (discovery, JWKS, PKCE, client auth) and asserts both the
   success path and every refusal. A green run points at your provider; a red one
   reproduces the defect locally without needing it.

### No data arriving

`ArgusSourcesSilent` — a source has stopped sending; `ArgusSourcesFailing` — a
source is sending but erroring. This is the quiet-failure pair: a healthy-looking
console on a silent or broken ingest.

1. `GET /api/v1/ingestion/sources-health` — which sources report errors?
2. Verify the collector is still sending to `/api/v1/otlp/v1/*` with a valid
   ingest token.
3. Check the queue consumer (`INGESTION_WORKER_ENABLED`) and Redis.

### Ingestion lag

`ArgusIngestionBacklog` — the queue is draining slower than it fills.

1. Check queue depth (`PLATFORM_QUEUE_DEPTH_WARN`).
2. Confirm the worker role is running (`docker compose --profile worker ps`).
3. Look for a slow database: `PLATFORM_DB_SLOW_QUERY_MS`.

### Dead letters

`ArgusDeadLetterGrowing` — rows the ingestion pipeline could not process and set
aside are accumulating.

1. `GET /api/v1/ingestion/dead-letter` — read the failure reason per row.
2. A malformed payload is a client fix; a schema change is a server fix; replay
   after correcting the source.

### Data-quality backlog

`ArgusDataQualityBacklog` — open data-quality issues are accumulating.

1. `GET /api/v1/platform/data-quality` — group by kind.
2. Most are missing or stale telemetry; fixing the source clears the issue on
   the next sweep.

### Incident backlog

`ArgusIncidentBacklog` — unresolved incidents are piling up (a staffing signal,
not a bug: incidents are not auto-closed for you); `ArgusMttrDegraded` — mean
time to resolve has regressed, which is a trend, not an individual incident.

1. Triage newest-first in the console; correlate before resolving.
2. A persistent incident on one component usually means a real, untreated fault.

### Investigation pipelines

`ArgusReproductionNotReproducing` — experiments are running but not reproducing;
`ArgusPatchesNotVerified` — patches are generated but failing verification. Every
stage is bounded, so a stall is a stuck sandbox, a full workspace, or a database
keeping the work from being leased.

1. Check the per-stage sweep is enabled and running.
2. Full sandbox workspace? Check `FIX_WORKSPACE_GRACE_SECONDS` and the sweep
   that reaps abandoned work.

### Remediation failures

`ArgusRemediationFailing` — autonomous actions are failing or being refused.

1. Each attempt records its reason and is boundedly retried
   (`REMEDIATION_MAX_EXECUTION_ATTEMPTS`).
2. A circuit breaker (`REMEDIATION_CIRCUIT_FAILURE_THRESHOLD`) opening is the
   system protecting you; investigate the target before re-enabling.
3. Confirmed emergency stop: `REMEDIATION_KILL_SWITCH` (process) or
   `EMERGENCY_STOP` (database). See §9.

### Learning pipeline

`ArgusLearningRunsFailing` — learning runs are failing rather than completing.

1. Confirm `INTELLIGENCE_LEARNING_ENABLED` and the sweep interval.
2. A pass with nothing to learn from is expected on a quiet platform; a pass
   that has not *run* is not.

### Backups and recovery

`ArgusBackupStale` (no successful dump within the expected window),
`ArgusBackupNeverSucceeded` (the series is absent — the scheduler never ran),
`ArgusBackupDrillStale` (a restore has not been rehearsed).

1. `docker compose --profile backup ps` and its logs.
2. Run one by hand: `bash infrastructure/backup.sh run` and
   `bash infrastructure/backup.sh drill`.
3. A backup that has never been *restored* is not a backup. See §4 and
   [high-availability.md](high-availability.md).

---

## 11. Related reading

* [quickstart.md](quickstart.md) — first run
* [api.md](api.md) — the surface you automate against
* [security-architecture.md](security-architecture.md) — trust model
* [troubleshooting.md](troubleshooting.md) — symptom → cause → fix
* [final-architecture-audit.md](final-architecture-audit.md) — what was audited
  and changed in the hardening pass
* [production-readiness.md](production-readiness.md) — the readiness verdict
  with its evidence
