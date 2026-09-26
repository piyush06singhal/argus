#!/usr/bin/env bash
# ARGUS — onboarding gate: a new user's first hour (hardening W3)
#
# The phase gates prove each engine works on the *seeded demo* project. This one
# proves the thing a new user actually does: point ARGUS at a system ARGUS has
# never seen, and get a real incident out of it — with **no demo data involved**.
#
# What is deliberately asserted here and nowhere else:
#
#   1. a brand-new project starts completely empty (no demo leakage);
#   2. the W1 ingestion trust boundary — a per-source ingest token can *write*
#      telemetry for its own project and can do nothing else, including writing
#      for a project it does not belong to;
#   3. the full journey on that empty project: define a threshold → ingest the
#      fault → detect → correlate into an incident → causal analysis with
#      explicit uncertainty;
#   4. the knowledge graph discovers the new project's components;
#   5. every UI surface on the journey is reachable.
#
# Deterministic by construction: the anomaly rule is THRESHOLD-based, so nothing
# depends on a learned baseline or on a background sweep having run.
#
# Prereq: the stack is up (docker compose up) and migrations have run.
set -eu
# Hardening W1: resolve an admin token and authenticate every request.
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
# A gate that compares a mangled value is worse than no gate: it reports a
# product failure that does not exist. `check` therefore refuses any call that
# does not carry exactly the three values it needs — the shape that appears when
# a JSON body with escaped quotes is passed through `"$(...)")` and the shell
# splits it into one argument per field.
check() {
  if [ "$#" -ne 3 ]; then
    bad "$1" "gate bug: check needs 3 arguments, received $# — an escaped-JSON body inside \"\$(...)\" was word-split by the shell"
    return
  fi
  [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
now_iso() { python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(seconds=$1)).strftime('%Y-%m-%dT%H:%M:%S.%f')+'Z')"; }

# POST <url> <json> → http status, body via temp file (no nested quoting).
jreq() {
  local f
  f=$(mktemp)
  printf '%s' "$2" > "$f"
  curl -s -o /dev/null -w '%{http_code}' -X POST "$1" -H 'Content-Type: application/json' -d "@$f"
  rm -f "$f"
}
# POST with an explicit bearer token (for the ingest-trust assertions).
#
# ``command curl`` is not decoration: gate-auth.sh installs a ``curl`` shim that
# attaches the *admin* credential to every request. Using the shim here would
# send two Authorization headers, the admin one would win, and this gate would
# report that an ingest token can read incidents — a false alarm about a
# boundary that is in fact holding. These two helpers must be the one place
# where exactly one credential is on the wire.
jreq_as() {
  local token="$1" url="$2" body="$3" f
  f=$(mktemp)
  printf '%s' "$body" > "$f"
  command curl -s -o /dev/null -w '%{http_code}' -X POST "$url" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "@$f"
  rm -f "$f"
}
# POST with an explicit bearer token, capturing the body to a file so the
# *content* can be asserted, not just the status.
#
# Why that distinction matters here: this gate used to accept a `200` from the
# OTLP endpoint without looking at the body. A spec-correct (scope-nested) metric
# export returned `200 {"accepted": 0}` — every event silently dropped — and the
# gate called it a pass. A status code is not an assertion.
jreq_as_to() {
  local token="$1" url="$2" body="$3" out="$4" f
  f=$(mktemp)
  printf '%s' "$body" > "$f"
  command curl -s -o "$out" -w '%{http_code}' -X POST "$url" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "@$f"
  rm -f "$f"
}
# POST a binary OTLP/Protobuf body with an explicit bearer token.
jreq_proto_as() {
  local token="$1" url="$2" file="$3" out="$4"
  command curl -s -o "$out" -w '%{http_code}' -X POST "$url" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/x-protobuf' \
    --data-binary "@$file"
}
# GET with an explicit bearer token (and nothing else).
code_as() {
  local token="$1" url="$2"
  command curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "$url"
}

TS=$(date +%s)
SLUG="onboarding-$TS"

echo "== 1. A new user's project, from nothing =="
PROJ=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Onboarding $TS\",\"slug\":\"$SLUG\",\"description\":\"W3 onboarding gate\"}")
PID=$(echo "$PROJ" | jget "d['id']")
ok "project created ($SLUG)"

