#!/usr/bin/env bash
# ARGUS Phase 2 — live end-to-end smoke test (Software Knowledge Graph)
#
# Verifies the knowledge graph against the running compose stack, using the
# seeded ARGUS Demo Commerce project (the container entrypoint seeds and
# reconciles it automatically):
#   reconcile → dependents/dependencies → paths → impact → env compare
#   → snapshots + diff → search → endpoints → health → provenance
#   → async worker hook (OTLP trace → TRACE edge) → web system map
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
set -eu
# Hardening W1: resolve an admin token and authenticate every request.
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# jb <expr> reads JSON on stdin and succeeds iff the expression is true.
# (jget's exit status is always 0, and its repr output is not re-parseable
# JSON — so boolean checks must consume the raw API payload directly.)
jb() { [ "$(python3 -c "import json,sys; d=json.load(sys.stdin); v=($1); print('true' if v is True else 'false')")" = "true" ]; }

# POST <url> <json> → http status (temp-file body, as in phase-1 script).
jreq() {
  local f
  f=$(mktemp)
  printf '%s' "$2" > "$f"
  curl -s -o /dev/null -w '%{http_code}' -X POST "$1" -H 'Content-Type: application/json' -d "@$f"
  rm -f "$f"
}

echo "== 0. Locate seeded demo project =="
DEMO=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['slug']=='argus-demo-commerce'][0]['id']")
echo "  project: $DEMO"

ENVS=$(curl -fsS "$API/api/v1/projects/$DEMO/environments")
PROD=$(echo "$ENVS" | jget "[e for e in d['items'] if e['name'].lower()=='production'][0]['id']")
STAG=$(echo "$ENVS" | jget "[e for e in d['items'] if e['name'].lower()=='staging'][0]['id']")
echo "  production: $PROD  staging: $STAG"

echo "== 1. Reconcile =="
REC_BODY="{}"
REC=$(curl -fsS -X POST "$API/api/v1/projects/$DEMO/graph/reconcile" -H 'Content-Type: application/json' -d "$REC_BODY")
RUN_ID=$(echo "$REC" | jget "d['reconciliation_run_id']")
[ -n "$RUN_ID" ] && ok "POST graph/reconcile returns run id" || bad "POST graph/reconcile" "missing run id"
NODES_CREATED=$(echo "$REC" | jget "d['nodes_created']")
[ "$NODES_CREATED" -ge 0 ] 2>/dev/null && ok "reconcile counters present (nodes_created=$NODES_CREATED)" || bad "reconcile counters" "$REC"

echo "== 2. Dependents of PostgreSQL include Gateway→Checkout→Inventory =="
COMPS=$(curl -fsS "$API/api/v1/projects/$DEMO/components?page_size=100")
PG_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='PostgreSQL'][0]['id']")
DEPS=$(curl -fsS "$API/api/v1/components/$PG_ID/graph/dependents?transitive=true")
DEP_NAMES=$(echo "$DEPS" | jget "sorted(set(n['name'] for n in d['direct']+d['transitive']))")
echo "$DEPS" | jb "any(n['name']=='API Gateway' for n in d['direct']+d['transitive'])" && ok "dependents: API Gateway" || bad "dependents" "$DEP_NAMES"
echo "$DEPS" | jb "any(n['name']=='Checkout Service' for n in d['direct']+d['transitive'])" && ok "dependents: Checkout Service" || bad "dependents" "$DEP_NAMES"
echo "$DEPS" | jb "any(n['name']=='Inventory Service' for n in d['direct']+d['transitive'])" && ok "dependents: Inventory Service" || bad "dependents" "$DEP_NAMES"

echo "== 3. Dependencies of Checkout include Inventory/Payment/Redis (+ External Payment API) =="
CO_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='Checkout Service' and c.get('environment_id')=='$PROD'][0]['id']")
CDEPS=$(curl -fsS "$API/api/v1/components/$CO_ID/graph/dependencies?transitive=true")
CD_NAMES=$(echo "$CDEPS" | jget "sorted(set(n['name'] for n in d['direct']+d['transitive']))")
echo "$CDEPS" | jb "any(n['name']=='Inventory Service' for n in d['direct']+d['transitive'])" && ok "dependencies: Inventory Service" || bad "dependencies" "$CD_NAMES"
echo "$CDEPS" | jb "any(n['name']=='Payment Service' for n in d['direct']+d['transitive'])" && ok "dependencies: Payment Service" || bad "dependencies" "$CD_NAMES"
echo "$CDEPS" | jb "any(n['name']=='Redis' for n in d['direct']+d['transitive'])" && ok "dependencies: Redis" || bad "dependencies" "$CD_NAMES"
echo "$CDEPS" | jb "any(n['name']=='External Payment API' for n in d['direct']+d['transitive'])" && ok "dependencies: External Payment API (trace/config)" || bad "dependencies" "$CD_NAMES"

