# Connect Your Own Service to ARGUS

A 10-minute journey from "ARGUS is running" to "ARGUS is reasoning about *my*
telemetry". Everything below was verified against a running stack — the
commands are copy-pasteable, and the expected output is what the platform
actually returns.

If you have not brought the stack up yet, do [quickstart.md](quickstart.md)
first. This document starts where that one ends.

---

## 0. What "connected" means here

ARGUS ingests **OpenTelemetry (OTLP/JSON)** traces, logs and metrics, then
derives everything else itself:

```text
your service
   │  OTLP/JSON  (traces · logs · metrics)
   ▼
ARGUS ingestion edge  ──►  knowledge graph  ──►  anomaly detection  ──►  incidents
```

You do not send ARGUS "incidents" or "root causes". You send telemetry, and the
platform builds the rest. That is the whole point of the ingestion boundary.

---

## 1. Create a project and an environment

A project is the tenancy boundary; every credential and every row is scoped to
one. Sign in to the web UI (`http://localhost:3000`) or use the API.

```bash
export ARGUS=http://localhost:8000
export TOKEN=<your admin token>          # printed once in the API logs on first boot

# create the project
curl -s -X POST "$ARGUS/api/v1/projects" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "Payments", "slug": "payments"}'

# an environment is optional, but every good incident story has one
curl -s -X POST "$ARGUS/api/v1/projects/<PROJECT_ID>/environments" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "production", "environment_type": "PRODUCTION"}'
```

Keep the returned `PROJECT_ID` and `ENVIRONMENT_ID`.

---

## 2. Mint a per-source ingest token

**Do not give your collectors an admin token.** Register the source and rotate
its own credential; the token is shown exactly once.

```bash
curl -s -X POST "$ARGUS/api/v1/ingestion/sources" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"project_id": "<PROJECT_ID>", "name": "otel-collector", "source_type": "OTEL"}'

curl -s -X POST "$ARGUS/api/v1/ingestion/sources/<SOURCE_ID>/rotate-token" \
  -H "Authorization: Bearer $TOKEN"
```

The response contains `ingest_token` — a secret beginning `argus_ing_`. Store it
in your collector's secret store, not in the collector's YAML checked into git.

**What that token can and cannot do**

| It can | It cannot |
| --- | --- |
| write OTLP telemetry into **its own project** | open any other API route (`401`) |
| be revoked instantly (`DELETE .../ingest-token`) | write telemetry into another project (`403`) |

The token decides the project. If a request names a different project, ARGUS
refuses it — and this is asserted live by
`infrastructure/e2e-smoke-hardening.sh`, not just in unit tests.

---

## 3. Point OpenTelemetry at ARGUS

### Option A — OpenTelemetry Collector (recommended)

The collector buffers, batches and retries, so your application never blocks on
telemetry. Two settings are load-bearing and both are easy to miss:

```yaml
# otel-collector-config.yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  batch: {}

exporters:
  otlphttp/argus:
    endpoint: http://argus-host:8000/api/v1/otlp
    # Protobuf is accepted (this is the exporter's default, so no `encoding`
    # line is needed). Set `encoding: json` if you prefer the JSON transport.
    headers:
      X-Argus-Ingest-Token: ${env:ARGUS_INGEST_TOKEN}

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/argus]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/argus]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/argus]
```

```bash
export ARGUS_INGEST_TOKEN=argus_ing_...
./otelcol --config otel-collector-config.yaml
```

Notes that save an afternoon:

* **Both encodings are accepted.** A stock `otlphttp` exporter defaults to
  Protobuf and works unchanged; set `encoding: json` only if you prefer the JSON
  transport. gRPC (`:4317`) is *not* served — use OTLP/HTTP (see §7).
* **You do not need to inject a project id.** A stock exporter cannot add a
  top-level `projectId` field to an `ExportTraceServiceRequest`, so ARGUS
  resolves the destination from the ingest token instead. Sending the field is
  still allowed and is checked against the token's project.
* `endpoint` is the **base** `…/api/v1/otlp`; the exporter appends
  `/v1/traces`, `/v1/logs`, `/v1/metrics` itself.

### Option B — SDK exporter straight from the application

For a single service, the SDK's OTLP/HTTP exporter is enough:

**Python**

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource

resource = Resource.create({"service.name": "payments-api"})
provider = TracerProvider(resource=resource)

# JSON encoding, and the ingest token in the header. No project id needed.
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint="http://argus-host:8000/api/v1/otlp/v1/traces",
            headers={"X-Argus-Ingest-Token": "argus_ing_..."},
        )
    )
)
```

The Python HTTP exporter already speaks JSON, so no extra flag is needed there —
unlike the collector.

**Node.js**

```js
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');

const exporter = new OTLPTraceExporter({
  url: 'http://argus-host:8000/api/v1/otlp/v1/traces',
  headers: { 'X-Argus-Ingest-Token': process.env.ARGUS_INGEST_TOKEN },
});
```

**Anything else that speaks OTLP/HTTP+JSON** works the same way: point it at
`{ARGUS}/api/v1/otlp/v1/{traces,logs,metrics}` and add the header.

---

## 4. Send something minimal by hand (sanity check)

Before wiring a whole service, prove the credential and the path:

```bash
curl -s -X POST "$ARGUS/api/v1/otlp/v1/traces" \
  -H 'Content-Type: application/json' \
  -H "X-Argus-Ingest-Token: $ARGUS_INGEST_TOKEN" \
  -d '{
    "resourceSpans": [{
      "resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "payments-api"}}
      ]},
      "scopeSpans": [{"spans": [{
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "POST /charge",
        "startTimeUnixNano": "1758600000000000000",
        "endTimeUnixNano": "1758600000100000000",
        "status": {"code": 2}
      }]}]
    }]
  }'