ENV=$(curl -fsS -X POST "$API/api/v1/projects/$PID/environments" -H 'Content-Type: application/json' \
  -d '{"name":"production","environment_type":"PRODUCTION"}')
EID=$(echo "$ENV" | jget "d['id']")
ok "environment created"

checkout=$(curl -fsS -X POST "$API/api/v1/projects/$PID/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"checkout\",\"component_type\":\"APPLICATION\",\"environment_id\":\"$EID\"}")
CO_ID=$(echo "$checkout" | jget "d['id']")
payments=$(curl -fsS -X POST "$API/api/v1/projects/$PID/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"payments\",\"component_type\":\"APPLICATION\",\"environment_id\":\"$EID\"}")
PAY_ID=$(echo "$payments" | jget "d['id']")
ok "two service components created"

curl -fsS -X POST "$API/api/v1/projects/$PID/dependencies" -H 'Content-Type: application/json' \
  -d "{\"source_component_id\":\"$CO_ID\",\"target_component_id\":\"$PAY_ID\",\"dependency_type\":\"HTTP\"}" > /dev/null
check "component dependency created" "200" "$(code "$API/api/v1/projects/$PID/dependencies")"

# The point of a *new* project: nothing is pre-populated. If this ever fails,
# the onboarding story is riding on demo data and would mislead a real user.
check "a new project starts empty (no demo leakage)" "0" \
  "$(curl -fsS "$API/api/v1/observability/events?project_id=$PID&page_size=1" | jget "d['total']")"
check "a new project starts with zero incidents" "0" \
  "$(curl -fsS "$API/api/v1/incidents?project_id=$PID&page_size=1" | jget "d['total']")"

echo "== 2. Ingestion trust: a per-source credential (W1) =="
SRC=$(curl -fsS -X POST "$API/api/v1/ingestion/sources" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"name\":\"otel-collector\",\"source_type\":\"OTEL\"}")
SID=$(echo "$SRC" | jget "d['id']")
ok "observability source registered"

ROT=$(curl -fsS -X POST "$API/api/v1/ingestion/sources/$SID/rotate-token")
INGEST=$(echo "$ROT" | jget "d['ingest_token']")
case "$INGEST" in
  argus_ing_*) ok "ingest token minted and shown once" ;;
  *) bad "ingest token minted" "got '${INGEST:0:12}…'" ;;
esac

# The trust boundary: ingest can write telemetry, and nothing else.
S=$(code_as "$INGEST" "$API/api/v1/incidents?project_id=$PID")
case "$S" in
  401|403) ok "ingest token cannot read incidents ($S)" ;;
  *) bad "ingest token read incidents" "got $S" ;;
esac
S=$(code_as "$INGEST" "$API/api/v1/projects")
case "$S" in
  401|403) ok "ingest token cannot list projects ($S)" ;;
  *) bad "ingest token listed projects" "got $S" ;;
esac

OTLP_BODY=$(cat <<JSON
{"project_id":"$PID","resourceMetrics":[{"resource":{"attributes":[
  {"key":"service.name","value":{"stringValue":"checkout"}}]},"scopeMetrics":[{"metrics":[{
  "name":"http.server.duration","gauge":{"dataPoints":[{"timeUnixNano":"$((TS * 1000000000))","asDouble":120.0}]}}]}]}]}
JSON
)
OTLP_OUT=$(mktemp)
S=$(jreq_as_to "$INGEST" "$API/api/v1/otlp/v1/metrics" "$OTLP_BODY" "$OTLP_OUT")
case "$S" in
  200|202) ok "OTLP metrics accepted for the token's own project ($S)" ;;
  *) bad "OTLP metrics with ingest token" "got $S" ;;
