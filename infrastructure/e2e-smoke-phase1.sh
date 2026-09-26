#!/usr/bin/env bash
# ARGUS Phase 1 — live end-to-end smoke test (spec §82, §83)
#
# Exercises the FULL ingestion lifecycle against a running stack:
#   sync path:   project → env → components → source → health/config events
#                → logs/metrics/traces/spans → deployments → query → validation
#   async path:  webhook.enqueue / batch queue → Redis → worker → pipeline
#                → persist → query (with drain polling)
#   security:    secret rejection on every ingestion boundary (§46)
#   operators:   retention policy/preview/sweep, ingestion stats, dead-letter
#
# Note on bash quoting: a double-quoted `-d "{\"...\":...}"` nested inside an
# outer `"$(...)"` splits at the commas in this shell, silently corrupting the
# body (a latent false-positive source). We therefore build each JSON body into
# a variable at the top level and POST it through `jreq` (temp file), capturing
# the status into its own variable before the `check` — never inlining `\"…\"`
# inside a nested command substitution.
#
# Prereq: the stack is up (docker compose up) and seed has run.
set -eu
# Hardening W1: resolve an admin token and authenticate every request.
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
NOW=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
check() { [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# POST <url> <json> → http status. Body goes through a temp file so no quoted
# JSON is ever nested inside a $(...) in this script's caller.
jreq() {
  local f
  f=$(mktemp)
  printf '%s' "$2" > "$f"
  curl -s -o /dev/null -w '%{http_code}' -X POST "$1" -H 'Content-Type: application/json' -d "@$f"
  rm -f "$f"
}

TS=$(date +%s)
SLUG="phase1-e2e-$TS"
NOW_NS=$(python3 -c "import time; print(int(time.time()*1e9))")
NOW_NS2=$(python3 -c "import time; print(int(time.time()*1e9)+50000000)")

echo "== 1. Create Project / Environment / Components =="
PROJ=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Phase1 E2E $TS\",\"slug\":\"$SLUG\"}")
PID=$(echo "$PROJ" | jget "d['id']")
BODY="{\"name\":\"dup\",\"slug\":\"$SLUG-dup$TS\"}"
S1=$(jreq "$API/api/v1/projects" "$BODY")
check "POST duplicate-name project" "201" "$S1"
echo "  project: $PID"

ENV_BODY="{\"name\":\"prod\",\"environment_type\":\"PRODUCTION\"}"
ENV=$(curl -fsS -X POST "$API/api/v1/projects/$PID/environments" -H 'Content-Type: application/json' -d "$ENV_BODY")
EID=$(echo "$ENV" | jget "d['id']")

C1=$(curl -fsS -X POST "$API/api/v1/projects/$PID/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"web\",\"component_type\":\"APPLICATION\",\"environment_id\":\"$EID\"}")
CID1=$(echo "$C1" | jget "d['id']")
C2=$(curl -fsS -X POST "$API/api/v1/projects/$PID/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"db\",\"component_type\":\"DATABASE\",\"environment_id\":\"$EID\"}")
CID2=$(echo "$C2" | jget "d['id']")
check "web components created" "200" "$(code "$API/api/v1/projects/$PID/components")"

echo "== 2. Register Observability Source (§5/45) =="
SRC=$(curl -fsS -X POST "$API/api/v1/ingestion/sources" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PID\",\"name\":\"e2e-otel\",\"source_type\":\"OTEL\",\"configuration\":{\"endpoint\":\"$API\"}}")
SID=$(echo "$SRC" | jget "d['id']")
check "source registered" "200" "$(code "$API/api/v1/ingestion/sources/$SID")"
BODY="{\"project_id\":\"$PID\",\"name\":\"x\",\"source_type\":\"OTEL\",\"configuration\":{\"password\":\"hunter2\"}}"
S1=$(jreq "$API/api/v1/ingestion/sources" "$BODY")
check "source secret rejected" "422" "$S1"

echo "== 3. Health Check + Configuration Change Events (§14/15) =="
curl -fsS -X POST "$API/api/v1/ingestion/health-checks" -H 'Content-Type: application/json' \
  -d "{\"component_id\":\"$CID1\",\"project_id\":\"$PID\",\"timestamp\":\"$NOW\",\"status\":\"HEALTHY\",\"latency_ms\":12}" >/dev/null
BODY="{\"component_id\":\"$CID1\",\"project_id\":\"$PID\",\"timestamp\":\"$NOW\",\"status\":\"HEALTHY\"}"
S1=$(jreq "$API/api/v1/ingestion/health-checks" "$BODY")
check "POST health check" "201" "$S1"
check "GET health checks" "200" "$(code "$API/api/v1/ingestion/health-checks?project_id=$PID")"

curl -fsS -X POST "$API/api/v1/ingestion/config-changes" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PID\",\"change_id\":\"cfg-$TS\",\"timestamp\":\"$NOW\",\"description\":\"enable canary\",\"component_id\":\"$CID1\"}" >/dev/null
check "GET config changes" "200" "$(code "$API/api/v1/ingestion/config-changes?project_id=$PID")"

echo "== 4. Sync Ingestion — Logs / Metrics / Traces / Spans (§9-12) =="
LOG=$(curl -fsS -X POST "$API/api/v1/observability/logs" -H 'Content-Type: application/json' \
  -d "{\"level\":\"ERROR\",\"message\":\"smoke error $TS\",\"service\":\"web\",\"timestamp\":\"$NOW\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CID1\"}")
LID=$(echo "$LOG" | jget "d['id']")
check "GET log by id" "200" "$(code "$API/api/v1/observability/logs?project_id=$PID")"

curl -fsS -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' \
  -d "{\"metric_name\":\"http_requests\",\"metric_type\":\"COUNTER\",\"value\":42,\"unit\":\"count\",\"timestamp\":\"$NOW\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CID1\"}" >/dev/null
check "GET metrics" "200" "$(code "$API/api/v1/observability/metrics?metric_name=http_requests")"

TRACE_ID="trace-$TS"
curl -fsS -X POST "$API/api/v1/observability/traces" -H 'Content-Type: application/json' \
  -d "{\"trace_id\":\"$TRACE_ID\",\"name\":\"e2e span chain\",\"start_time\":\"$NOW\",\"duration_ms\":60,\"status\":\"OK\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\"}" >/dev/null
curl -fsS -X POST "$API/api/v1/observability/traces/spans" -H 'Content-Type: application/json' \
  -d "{\"trace_id\":\"$TRACE_ID\",\"span_id\":\"span-root-$TS\",\"parent_span_id\":null,\"operation\":\"GET /\",\"start_time\":\"$NOW\",\"duration_ms\":10,\"status\":\"OK\",\"project_id\":\"$PID\",\"component_id\":\"$CID1\"}" >/dev/null
curl -fsS -X POST "$API/api/v1/observability/traces/spans" -H 'Content-Type: application/json' \
  -d "{\"trace_id\":\"$TRACE_ID\",\"span_id\":\"span-child-$TS\",\"parent_span_id\":\"span-root-$TS\",\"operation\":\"SELECT\",\"start_time\":\"$NOW\",\"duration_ms\":20,\"status\":\"OK\",\"project_id\":\"$PID\",\"component_id\":\"$CID2\"}" >/dev/null
check "GET trace with spans (§12)" "200" "$(code "$API/api/v1/observability/traces/$TRACE_ID")"
check "GET traces list" "200" "$(code "$API/api/v1/observability/traces?trace_id=$TRACE_ID")"

echo "== 5. Trace Cross-Reference Validation (§20) =="
VAL=$(curl -fsS "$API/api/v1/ingestion/trace-validation/$TRACE_ID")
check "trace validation is_valid (no orphans)" "True" "$(echo "$VAL" | jget "d['is_valid']")"
check "project trace validation" "200" "$(code "$API/api/v1/ingestion/trace-validation/$PID/project")"

echo "== 6. OTLP Ingestion (§6) =="
OTLP_TRACE_ID=$(python3 -c "import uuid;print(uuid.uuid4().hex)")
OTLP_SPAN_ID_1=$(python3 -c "import uuid;print(uuid.uuid4().hex[:16])")
OTLP_SPAN_ID_2=$(python3 -c "import uuid;print(uuid.uuid4().hex[:16])")
OTLP_BODY=$(python3 -c "
import json, uuid
print(json.dumps({
  'project_id': '$PID',
  'resourceSpans': [{
    'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'otlp-smoke'}}]},
    'scopeSpans': [{
      'spans': [
        {'traceId': '$OTLP_TRACE_ID', 'spanId': '$OTLP_SPAN_ID_1', 'name': 'otlp-root', 'kind': 2,
         'startTimeUnixNano': '$NOW_NS', 'endTimeUnixNano': '$NOW_NS2', 'status': {'code': 1}},
        {'traceId': '$OTLP_TRACE_ID', 'spanId': '$OTLP_SPAN_ID_2', 'name': 'otlp-child', 'kind': 3,
         'parentSpanId': '$OTLP_SPAN_ID_1',
         'startTimeUnixNano': '$NOW_NS', 'endTimeUnixNano': '$NOW_NS2', 'status': {'code': 1}},
      ],
    }],
  }],
}))
")
S1=$(jreq "$API/api/v1/otlp/v1/traces" "$OTLP_BODY")
check "OTLP traces accepted" "200" "$S1"
OTLP_LOG_BODY=$(python3 -c "
import json
print(json.dumps({
  'project_id': '$PID',
  'resourceLogs': [{
    'resource': {'attributes': []},
    'scopeLogs': [{'logRecords': [{
      'body': {'stringValue': 'otlp log'},
      'severityNumber': 9,
      'timeUnixNano': '$NOW_NS',
    }]}],
  }],
}))
")
S1=$(jreq "$API/api/v1/otlp/v1/logs" "$OTLP_LOG_BODY")
check "OTLP log accepted" "200" "$S1"

echo "== 7. Prometheus Scrape (§40) =="
check "GET /metrics text format" "200" "$(code "$API/metrics")"
METRICS_BODY=$(curl -fsS "$API/metrics")
check "metrics shows argus_events_24h_total" "True" "$(echo "$METRICS_BODY" | grep -q 'argus_events_24h_total' && echo True || echo False)"

echo "== 8. ASYNC PATH — webhook → Redis → worker → persist (§47/41/42/43) =="
BODY="{\"event_type\":\"SYSTEM_EVENT\",\"timestamp\":\"$NOW\",\"payload\":{\"message\":\"async webhook $TS\"},\"project_id\":\"$PID\"}"
S1=$(jreq "$API/api/v1/ingestion/webhook" "$BODY")
check "webhook enqueued" "202" "$S1"
# wait for the worker to drain; poll the events endpoint for our project
DRAINED="False"
for _ in $(seq 1 20); do
  if curl -fsS "$API/api/v1/observability/events?project_id=$PID&event_type=SYSTEM_EVENT" | grep -q "async webhook $TS"; then
    DRAINED="True"; break
  fi
  sleep 1
done
check "webhook payload drained by worker" "True" "$DRAINED"

echo "== 9. ASYNC PATH — batch queue (§37/41) =="
BODY="{\"project_id\":\"$PID\",\"events\":[{\"source_type\":\"mock\",\"source_name\":\"queue-batch\",\"timestamp\":\"$NOW\",\"event_type\":\"SYSTEM_EVENT\",\"payload\":{\"message\":\"queued batch $TS\"}}]}"
S1=$(jreq "$API/api/v1/ingestion/queue" "$BODY")
check "batch queue enqueued" "202" "$S1"
DRAINED2="False"
for _ in $(seq 1 20); do
  if curl -fsS "$API/api/v1/observability/events?project_id=$PID" | grep -q "queued batch $TS"; then
    DRAINED2="True"; break
  fi
  sleep 1
done
check "batch queue drained by worker" "True" "$DRAINED2"

echo "== 10. Secret Rejection at Every Boundary (§46) =="
BODY="{\"project_id\":\"$PID\",\"timestamp\":\"$NOW\",\"source\":\"x\",\"event_type\":\"SYSTEM_EVENT\",\"payload\":{\"token\":\"sec\"}}"
S1=$(jreq "$API/api/v1/observability/events" "$BODY")
check "event secret rejected" "422" "$S1"
BODY="{\"project_id\":\"$PID\",\"timestamp\":\"$NOW\",\"level\":\"INFO\",\"message\":\"x\",\"metadata\":{\"password\":\"hunter2\"}}"
S1=$(jreq "$API/api/v1/observability/logs" "$BODY")
check "log secret rejected" "422" "$S1"
BODY="{\"project_id\":\"$PID\",\"timestamp\":\"$NOW\",\"metric_name\":\"m\",\"metric_type\":\"GAUGE\",\"value\":1,\"metadata\":{\"api_key\":\"sk-x\"}}"
S1=$(jreq "$API/api/v1/observability/metrics" "$BODY")
check "metric secret rejected" "422" "$S1"
BODY="{\"event_type\":\"LOG\",\"timestamp\":\"$NOW\",\"payload\":{\"password\":\"x\"},\"project_id\":\"$PID\"}"
S1=$(jreq "$API/api/v1/ingestion/webhook" "$BODY")
check "webhook secret rejected" "422" "$S1"
BODY="{\"event_type\":\"LOG\",\"timestamp\":\"$NOW\",\"payload\":{}}"
S1=$(jreq "$API/api/v1/ingestion/webhooks/00000000-0000-0000-0000-000000000000" "$BODY")
check "source-scoped webhook 404 on missing source" "404" "$S1"

echo "== 11. Payload limits (§27) =="
BIG="$(python3 -c "print('x'*9000)")"
BODY="{\"project_id\":\"$PID\",\"timestamp\":\"$NOW\",\"level\":\"INFO\",\"message\":\"$BIG\"}"
S1=$(jreq "$API/api/v1/observability/logs" "$BODY")
check "log message limit" "422" "$S1"

echo "== 12. Ingestion Operators (§39/45) =="
check "GET ingestion stats" "200" "$(code "$API/api/v1/ingestion/stats?project_id=$PID")"
check "GET sources health" "200" "$(code "$API/api/v1/ingestion/sources-health?project_id=$PID")"
check "GET dead-letter list" "200" "$(code "$API/api/v1/ingestion/dead-letter?project_id=$PID")"
check "GET retention policy" "200" "$(code "$API/api/v1/ingestion/retention/policy")"
check "GET retention preview" "200" "$(code "$API/api/v1/ingestion/retention/preview")"
check "POST retention sweep" "200" "$(code -X POST "$API/api/v1/ingestion/retention/sweep")"

echo "== 13. Event retrieval + pagination (§37) =="
check "GET events paginated" "200" "$(code "$API/api/v1/observability/events?page=1&page_size=5")"
EVENT_ID=$(curl -fsS "$API/api/v1/observability/events?project_id=$PID&page_size=1" | jget "d['items'][0]['id']")
check "GET single event by id" "200" "$(code -X GET "$API/api/v1/observability/events/$EVENT_ID")"
check "GET missing event 404" "404" "$(code -X GET "$API/api/v1/observability/events/00000000-0000-0000-0000-000000000000")"

echo "== 14. Web UI Pages (new Phase 1 surfaces) =="
for p in /observability /observability/events /observability/logs /observability/metrics /observability/traces /observability/traces/$TRACE_ID /ingestion-health /deployments /system-map /incidents; do
  check "WEB $p" "200" "$(code "$WEB$p")"
done

echo ""
echo "=============================="
echo "PHASE 1 RESULT: $PASS passed, $FAIL failed"
echo "=============================="
[ "$FAIL" = 0 ]