```

Expected:

```json
{"accepted": 1, "duplicates": 0, "failed": 0}
```

`accepted: 0` with HTTP 200 means the span was recognised as a duplicate — the
`(traceId, spanId)` pair was already stored. That is idempotency working, not a
failure. Change the `spanId` to send a new one.

---

## 5. Verify ARGUS actually absorbed it

Three independent confirmations, cheapest first.

```bash
# 1. the pipeline is healthy at all
curl -s "$ARGUS/health/ready"

# 2. counters moved (public — no token needed)
curl -s "$ARGUS/metrics" | grep -E "argus_ingestion_queue_depth|argus_source_events_total|argus_events_|argus_traces_"

# 3. the data is queryable in the project scope
curl -s -H "Authorization: Bearer $TOKEN" \
  "$ARGUS/api/v1/projects/<PROJECT_ID>/components"
```

Within a moment the ingestion worker also runs the post-ingest hooks, so the
**knowledge graph** gets the new service relationships and **anomaly detection**
gets a fresh evaluation window. An idle service produces a graph, not incidents
— incidents appear when behaviour actually deviates.

To watch it happen:

```bash
docker compose logs -f worker        # or: docker compose logs -f api
curl -s "$ARGUS/metrics" | grep -E "argus_ingestion_queue_depth|argus_source_events_total"
```

Queue depth is the one number worth watching: it should rise under burst and
drain back to `0`. A depth that only rises means the consumer, not the API, is
the bottleneck.

---

## 6. Where your telemetry goes next

Once data is flowing, the UI path is the story of your system:

```text
/system-map          services and their dependencies, learned from traces
/incidents           abnormal behaviour, correlated into incidents
  └ /causal-analysis   candidate causes, each with its evidence and limits
  └ /reproductions     a controlled experiment attempting to reproduce it
  └ /debugger          the code paths implicated by the failing trace
  └ /fixes             a proposed patch, verified in a sandbox
/reliability         forecasts from the recorded history
/remediation         policy-gated actions (inert until a policy exists — see §7)
```

Everything on those pages is derived from the telemetry you just started
sending. Nothing is seeded or invented.

---

## 7. What this integration does *not* do

Stated plainly, because the failure mode of hiding these is an afternoon lost:

1. **OTLP/Protobuf over HTTP is accepted; gRPC is not.** Both OTLP/HTTP
   encodings work — Protobuf (a stock collector's default) and JSON — but there
   is no listener on `:4317`. A gRPC-only client must be pointed at OTLP/HTTP.
2. **Telemetry older than the ingestion window is not backfilled.** ARGUS
   reasons about what arrives, plus what the detection windows can see.
3. **`environmentId` is optional but meaningful.** Omit it and the environment
   link is empty, which makes environment-scoped pages thinner. Send it when
   you have it.
4. **Remediation actions ship disabled.** Connecting a service does not give
   ARGUS permission to act on it; that is a separate, explicit policy decision.
   See [safe-autonomous-remediation.md](safe-autonomous-remediation.md).
5. **Code intelligence needs a repository.** Telemetry alone gives you
   incidents and causal analysis; source-level debugging additionally requires
   a registered repository (see [quickstart.md](quickstart.md)).

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `400 OTLP_PROTOBUF_DECODE_FAILED` | the body is neither valid protobuf nor JSON (e.g. a JSON payload sent with a protobuf `Content-Type`) | send the bytes the encoder actually produced, or set `encoding: json` and a matching `Content-Type` |
| `422` naming `resourceSpans` | empty or structurally invalid JSON body | check the exporter's `encoding` matches the body |
| `401 Invalid ingest token` | token revoked, mistyped, or a truncated copy | rotate it (`POST …/rotate-token`) and update the collector |
| `401` on a status page | ingest tokens cannot open general API routes | use an API token for reads, the ingest token for OTLP only |
| `403 Credential is not valid for this project` | the body names a project the token does not own | remove `projectId` (it is inferred) or fix the token's source |
| `400 … does not imply a single project` | body omitted `projectId` and the credential is unscoped or multi-grant | send `projectId`, or use a per-source ingest token |
| `429 Rate limit exceeded` | more than 600 requests/min (burst 120) from one credential | send in batches, or raise `RATE_LIMIT_PER_MINUTE` for that deployment |
| `413` on large trace payloads | payload above `MAX_REQUEST_BODY_BYTES` | batch smaller; a collector's `batch` processor handles this |
| `accepted: 0` on a repeat send | duplicate `(traceId, spanId)` | expected — idempotent ingestion |

More failure modes: [troubleshooting.md](troubleshooting.md). Deeper protocol
detail: [ingestion.md](ingestion.md) and [telemetry.md](telemetry.md).

---

## 9. Alternative: webhooks for non-OTLP sources

If a system cannot speak OTLP at all (a legacy job, a SaaS alerting product),
use the webhook edge instead. It is deliberately stricter:

* a delivery addressed to one source must carry that source's ingest token, or
* a deployment-wide `PLATFORM_WEBHOOK_SECRET` must sign the body with
  `X-Argus-Signature`, and replay is refused.

See [ingestion.md](ingestion.md) for the signature scheme and payload envelope.