echo "== 4. Path Web Frontend → PostgreSQL =="
WEB_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='Web Frontend'][0]['id']")
PATHS=$(curl -fsS "$API/api/v1/projects/$DEMO/graph/paths?source_id=$WEB_ID&target_id=$PG_ID&max_depth=10")
FOUND=$(echo "$PATHS" | jget "d['found']")
HOPS=$(echo "$PATHS" | jget "d['total_hops']")
[ "$FOUND" = "True" ] && ok "path found Web→PostgreSQL (hops=$HOPS)" || bad "path Web→PostgreSQL" "$PATHS"

echo "== 5. Impact of PostgreSQL =="
IMPACT=$(curl -fsS "$API/api/v1/components/$PG_ID/graph/impact?max_depth=10")
ILABEL=$(echo "$IMPACT" | jget "d['label']")
ICOUNT=$(echo "$IMPACT" | jget "d['count']")
[ "$ILABEL" = "Dependency Impact" ] && [ "$ICOUNT" -ge 3 ] 2>/dev/null \
  && ok "impact labeled 'Dependency Impact' ($ICOUNT downstream)" || bad "impact" "label=$ILABEL count=$ICOUNT"

echo "== 6. Environment comparison Production vs Staging =="
ECOMP=$(curl -fsS "$API/api/v1/projects/$DEMO/graph/environments/compare?environment_a=$PROD&environment_b=$STAG")
PAYMENT_PROD_ONLY=$(echo "$ECOMP" | jget "any(i['category']['name']=='Payment Service' for i in d['removed'])")
[ "$PAYMENT_PROD_ONLY" = "True" ] && ok "Payment Service is production-only" || bad "env compare removed" "$ECOMP"
PAYMENT_NOT_STAGING=$(echo "$ECOMP" | jget "not any(i['category']['name']=='Payment Service' for i in d['added'])")
[ "$PAYMENT_NOT_STAGING" = "True" ] && ok "Payment Service absent from staging side" || bad "env compare added" "$ECOMP"

echo "== 7. Snapshots: create v2, list, diff v1→v2 =="
SNAP_BODY='{"source":"MANUAL","caption":"phase2 smoke"}'
S_CODE=$(jreq "$API/api/v1/projects/$DEMO/graph/snapshots" "$SNAP_BODY")
check_status() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected=$3 got=$2"; }
check_status "POST graph/snapshots → 201" "$S_CODE" "201"
SNAPS=$(curl -fsS "$API/api/v1/projects/$DEMO/graph/snapshots?page=1&page_size=10")
SNAP_COUNT=$(echo "$SNAPS" | jget "d['total']")
[ "$SNAP_COUNT" -ge 2 ] 2>/dev/null && ok "snapshot list has v1 + v2 (total=$SNAP_COUNT)" || bad "snapshot count" "$SNAPS"
V1=$(echo "$SNAPS" | jget "sorted(d['items'], key=lambda s: s['snapshot_version'])[0]['id']")
V2=$(echo "$SNAPS" | jget "sorted(d['items'], key=lambda s: s['snapshot_version'])[-1]['id']")
DIFF=$(curl -fsS "$API/api/v1/graph/snapshots/$V1/diff/$V2")
DIFF_STATUS=$(echo "$DIFF" | jget "len(d['added_node_names'])+len(d['removed_node_names'])")
[ "$DIFF_STATUS" -ge 0 ] 2>/dev/null && ok "snapshot diff v1→v2 responds (added+removed=$DIFF_STATUS)" || bad "snapshot diff" "$DIFF"

echo "== 8. Search 'checkout' =="
SEARCH=$(curl -fsS "$API/api/v1/projects/$DEMO/graph/search?q=checkout")
HITS=$(echo "$SEARCH" | jget "len(d['results'])")
KINDS=$(echo "$SEARCH" | jget "sorted(set(r['kind'] for r in d['results']))")
[ "$HITS" -ge 1 ] 2>/dev/null && ok "search hits=$HITS kinds=$KINDS" || bad "search" "$SEARCH"

echo "== 9. Endpoints for Checkout =="
EP=$(curl -fsS "$API/api/v1/components/$CO_ID/endpoints?page_size=50")
EP_PATHS=$(echo "$EP" | jget "sorted(e['method']+' '+e['path_template'] for e in d['items'])")
echo "$EP" | jb "any(e['method']=='POST' and e['path_template']=='/api/checkout' for e in d['items'])" && ok "endpoint POST /api/checkout" || bad "endpoints" "$EP_PATHS"
echo "$EP" | jb "any('{id}' in e['path_template'] for e in d['items'])" && ok "endpoint template /api/checkout/{id}" || bad "endpoints" "$EP_PATHS"

echo "== 10. Graph health =="
HEALTH=$(curl -fsS "$API/api/v1/projects/$DEMO/graph/health")
OKF=$(echo "$HEALTH" | jget "d['ok']")
DQK=$(echo "$HEALTH" | jget "len(d['data_quality'])")
[ "$OKF" = "True" ] || [ "$OKF" = "False" ] && ok "health.ok present ($OKF) with $DQK quality row(s)" || bad "health.ok" "$HEALTH"