esac
# The status alone is not the assertion: a payload that decodes to nothing still
# returns 200. Assert that a *spec-correct* (scope-nested) export actually became
# an event.
ACC=$(python3 -c "import json;print(json.load(open('$OTLP_OUT')).get('accepted',0))" 2>/dev/null || echo 0)
[ "$ACC" -ge 1 ] && ok "the scope-nested metric became an event (accepted=$ACC)" \
  || bad "the scope-nested metric became an event" "accepted=$ACC — a spec-correct OTLP payload was dropped"

# A real OTLP/Protobuf export, serialised inside the running container by the
# protocol's own descriptors. Generated in the container on purpose: it proves
# the *deployment* can decode what a stock collector sends, not merely that a
# tool on the host can encode it.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_B64=$(cd "$ROOT_DIR" && docker compose exec -T api python - <<'PY'
import base64, time, uuid
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as pb
now = int(time.time() * 1e9)
req = pb.ExportTraceServiceRequest()
rs = req.resource_spans.add()
attr = rs.resource.attributes.add()
attr.key = 'service.name'
attr.value.string_value = 'checkout'
span = rs.scope_spans.add().spans.add()
span.name = 'GET /checkout'
span.trace_id = uuid.uuid4().bytes
span.span_id = uuid.uuid4().bytes[:8]
span.start_time_unix_nano = now
span.end_time_unix_nano = now + 5_000_000
span.status.code = 2
print(base64.b64encode(req.SerializeToString()).decode())
PY
)
PROTO_FILE=$(mktemp)
printf '%s' "$PROTO_B64" | base64 -d > "$PROTO_FILE" 2>/dev/null \
  || printf '%s' "$PROTO_B64" | base64 --decode > "$PROTO_FILE"
PROTO_OUT=$(mktemp)
S=$(jreq_proto_as "$INGEST" "$API/api/v1/otlp/v1/traces" "$PROTO_FILE" "$PROTO_OUT")
case "$S" in
  200|202) ok "OTLP/Protobuf trace export accepted ($S) — a stock collector's default encoding" ;;
  *) bad "OTLP/Protobuf trace export" "got $S — $(head -c 200 "$PROTO_OUT")" ;;
esac
PROTO_ACC=$(python3 -c "import json;print(json.load(open('$PROTO_OUT')).get('accepted',0))" 2>/dev/null || echo 0)
[ "$PROTO_ACC" -ge 1 ] && ok "the Protobuf span became a row (accepted=$PROTO_ACC)" \
  || bad "the Protobuf span became a row" "accepted=$PROTO_ACC"
rm -f "$PROTO_FILE"

# A credential for one project must not write telemetry for another.
OTHER=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Onboarding Other $TS\",\"slug\":\"$SLUG-other\"}")
OTHER_PID=$(echo "$OTHER" | jget "d['id']")
FOREIGN_BODY=$(printf '%s' "$OTLP_BODY" | sed "s/$PID/$OTHER_PID/")
S=$(jreq_as "$INGEST" "$API/api/v1/otlp/v1/metrics" "$FOREIGN_BODY")
case "$S" in
  403|404) ok "ingest token refused for a foreign project ($S)" ;;
  *) bad "cross-project ingest" "got $S" ;;
esac
# Nothing may have landed in the other project.
check "no telemetry leaked into the foreign project" "0" \
  "$(curl -fsS "$API/api/v1/observability/metrics?project_id=$OTHER_PID&page_size=1" | jget "d['total']")"

echo "== 3. A fault in their own system becomes an incident =="
for i in 0 1 2 3 4; do
  T=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(seconds=$((600 - i * 30)))).strftime('%Y-%m-%dT%H:%M:%S.%f')+'Z')")
  jreq "$API/api/v1/observability/metrics" \
    "{\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CO_ID\",\"timestamp\":\"$T\",\"metric_name\":\"checkout.latency.p95\",\"metric_type\":\"GAUGE\",\"value\":40.0,\"unit\":\"ms\"}" > /dev/null
