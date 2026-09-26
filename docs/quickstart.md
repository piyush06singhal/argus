# Quickstart — ARGUS in 15 minutes

This is the shortest honest path from `git clone` to **"ARGUS found something in
my system and explained it."** It assumes nothing about ARGUS, and it does not
use the demo dataset — the whole point is to prove the pipeline works on *your*
data.

If you would rather see a populated product first, do
[First run](#first-run-3-minutes) below and then come back to
[Connect your own system](#connect-your-own-system-10-minutes).

---

## Prerequisites

| Requirement | Why | Check |
| --- | --- | --- |
| Docker Engine 24+ with Compose v2 | The whole stack runs in containers; nothing else is installed on your host | `docker compose version` |
| ~4 GB free RAM, 10 GB disk | Postgres, Redis, API, Web, plus sandbox space for reproduction runs | `docker info` |
| `curl` and `jq` (or `python3`) | For the API calls below | `curl --version` |
| Ports 3000, 8000, 5432, 6379 free | Web, API, Postgres, Redis | `lsof -i :8000` |

Python, Node.js and Postgres are **not** required on the host — they are all in
the containers. You only need them if you want to run the test suites
(see [development.md](development.md)).

---

## First run (3 minutes)

```bash
git clone https://github.com/piyush06singhal/argus.git
cd argus
cp .env.example .env
docker compose up --build -d
```

Watch it come up:

```bash
docker compose ps                 # all four services: healthy
docker compose logs -f api        # until "Application startup complete"
```

**Copy your admin token now.** On first boot ARGUS mints one root `ADMIN` token
and prints it once:

```bash
docker compose logs api | grep "bootstrap admin token"
# ARGUS bootstrap admin token (shown ONCE — store it now): argus_xxxxxxxx…
```

That token is stored **hashed** — it can never be shown again. If you lose it,
mint another one inside the container:

```bash
docker compose exec api python -m app.cli bootstrap-token
```

Now open **http://localhost:3000**. The dashboard asks you to connect:

1. Paste the token into the **Connect** page (`/connect`) → *Save token*.
2. The header badge turns green and the dashboard loads.

**That header badge is the honest indicator of everything that follows.** It
says whether the console can actually reach the API with a credential that
works; a red badge is never a styling bug, it is a real connectivity or
credential problem.

> Development convenience: setting `AUTH_DISABLED=true` in `.env` bypasses
> authentication for local work. It is **refused** when
> `API_ENVIRONMENT=production`, so production can never come up half-open.

### What you should see

The compose stack ships a small demo dataset (`SEED_DEMO`, default: on outside
production) so the console is not empty on first boot. That dataset is for
*looking around* — see [demo.md](demo.md) for what it contains and
`SEED_DEMO=false` for how to start with an empty database.

---

## Connect your own system (10 minutes)

Everything below uses the API directly. The same actions exist in the UI; the
API is shown because it is unambiguous and copy-pasteable.

Set up a shell with your token:

```bash
export ARGUS=http://localhost:8000/api/v1
export TOKEN=argus_xxxxxxxx…        # the bootstrap token from above
alias acurl='curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"'
```

### 1. Create a project

A **project** is the unit of isolation. Tokens, telemetry, incidents and learned
knowledge never cross projects.

```bash
acurl -X POST "$ARGUS/projects" -d '{
  "name": "Payments",
  "slug": "payments",
  "description": "Checkout and payment services"
}' | jq .
```

### 2. Create an environment

```bash
acurl -X POST "$ARGUS/projects/<project_id>/environments" -d '{
  "name": "production",
  "environment_type": "PRODUCTION"
}' | jq .
```

### 3. Register an observability source and mint its ingest token

An **ingest token** is a *different credential space* from your API token: it
can only write telemetry for one source, so an OTLP collector in a
less-trusted network never holds an admin credential.

```bash
SOURCE=$(acurl -X POST "$ARGUS/ingestion/sources" -d '{
  "project_id": "<project_id>",
  "environment_id": "<environment_id>",
  "name": "otel-collector",
  "source_type": "OTLP"
}' | jq -r .id)

acurl -X POST "$ARGUS/ingestion/sources/$SOURCE/rotate-token" | jq -r .ingest_token
# argus_ing_xxxxxxxx…   ← store this; it is shown once
```

### 4. Send real telemetry

Point any OpenTelemetry collector at ARGUS, or send a request by hand:

```bash
export INGEST=argus_ing_xxxxxxxx…

curl -sS -X POST "$ARGUS/otlp/v1/metrics" \
  -H "Authorization: Bearer $INGEST" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON' | jq .
{
  "project_id": "<project_id>",
  "resourceMetrics": [{
    "resource": { "attributes": [
      { "key": "service.name", "value": { "stringValue": "checkout" } },
      { "key": "deployment.environment", "value": { "stringValue": "production" } }
    ]},
    "scopeMetrics": [{
      "metrics": [{
        "name": "http.server.duration",
        "gauge": { "dataPoints": [{
          "timeUnixNano": "1758500000000000000",
          "asDouble": 412.5,
          "attributes": [
            { "key": "http.route", "value": { "stringValue": "/checkout" } }
          ]
        }]}
      }]
    }]
  }]
}
JSON
```

The same endpoints exist for `/otlp/v1/logs` and `/otlp/v1/traces`, plus native
JSON at `POST /api/v1/observability/{metrics,logs,traces,events}`.

### 5. Define what "abnormal" means, then detect

Anomaly rules are yours to define — ARGUS does not guess thresholds, because a
threshold that is right for one service is noise for another.

```bash
acurl -X POST "$ARGUS/anomaly-rules" -d '{
  "project_id": "<project_id>",
  "name": "checkout latency z-score",
  "anomaly_type": "LATENCY_SPIKE",
  "condition": "Z_SCORE",
  "z_threshold": 3.0,
  "metric_name": "http.server.duration",
  "window_seconds": 300,
  "min_samples": 5,
  "severity": "HIGH"
}' | jq .

acurl -X POST "$ARGUS/projects/<project_id>/anomalies/detect" | jq .
```

Rules are validated at the boundary: `Z_SCORE` demands a `z_threshold`,
`THRESHOLD` demands a `threshold`, and so on. A rule that could never be
evaluated is refused rather than accepted and silently detecting nothing
(`GET /api/v1/anomaly-rules`, then `POST /api/v1/anomaly-rules/{id}/…` to
enable, disable or update one).

Keep sending samples; once five arrive inside the window, the detector compares
them against the learned baseline. Everything downstream (incidents, causal
analysis, reproduction, forecasts, remediation) keys off anomalies, so this step
is what turns ARGUS from a telemetry store into an intelligence platform.

### 6. Watch it work

* `GET /api/v1/anomalies?project_id=…` — what was detected, with the rule and
  numbers that triggered it
* `GET /api/v1/incidents?project_id=…` — anomalies correlated into incidents
  with severity, timeline and evidence
* `POST /api/v1/incidents/{id}/causal-analysis` — ranked hypotheses, each with
  its supporting evidence, confidence bucket and stated limitations
* Web UI: **Incidents**, **System Map**, **Reliability**, **Remediation**

---

## Where to go next

| I want to… | Read |
| --- | --- |
| Understand what each screen does | [ui-guide.md](ui-guide.md) |
| Call the API from my own tooling | [api.md](api.md) |
| Run it in production, back it up, upgrade it | [operations.md](operations.md) |
| Understand the security model before exposing it | [security-architecture.md](security-architecture.md) |
| Fix something that is not working | [troubleshooting.md](troubleshooting.md) |
| Understand *why* the architecture is shaped this way | [architecture.md](architecture.md) |

---

## Common first-run problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker compose ps` shows `unhealthy` API | Postgres not ready or migrations failed | `docker compose logs postgres`, then `docker compose logs api` |
| Web loads but every page errors | Console has no token yet | Open `/connect` and paste the bootstrap token |
| `401 Unauthorized` on an API call | Missing/expired/revoked token, or token not scoped to that project | `acurl "$ARGUS/auth/whoami"` to see the effective identity and scope |
| `403` on a project you can see in the list | Token is project-scoped and this project is not in its grant list | Use an unscoped token, or mint one with the project granted |
| `413` on a large ingest | Body above `MAX_REQUEST_BODY_BYTES` | Split the batch, or raise the limit deliberately |
| `429` after a burst | Rate limiter (`RATE_LIMIT_PER_MINUTE`) | Back off and retry; see [operations.md](operations.md) for tuning |
| No anomalies appear | Fewer samples than the rule's `min_samples` in the window | Keep sending samples, or lower `min_samples` knowingly |

More in [troubleshooting.md](troubleshooting.md).