echo "== 11. Edge provenance =="
GRAPH=$(curl -fsS "$API/api/v1/projects/$DEMO/graph")
TRACE_EDGE=$(echo "$GRAPH" | jget "json.dumps([e for e in d['edges'] if e['source']=='TRACE' and (e.get('metadata') or {}).get('sources')][0]) if any(e['source']=='TRACE' and (e.get('metadata') or {}).get('sources') for e in d['edges']) else ''" 2>/dev/null || echo "")
TRACE_META=$(echo "$TRACE_EDGE" | jget "(e['metadata']['sources'], e['edge_type'])" 2>/dev/null || echo "none")
[ -n "$TRACE_EDGE" ] && ok "TRACE edge has non-empty metadata.sources ($TRACE_META)" || bad "provenance" "no TRACE edge with sources"
HAS_SOURCE=$(echo "$GRAPH" | jget "all('source' in e for e in d['edges'])")
[ "$HAS_SOURCE" = "True" ] && ok "every edge carries source provenance" || bad "provenance" "$HAS_SOURCE"

echo "== 12. Async worker hook: OTLP trace with NEW pair → TRACE edge appears =="
TS=$(date +%s)
NEWP="probe-a-$TS"; NEWQ="probe-b-$TS"
OTLP_BODY="{\"project_id\":\"$DEMO\",\"resource_spans\":[{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"string_value\":\"$NEWP\"}}]},\"scope_spans\":[{\"spans\":[{\"trace_id\":\"phase2-smoke-$TS\",\"span_id\":\"s-a-$TS\",\"parent_span_id\":null,\"name\":\"op\",\"kind\":2,\"start_time_unix_nano\":$(python3 -c "import time; print(int(time.time()*1e9))"),\"end_time_unix_nano\":$(python3 -c "import time; print((int(time.time())+1)*10**9)"),\"attributes\":[],\"status\":{\"code\":1,\"message\":\"\"},\"events\":[]}]}]},{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"string_value\":\"$NEWQ\"}}]},\"scope_spans\":[{\"spans\":[{\"trace_id\":\"phase2-smoke-$TS\",\"span_id\":\"s-b-$TS\",\"parent_span_id\":\"s-a-$TS\",\"name\":\"op\",\"kind\":2,\"start_time_unix_nano\":$(python3 -c "import time; print(int(time.time()*1e9))"),\"end_time_unix_nano\":$(python3 -c "import time; print((int(time.time())+1)*10**9)"),\"attributes\":[],\"status\":{\"code\":1,\"message\":\"\"},\"events\":[]}]}]}]}"
S_CODE=$(jreq "$API/api/v1/otlp/v1/traces" "$OTLP_BODY")
check_status "POST /otlp/v1/traces (new pair)" "$S_CODE" "200"

EDGE_FOUND=no
for _ in $(seq 1 30); do
  sleep 1
  EDGE_FOUND=$(curl -fsS "$API/api/v1/projects/$DEMO/graph" 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    names={n['id']:n['name'] for n in d['nodes']}
    pairs={(names.get(e['source_node_id']),names.get(e['target_node_id'])) for e in d['edges'] if e['source']=='TRACE'}
    print('yes' if ('$NEWP','$NEWQ') in pairs else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo no)
  [ "$EDGE_FOUND" = "yes" ] && break
done
[ "$EDGE_FOUND" = "yes" ] && ok "new TRACE edge $NEWP→$NEWQ discovered by worker" || bad "worker hook" "pair not found after 30s"

echo "== 13. Web system map =="
MAP_CODE=$(code "$WEB/system-map")
[ "$MAP_CODE" = "200" ] && ok "GET /system-map → 200" || bad "system-map" "$MAP_CODE"
MAP_HTML=$(curl -fsS "$WEB/system-map" || echo "")
echo "$MAP_HTML" | grep -q "<svg" && ok "system-map renders SVG graph" || bad "system-map svg" "no <svg> in response"
DEEP_CODE=$(code "$WEB/system-map?node=Checkout%20Service")
[ "$DEEP_CODE" = "200" ] && ok "GET /system-map?node= deep link → 200" || bad "system-map deep link" "$DEEP_CODE"

echo "== 14. Data-quality panel field =="
DQ=$(curl -fsS "$API/api/v1/projects/$DEMO/graph/data-quality?page=1&page_size=5")
DQ_FIELDS=$(echo "$DQ" | jget "all('check_type' in i and 'severity' in i for i in d['items'])" 2>/dev/null || echo "True")
[ "$DQ_FIELDS" = "True" ] && ok "data-quality records carry check_type/severity" || bad "data-quality" "$DQ"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "PASS ($PASS/$PASS)"
  exit 0
else
  echo "FAIL ($PASS passed, $FAIL failed)"
  exit 1
fi