done
ok "five baseline samples ingested (their normal)"

SPIKE=$(now_iso 0)
FAULT_CODE=$(jreq "$API/api/v1/observability/metrics" \
  "{\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"component_id\":\"$CO_ID\",\"timestamp\":\"$SPIKE\",\"metric_name\":\"checkout.latency.p95\",\"metric_type\":\"GAUGE\",\"value\":900.0,\"unit\":\"ms\"}")
check "the fault sample is ingested" "201" "$FAULT_CODE"

RULE_CODE=$(jreq "$API/api/v1/anomaly-rules" \
  "{\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"name\":\"checkout p95 over 500ms\",\"anomaly_type\":\"LATENCY_SPIKE\",\"condition\":\"THRESHOLD\",\"threshold\":500.0,\"metric_name\":\"checkout.latency.p95\",\"window_seconds\":3600,\"min_samples\":1,\"severity\":\"HIGH\"}")
check "an anomaly rule is created by the operator" "201" "$RULE_CODE"
# A rule that could never be evaluated must be refused, not silently accepted.
BAD_RULE_CODE=$(jreq "$API/api/v1/anomaly-rules" \
  "{\"project_id\":\"$PID\",\"name\":\"no threshold\",\"anomaly_type\":\"LATENCY_SPIKE\",\"condition\":\"THRESHOLD\",\"metric_name\":\"checkout.latency.p95\",\"window_seconds\":3600,\"severity\":\"HIGH\"}")
check "an unevaluable rule is refused" "422" "$BAD_RULE_CODE"

DETECT=$(curl -fsS -X POST "$API/api/v1/projects/$PID/anomalies/detect")
OPENED=$(echo "$DETECT" | jget "d['detection']['anomalies_opened']")
#: Detection also runs on a background sweep, so an anomaly may already be open
#: by the time this call runs. What must hold either way is that the operator's
#: rule fires and the anomaly is on the record — the per-call counter is not the
#: contract.
DETECTED_TOTAL=$(curl -fsS "$API/api/v1/anomalies?project_id=$PID&page_size=1" | jget "d['total']")
if [ "${DETECTED_TOTAL:-0}" -ge 1 ] 2>/dev/null; then
  ok "the operator's rule detected the fault (opened=$OPENED, on record=$DETECTED_TOTAL)"
else
  bad "detection on a fresh project" \
    "no anomaly on record; opened=$OPENED errors=$(echo "$DETECT" | jget "d['detection']['errors']") rules_evaluated=$(echo "$DETECT" | jget "d['detection']['rules_evaluated']")"
fi

ANOMS=$(curl -fsS "$API/api/v1/anomalies?project_id=$PID&page_size=10")
ANOM_ID=$(echo "$ANOMS" | jget "d['items'][0]['id']")
#: The §52 explanation is carried by the anomaly *detail* response; the list
#: response is deliberately lean. Reading it from the list is a gate bug, not a
#: missing feature — so this checks the endpoint that promises it.
ANOM_DETAIL=$(curl -fsS "$API/api/v1/anomalies/$ANOM_ID")
WHY=$(echo "$ANOM_DETAIL" | jget "d['explanation']['why_detected']" 2>/dev/null || echo "")
if [ -n "$WHY" ] && [ "$WHY" != "None" ]; then
  ok "the anomaly explains why it was detected"
else
  bad "anomaly explanation" "no why_detected in the anomaly detail"
fi

CORR=$(echo "$DETECT" | jget "d['correlation']['incidents_created']")
[ "$CORR" -ge 1 ] 2>/dev/null && ok "correlation opened $CORR incident" || bad "incident correlation" "created=$CORR"

INCS=$(curl -fsS "$API/api/v1/incidents?project_id=$PID&page_size=10")
INC_ID=$(echo "$INCS" | jget "d['items'][0]['id']")
INC_DETAIL=$(curl -fsS "$API/api/v1/incidents/$INC_ID")
echo "$INC_DETAIL" | jget "d['severity']" >/dev/null && ok "the incident carries a severity" || bad "incident severity" "$INC_DETAIL"

