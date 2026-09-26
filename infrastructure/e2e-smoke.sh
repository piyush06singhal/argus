#!/usr/bin/env bash
# ARGUS Phase 0 — live end-to-end smoke test (spec section 57 flow)
# Runs against a freshly-seeded stack. Creates a full data lifecycle then
# verifies every API surface responds.
set -eu
# Hardening W1: every request carries the credential the other gates use.
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
NOW=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
check() { [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

TS=$(date +%s)
PROJ_SLUG="audit-e2e-$TS"

echo "== 1. Create Project =="
PROJ=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"E2E Audit $TS\",\"slug\":\"$PROJ_SLUG\",\"description\":\"e2e audit\"}")
PID=$(echo "$PROJ" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "  project id: $PID"

echo "== 2. Create Environment =="
ENV=$(curl -fsS -X POST "$API/api/v1/projects/$PID/environments" -H 'Content-Type: application/json' \
  -d "{\"name\":\"staging\",\"environment_type\":\"STAGING\",\"description\":\"e2e env\"}")
EID=$(echo "$ENV" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "  environment id: $EID"

echo "== 3. Create Components =="
C1=$(curl -fsS -X POST "$API/api/v1/projects/$PID/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"web\",\"component_type\":\"APPLICATION\",\"environment_id\":\"$EID\"}")
CID1=$(echo "$C1" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
C2=$(curl -fsS -X POST "$API/api/v1/projects/$PID/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"db\",\"component_type\":\"DATABASE\",\"environment_id\":\"$EID\"}")
CID2=$(echo "$C2" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "  component ids: $CID1 (web), $CID2 (db)"
check "GET components" "200" "$(code "$API/api/v1/projects/$PID/components")"

echo "== 4. Create Dependency =="
DEP_BODY="{\"source_component_id\":\"$CID1\",\"target_component_id\":\"$CID2\",\"dependency_type\":\"HTTP\"}"
curl -fsS -X POST "$API/api/v1/projects/$PID/dependencies" -H 'Content-Type: application/json' -d "$DEP_BODY" >/dev/null
check "GET dependencies" "200" "$(code "$API/api/v1/projects/$PID/dependencies")"

echo "== 5. Ingest Observability =="
LOG_BODY="{\"level\":\"ERROR\",\"message\":\"e2e audit error\",\"service\":\"web\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CID1\",\"timestamp\":\"$NOW\"}"
curl -fsS -X POST "$API/api/v1/observability/logs" -H 'Content-Type: application/json' -d "$LOG_BODY" >/dev/null
check "POST log" "201" "$(code -X POST "$API/api/v1/observability/logs" -H 'Content-Type: application/json' -d '{"level":"INFO","message":"info","service":"web","project_id":"'$PID'","timestamp":"'$NOW'"}')"

METRIC_BODY="{\"metric_name\":\"http_requests\",\"metric_type\":\"COUNTER\",\"value\":42,\"unit\":\"count\",\"timestamp\":\"$NOW\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CID1\"}"
curl -fsS -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' -d "$METRIC_BODY" >/dev/null
check "POST metric" "201" "$(code -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' -d '{"metric_name":"x","metric_type":"GAUGE","value":1,"timestamp":"'$NOW'","project_id":"'$PID'"}')"

TRACE_ID="e2e-trace-$TS"
TRACE_BODY="{\"trace_id\":\"$TRACE_ID\",\"name\":\"e2e-trace\",\"start_time\":\"$NOW\",\"duration_ms\":30,\"status\":\"OK\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\"}"
TR=$(curl -fsS -X POST "$API/api/v1/observability/traces" -H 'Content-Type: application/json' -d "$TRACE_BODY")
CHECK_TRACE="{\"trace_id\":\"check-trace-$TS\",\"start_time\":\"$NOW\",\"project_id\":\"$PID\",\"duration_ms\":1,\"name\":\"x\"}"
check "POST trace" "201" "$(code -X POST "$API/api/v1/observability/traces" -H 'Content-Type: application/json' -d "$CHECK_TRACE")"

SPAN_BODY="{\"trace_id\":\"$TRACE_ID\",\"span_id\":\"span-1-$TS\",\"project_id\":\"$PID\",\"start_time\":\"$NOW\",\"component_id\":\"$CID1\",\"operation\":\"GET /\",\"duration_ms\":10,\"status\":\"OK\"}"
curl -fsS -X POST "$API/api/v1/observability/traces/spans" -H 'Content-Type: application/json' -d "$SPAN_BODY" >/dev/null
CHECK_SPAN="{\"trace_id\":\"$TRACE_ID\",\"span_id\":\"span-check-$TS\",\"project_id\":\"$PID\",\"start_time\":\"$NOW\",\"duration_ms\":5}"
check "POST span" "201" "$(code -X POST "$API/api/v1/observability/traces/spans" -H 'Content-Type: application/json' -d "$CHECK_SPAN")"

echo "== 6. Create Deployment =="
DEPLOY_BODY="{\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CID1\",\"deployment_id\":\"deploy-e2e-$TS\",\"deployed_at\":\"$NOW\",\"version\":\"2.0.0\",\"status\":\"SUCCESS\"}"
curl -fsS -X POST "$API/api/v1/deployments" -H 'Content-Type: application/json' -d "$DEPLOY_BODY" >/dev/null
CHECK_DEPLOY="{\"project_id\":\"$PID\",\"deployment_id\":\"dup-check-$TS\",\"deployed_at\":\"$NOW\"}"
check "POST deployment" "201" "$(code -X POST "$API/api/v1/deployments" -H 'Content-Type: application/json' -d "$CHECK_DEPLOY")"

echo "== 7. Create Incident + Evidence =="
INC_BODY="{\"title\":\"e2e incident\",\"severity\":\"HIGH\",\"status\":\"OPEN\",\"detected_at\":\"$NOW\",\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"description\":\"e2e\"}"
INC=$(curl -fsS -X POST "$API/api/v1/incidents" -H 'Content-Type: application/json' -d "$INC_BODY")
IID=$(echo "$INC" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
check "POST incident" "201" "$(code -X POST "$API/api/v1/incidents" -H 'Content-Type: application/json' -d '{"title":"x","severity":"LOW","detected_at":"'$NOW'","project_id":"'$PID'"}')"

EVIDENCE_BODY="{\"evidence_type\":\"LOG\",\"source_id\":\"e2e-log-$TS\",\"timestamp\":\"$NOW\",\"description\":\"e2e evidence\"}"
curl -fsS -X POST "$API/api/v1/incidents/$IID/evidence" -H 'Content-Type: application/json' -d "$EVIDENCE_BODY" >/dev/null
CHECK_EVIDENCE="{\"evidence_type\":\"LOG\",\"source_id\":\"check-log-$TS\",\"timestamp\":\"$NOW\"}"
check "POST evidence" "201" "$(code -X POST "$API/api/v1/incidents/$IID/evidence" -H 'Content-Type: application/json' -d "$CHECK_EVIDENCE")"

echo "== 8. Verify All Read Endpoints =="
check "GET project"       "200" "$(code "$API/api/v1/projects/$PID")"
check "GET env"           "200" "$(code "$API/api/v1/environments/$EID")"
check "GET system-map"    "200" "$(code "$API/api/v1/projects/$PID/system-map")"
check "GET components"    "200" "$(code "$API/api/v1/projects/$PID/components")"
check "GET dependencies"  "200" "$(code "$API/api/v1/projects/$PID/dependencies")"
check "GET deployments"   "200" "$(code "$API/api/v1/projects/$PID/deployments")"
check "GET incident"      "200" "$(code "$API/api/v1/incidents/$IID")"
check "GET evidence"      "200" "$(code "$API/api/v1/incidents/$IID/evidence")"
check "GET logs filtered" "200" "$(code "$API/api/v1/observability/logs?project_id=$PID")"
check "GET metrics filtered" "200" "$(code "$API/api/v1/observability/metrics?metric_name=http_requests")"
check "GET traces"        "200" "$(code "$API/api/v1/observability/traces")"
check "GET trace+spans"   "200" "$(code "$API/api/v1/observability/traces/$TRACE_ID")"
check "GET events"        "200" "$(code "$API/api/v1/observability/events")"

echo "== 9. Health Checks =="
check "GET /health/live"          "200" "$(code "$API/health/live")"
check "GET /health/ready"         "200" "$(code "$API/health/ready")"
check "GET /health/dependencies"  "200" "$(code "$API/health/dependencies")"

echo "== 10. Verify Seed Data (ARGUS Demo Commerce) =="
SEED_FOUND=$(curl -fsS "$API/api/v1/projects" | python3 -c "
import json,sys
d=json.load(sys.stdin)
names=[p['slug'] for p in d['items']]
print('argus-demo-commerce' in names)
")
check "Seed project present" "True" "$SEED_FOUND"

echo "== 11. 404 / Error Paths =="
FAKE="00000000-0000-0000-0000-000000000000"
check "GET missing project"  "404" "$(code "$API/api/v1/projects/$FAKE")"
check "DELETE missing project" "404" "$(code -X DELETE "$API/api/v1/projects/$FAKE")"
check "GET missing incident" "404" "$(code "$API/api/v1/incidents/$FAKE")"
check "GET missing trace"    "404" "$(code "$API/api/v1/observability/traces/missing-trace")"

echo "== 12. Web UI Pages =="
for p in / /projects /system-map /incidents /observability/logs /observability/metrics /observability/traces /deployments /settings; do
  check "WEB $p" "200" "$(code "$WEB$p")"
done

echo ""
echo "=============================="
echo "RESULT: $PASS passed, $FAIL failed"
echo "=============================="
[ "$FAIL" = 0 ]