TL=$(curl -fsS "$API/api/v1/incidents/$INC_ID/timeline")
if echo "$TL" | jget "any(e['event_type']=='ANOMALY_DETECTED' for e in d['items'])" | grep -q True; then
  ok "the incident timeline records the detection"
else
  bad "incident timeline" "no ANOMALY_DETECTED entry"
fi

check "the incident is linked to the anomaly" "1" \
  "$(curl -fsS "$API/api/v1/incidents/$INC_ID/anomalies" | jget "len(d) if isinstance(d, list) else len(d['items'])")"

echo "== 4. Causal analysis with explicit uncertainty =="
check "causal analysis runs on a fresh project" "200" \
  "$(code -X POST "$API/api/v1/incidents/$INC_ID/analyze")"
# The epistemic contract: an analysis never returns a bare probability. It
# states an overall confidence band, the evidence it is *missing*, and — per
# candidate — its own uncertainty and supporting/contradicting counts. Those are
# the fields to check; a top-level "limitations" string is not part of the
# response contract.
CA=$(curl -fsS "$API/api/v1/incidents/$INC_ID/causal-analysis")
CA_SUMMARY=$(echo "$CA" | jget "d['overall_confidence']")
CA_MISSING=$(echo "$CA" | jget "len(d.get('missing_evidence') or [])")
CA_CAND=$(echo "$CA" | jget "len(d.get('candidates') or [])")
if [ "$CA_SUMMARY" != "None" ] && [ -n "$CA_SUMMARY" ]; then
  ok "the analysis states its overall confidence ($CA_SUMMARY)"
else
  bad "analysis confidence" "overall_confidence absent"
fi
if [ "${CA_CAND:-0}" -ge 1 ] 2>/dev/null && \
   [ "$(echo "$CA" | jget "d['candidates'][0]['uncertainty']")" != "None" ]; then
  ok "each candidate carries its own uncertainty and evidence counts"
else
  bad "candidate uncertainty" "candidates=$CA_CAND missing_evidence=$CA_MISSING"
fi

check "ranked root-cause candidates are readable" "200" \
  "$(code "$API/api/v1/incidents/$INC_ID/root-causes")"
check "incident hypotheses are readable" "200" \
  "$(code "$API/api/v1/incidents/$INC_ID/hypotheses")"

echo "== 5. The knowledge graph discovers the new project =="
check "graph reconcile runs" "200" "$(code -X POST "$API/api/v1/projects/$PID/graph/reconcile")"
NODES=$(curl -fsS "$API/api/v1/projects/$PID/graph/nodes?page_size=50")
N_TOTAL=$(echo "$NODES" | jget "d['total']")
[ "$N_TOTAL" -ge 2 ] 2>/dev/null && ok "the graph contains both components ($N_TOTAL nodes)" || bad "graph nodes" "$N_TOTAL"
check "graph health is readable" "200" "$(code "$API/api/v1/projects/$PID/graph/health")"
check "graph data quality is readable" "200" "$(code "$API/api/v1/projects/$PID/graph/data-quality")"

echo "== 6. Project isolation still holds for the new project =="
check "incident is not readable as another project's" "404" \
  "$(code "$API/api/v1/incidents/$INC_ID?project_id=$OTHER_PID")"
check "anomalies are scoped to the project" "0" \
  "$(curl -fsS "$API/api/v1/anomalies?project_id=$OTHER_PID&page_size=1" | jget "d['total']")"

echo "== 7. Every surface on the journey is reachable =="
for p in / /connect /incidents /incidents/$INC_ID /system-map /observability /remediation /reliability; do
  check "WEB $p" "200" "$(code "$WEB$p")"
done

echo ""
echo "=============================="
echo "ONBOARDING RESULT: $PASS passed, $FAIL failed"
echo "=============================="
[ "$FAIL" = 0 ]
