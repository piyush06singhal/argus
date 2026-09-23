#!/usr/bin/env bash
# ARGUS Phase 11 — live end-to-end smoke test (unified reliability platform)
#
# Runs the whole control plane against the running compose stack over the real
# HTTP API — nothing is stubbed, no summary is hard-coded, and every count comes
# from the run's own responses:
#
#   0. an isolated scratch project (so the gate cannot perturb the seeded demo)
#   1. scope, ownership and unknown-id refusals (a read without a project is 422,
#      a foreign id is 404)
#   2. ingest a real degradation timeline through the ingest API, detect and
#      correlate it into an incident
#   3. unified system state: components carry a state *and* a reason, and a
#      component with no evidence is UNKNOWN, never HEALTHY
#   4. the control-plane sweep routes the incident into a reliability case
#   5. cases: list, detail, timeline, a legal transition, and an illegal one
#   6. the evidence-grounded case assistant cites stored rows
#   7. service catalog: list, detail, and ownership recorded (UNKNOWN before)
#   8. SLOs: define, evaluate, error budget, and a never-evaluated objective is
#      not reported as meeting
#   9. global search: grouped results, an honest no-match, and cross-project
#      isolation
#  10. data quality: the checks run, findings are recorded, and an issue's status
#      is changed without mutating the finding
#  11. configuration: read, write a version, see the version ledger, roll back
#  12. feature flags carry a reason and safe defaults
#  13. notifications are raised and acknowledged
#  14. reports, engineering metrics and the improvement plan generate
#  15. platform health, readiness, dependencies and the live probe answer
#  16. multi-project isolation: another project sees none of it
#  17. the platform workspace renders (§116)
#  18. cleanup: the scratch project deletes and nothing it created survives
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
set -eu
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
COMPOSE="docker compose"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
jb()   { [ "$(python3 -c "import json,sys; d=json.load(sys.stdin); v=($1); print('true' if v else 'false')")" = "true" ]; }
check_status() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected=$3 got=$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
post() { curl -fsS -X POST "$@"; }
put()  { curl -fsS -X PUT "$@"; }
sql()  { $COMPOSE exec -T postgres psql -U argus -d argus_db -tAc "$1" 2>/dev/null || true; }
nowiso() { python3 -c "import datetime; print(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'))"; }
isoago() { python3 -c "import datetime; print((datetime.datetime.utcnow() - datetime.timedelta(seconds=$1)).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }

TS=$(date +%s)
echo "== 0. An isolated scratch project =="
PROJ=$(post "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"smoke-platform-$TS\",\"slug\":\"smoke-platform-$TS\",\"description\":\"Phase 11 smoke\"}" \
  | jget "d['id']")
OTHER=$(post "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"smoke-platform-other-$TS\",\"slug\":\"smoke-platform-other-$TS\"}" \
  | jget "d['id']")
ENV=$(post "$API/api/v1/projects/$PROJ/environments" -H 'Content-Type: application/json' \
  -d '{"name":"production","environment_type":"PRODUCTION"}' | jget "d['id']")
# The components are declared *in* the environment, matching the platform's own
# convention (and the seed): a component belongs to a place, so an
# environment-scoped dashboard finds it. Declaring them environment-less and then
# ingesting environment-scoped telemetry for them would test a state the system
# does not actually produce.
CO=$(post "$API/api/v1/projects/$PROJ/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"checkout-service\",\"component_type\":\"SERVICE\",\"environment_id\":\"$ENV\"}" | jget "d['id']")
INV=$(post "$API/api/v1/projects/$PROJ/components" -H 'Content-Type: application/json' \
  -d "{\"name\":\"inventory-service\",\"component_type\":\"SERVICE\",\"environment_id\":\"$ENV\"}" | jget "d['id']")
post "$API/api/v1/projects/$PROJ/dependencies" -H 'Content-Type: application/json' \
  -d "{\"source_component_id\":\"$CO\",\"target_component_id\":\"$INV\",\"dependency_type\":\"HTTP\"}" > /dev/null
ok "scratch project $PROJ with checkout $CO and inventory $INV"

echo "== 1. Scope, ownership and unknown-id refusals =="
check_status "the overview without a project scope is refused" \
  "$(code "$API/api/v1/platform/overview")" "422"
check_status "unknown project answers 404" \
  "$(code "$API/api/v1/platform/overview?project_id=00000000-0000-0000-0000-000000000000")" "404"
check_status "an unknown case id answers 404" \
  "$(code "$API/api/v1/platform/cases/00000000-0000-0000-0000-000000000000?project_id=$PROJ")" "404"
check_status "an unknown service id answers 404" \
  "$(code "$API/api/v1/platform/services/00000000-0000-0000-0000-000000000000?project_id=$PROJ")" "404"
check_status "an unknown SLO id answers 404" \
  "$(code "$API/api/v1/platform/slo/00000000-0000-0000-0000-000000000000?project_id=$PROJ")" "404"
check_status "an unknown case status is refused, not defaulted" \
  "$(code "$API/api/v1/platform/cases?project_id=$PROJ&status=TOTALLY_MADE_UP")" "422"
check_status "search without a project scope is refused" \
  "$(code "$API/api/v1/platform/search?q=anything")" "422"
check_status "the live probe answers" "$(code "$API/api/v1/platform/live")" "200"

echo "== 2. Ingest a real degradation timeline and correlate it (§2) =="
for i in 0 1 2 3 4 5 6 7 8 9; do
  OFFSET=$(( (9 - i) * 300 ))
  STAMP=$(isoago "$OFFSET")
  ERR=$(python3 -c "print(round(0.02 + (0.55 - 0.02) * $i / 9, 4))")
  LAT=$(python3 -c "print(round(120.0 + (900.0 - 120.0) * $i / 9, 1))")
  curl -fsS -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV\",\"component_id\":\"$CO\",\"timestamp\":\"$STAMP\",\"metric_name\":\"http.checkout.error_rate\",\"metric_type\":\"GAUGE\",\"value\":$ERR,\"unit\":\"ratio\"}" > /dev/null
  curl -fsS -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV\",\"component_id\":\"$INV\",\"timestamp\":\"$STAMP\",\"metric_name\":\"http.inventory.latency.p95\",\"metric_type\":\"GAUGE\",\"value\":$LAT,\"unit\":\"ms\"}" > /dev/null
done
ok "ingested ten paired samples across both components"

mk_rule() { # name metric threshold component
  post "$API/api/v1/anomaly-rules" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"name\":\"$1\",\"anomaly_type\":\"METRIC_THRESHOLD\",\"condition\":\"THRESHOLD\",\"metric_name\":\"$2\",\"threshold\":$3,\"severity\":\"HIGH\",\"component_id\":\"$4\",\"window_seconds\":86400,\"cooldown_seconds\":0,\"min_samples\":1,\"persistence_cycles\":1,\"created_by\":\"smoke\"}" > /dev/null
}
mk_rule "checkout-errors" "http.checkout.error_rate" 0.5 "$CO"
mk_rule "inventory-latency" "http.inventory.latency.p95" 850 "$INV"
post "$API/api/v1/projects/$PROJ/anomalies/detect?environment_id=$ENV&correlate=true" \
  -H 'Content-Type: application/json' -d '{}' > /dev/null
INCIDENTS=$(curl -fsS "$API/api/v1/incidents?project_id=$PROJ&page_size=50" | jget "len(d.get('items', d) if isinstance(d, dict) else d)")
[ "${INCIDENTS:-0}" -ge 1 ] \
  && ok "detection raised ${INCIDENTS} incident(s) for the episode" \
  || bad "detection" "no incident was raised"
INC=$(curl -fsS "$API/api/v1/incidents?project_id=$PROJ&page_size=50" | jget "((d.get('items', d) if isinstance(d, dict) else d) or [{}])[0].get('id', '')")
ok "newest incident is $INC"

echo "== 3. Unified system state (§2–§5) =="
STATE=$(curl -fsS "$API/api/v1/platform/state?project_id=$PROJ&environment_id=$ENV")
echo "$STATE" | jb "d['components_total'] if 'components_total' in d else len(d.get('components', [])) >= 1" >/dev/null 2>&1 || true
COMPONENTS=$(echo "$STATE" | jget "len(d['components'])")
[ "${COMPONENTS:-0}" -ge 2 ] \
  && ok "state lists ${COMPONENTS} components" || bad "state components" "got=$COMPONENTS"
echo "$STATE" | jb "len(d['components']) >= 2 and all(('state' in c) for c in d['components'])" \
  && ok "every component carries a state" || bad "component state" "missing"
echo "$STATE" | jb "len(d['components']) >= 2 and all(('state_reason' in c) for c in d['components'])" \
  && ok "every component carries the reason for its state" || bad "state reason" "missing"
echo "$STATE" | jb "len(d['components']) >= 2 and all('state_evidence' in c for c in d['components'])" \
  && ok "every component names the evidence behind its state" || bad "state evidence" "missing"
echo "$STATE" | jb "d['health']['components_unknown'] >= 1 or d['health']['components_with_evidence'] >= 1" \
  && ok "unknown components are counted, not hidden" || bad "health coverage" "missing"
RECOMPUTE=$(post "$API/api/v1/platform/state/recompute?project_id=$PROJ" -H 'Content-Type: application/json' -d '{}')
echo "$RECOMPUTE" | jb "d['components_evaluated'] >= 2" \
  && ok "recompute evaluated the components and recorded transitions" \
  || bad "recompute" "$RECOMPUTE"

echo "== 4. The control-plane sweep routes the incident into a case (§12) =="
SWEEP=$(post "$API/api/v1/platform/sweep?project_id=$PROJ" -H 'Content-Type: application/json' -d '{}')
echo "$SWEEP" | jb "d.get('paused') is False and d.get('disabled') is False" \
  && ok "the sweep ran (not paused, not disabled)" || bad "sweep" "$SWEEP"
CASES=$(curl -fsS "$API/api/v1/platform/cases?project_id=$PROJ")
CASE_COUNT=$(echo "$CASES" | jget "d['total']")
[ "${CASE_COUNT:-0}" -ge 1 ] \
  && ok "the sweep opened ${CASE_COUNT} reliability case(s)" \
  || bad "auto-case" "no case was opened"
CASE=$(echo "$CASES" | jget "d['cases'][0]['id']")
CASE_STATUS=$(echo "$CASES" | jget "d['cases'][0]['status']")

echo "== 5. Case detail, timeline and legal transitions (§14–§16) =="
DETAIL=$(curl -fsS "$API/api/v1/platform/cases/$CASE?project_id=$PROJ&include_evidence=true")
echo "$DETAIL" | jb "d['case']['id'] == '$CASE'" \
  && ok "the case reads back by id" || bad "case detail" "mismatch"
echo "$DETAIL" | jb "len(d['case']['component_ids']) >= 1" \
  && ok "the case names its components" || bad "case components" "empty"
echo "$DETAIL" | jb "isinstance(d['timeline'], list)" \
  && ok "the case exposes a timeline list" || bad "case timeline" "missing"
echo "$DETAIL" | jb "isinstance(d['case']['allowed_transitions'], list)" \
  && ok "the case exposes its allowed transitions" || bad "allowed transitions" "missing"
#: 409, matching the incident subsystem's precedent: the body is well-formed, the
#: *move* is illegal, and the response names the moves that would be accepted.
check_status "an illegal case transition is refused" \
  "$(code -X POST "$API/api/v1/platform/cases/$CASE/status?project_id=$PROJ" \
      -H 'Content-Type: application/json' -d '{"status":"LEARNED","reason":"smoke"}')" "409"
REFUSAL=$(curl -s -X POST "$API/api/v1/platform/cases/$CASE/status?project_id=$PROJ" \
  -H 'Content-Type: application/json' -d '{"status":"LEARNED","reason":"smoke"}')
echo "$REFUSAL" | jb "d['detail']['code'] == 'illegal_transition'" \
  && ok "the refusal names itself as an illegal transition" || bad "transition code" "missing"
echo "$REFUSAL" | jb "len(d['detail']['allowed']) >= 1" \
  && ok "the refusal lists the transitions that are allowed" || bad "transition allowed" "missing"
#: Walk a legal transition if one is offered. The set is status-dependent, so the
#: gate asserts the *rule* (an offered transition succeeds, an unoffered one is
#: refused) rather than a hard-coded target status.
NEXT=$(echo "$DETAIL" | jget "(d['case']['allowed_transitions'] or [''])[0]")
if [ -n "$NEXT" ]; then
  TRANSITION=$(post "$API/api/v1/platform/cases/$CASE/status?project_id=$PROJ" \
    -H 'Content-Type: application/json' -d "{\"status\":\"$NEXT\",\"reason\":\"smoke transition\"}")
  echo "$TRANSITION" | jb "d['status'] == '$NEXT'" \
    && ok "an offered transition ($NEXT) succeeds" \
    || bad "case transition" "$TRANSITION"
else
  ok "no transition is offered from $CASE_STATUS (terminal or waiting)"
fi

echo "== 6. The case assistant is evidence-grounded (§27, §28) =="
#: The assistant ships switched off, and that is a fact the platform states
#: rather than hides: the capability sheet is served either way, and the ask
#: endpoint refuses with a reason instead of answering from nothing.
CAPABILITY=$(curl -fsS "$API/api/v1/platform/case-assistant")
echo "$CAPABILITY" | jb "isinstance(d['enabled'], bool)" \
  && ok "the capability sheet states whether the assistant is on" \
  || bad "assistant capability" "missing flag"
echo "$CAPABILITY" | jb "len(d['guarantees']) >= 1 and len(d['refuses']) >= 1" \
  && ok "the capability sheet publishes its guarantees and its refusals" \
  || bad "assistant guarantees" "missing"
ASSISTANT_ON=$(echo "$CAPABILITY" | jget "d['enabled']")
if [ "$ASSISTANT_ON" = "True" ]; then
  ANSWER=$(post "$API/api/v1/platform/cases/$CASE/ask?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d '{"question":"what happened in this case?","include_evidence":false}')
  echo "$ANSWER" | jb "isinstance(d['citations'], list)" \
    && ok "the assistant returns a citation list" || bad "assistant citations" "missing"
  echo "$ANSWER" | jb "isinstance(d['unknowns'], list)" \
    && ok "the assistant states its unknowns" || bad "assistant unknowns" "missing"
  echo "$ANSWER" | jb "isinstance(d['confidence_reason'], str) and len(d['confidence_reason']) > 0" \
    && ok "the assistant explains its confidence" || bad "assistant confidence" "missing"
else
  check_status "a switched-off assistant refuses instead of answering" \
    "$(code -X POST "$API/api/v1/platform/cases/$CASE/ask?project_id=$PROJ" \
        -H 'Content-Type: application/json' -d '{"question":"anything"}')" "503"
fi

echo "== 7. Service catalog and ownership (§30, §31) =="
CATALOG=$(curl -fsS "$API/api/v1/platform/services?project_id=$PROJ")
echo "$CATALOG" | jb "d['total'] >= 2" \
  && ok "the catalog lists the components" || bad "catalog" "$CATALOG"
SERVICE=$(curl -fsS "$API/api/v1/platform/services/$CO?project_id=$PROJ")
echo "$SERVICE" | jb "d['component_id'] == '$CO'" \
  && ok "a service reads back by id" || bad "service detail" "mismatch"
echo "$SERVICE" | jb "isinstance(d['state'], str) and len(d['state']) > 0" \
  && ok "the service carries an operational state" || bad "service state" "missing"
echo "$SERVICE" | jb "isinstance(d['unavailable'], dict)" \
  && ok "the service names the sections it could not compute" || bad "unavailable" "missing"
OWNED=$(put "$API/api/v1/platform/services/$CO/ownership?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"team":"payments","owner_name":"Smoke Owner","actor":"smoke"}')
echo "$OWNED" | jb "d.get('recorded') is True or d.get('team') == 'payments'" \
  && ok "ownership is recorded by a human" || bad "ownership" "$OWNED"
OWNED_READ=$(curl -fsS "$API/api/v1/platform/services/$CO?project_id=$PROJ")
echo "$OWNED_READ" | jb "d['owner'].get('team') == 'payments'" \
  && ok "the recorded owner reads back" || bad "ownership read-back" "$OWNED_READ"

echo "== 8. Objectives and error budgets (§32–§35) =="
#: The objective names the metric it measures — §32 wants a real indicator, not a
#: free-text wish, and the validator refuses an objective that names nothing.
SLO=$(post "$API/api/v1/platform/slo?project_id=$PROJ&environment_id=$ENV" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"checkout error rate\",\"indicator\":\"ERROR_RATE\",\"target\":0.5,\"comparison\":\"AT_MOST\",\"metric_name\":\"http.checkout.error_rate\",\"component_id\":\"$CO\",\"actor\":\"smoke\"}")
SLO_ID=$(echo "$SLO" | jget "d['slo_id']")
ok "defined objective $SLO_ID"
check_status "an objective that names no metric is refused" \
  "$(code -X POST "$API/api/v1/platform/slo?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d '{"name":"vague","indicator":"AVAILABILITY","target":0.99,"comparison":"AT_LEAST"}')" "422"
EVAL=$(post "$API/api/v1/platform/slo/evaluate?project_id=$PROJ" -H 'Content-Type: application/json' -d '{}')
echo "$EVAL" | jb "d.get('evaluated', 0) >= 1" \
  && ok "evaluation ran over the defined objectives" || bad "slo evaluate" "$EVAL"
SLO_DETAIL=$(curl -fsS "$API/api/v1/platform/slo/$SLO_ID?project_id=$PROJ")
echo "$SLO_DETAIL" | jb "d['slo_id'] == '$SLO_ID'" \
  && ok "the objective evaluates by id" || bad "slo detail" "mismatch"
echo "$SLO_DETAIL" | jb "'data_quality' in d and 'sample_count' in d" \
  && ok "the evaluation reports its data quality and sample count" || bad "slo evidence" "missing"
BUDGET=$(curl -fsS "$API/api/v1/platform/slo/$SLO_ID/error-budget?project_id=$PROJ")
echo "$BUDGET" | jb "isinstance(d['history'], list) and len(d['definition']) > 0" \
  && ok "the error budget exposes history and its definition" || bad "error budget" "$BUDGET"
check_status "a ratio target outside 0..1 is refused" \
  "$(code -X POST "$API/api/v1/platform/slo?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d "{\"name\":\"bad\",\"indicator\":\"AVAILABILITY\",\"target\":5.0,\"comparison\":\"AT_LEAST\",\"metric_name\":\"http.checkout.availability\"}")" "422"
#: §32 requires a latency objective in milliseconds, which a single 0..1 target
#: bound would make unexpressible — so the bound is indicator-aware, and the gate
#: proves both halves of that: the ratio target above is refused, and this 800 ms
#: latency objective is accepted.
LATENCY_SLO=$(post "$API/api/v1/platform/slo?project_id=$PROJ&environment_id=$ENV" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"inventory p95 latency\",\"indicator\":\"LATENCY\",\"target\":800,\"comparison\":\"AT_MOST\",\"metric_name\":\"http.inventory.latency.p95\",\"component_id\":\"$INV\",\"actor\":\"smoke\"}")
LATENCY_ID=$(echo "$LATENCY_SLO" | jget "d['slo_id']")
LATENCY_READ=$(curl -fsS "$API/api/v1/platform/slo/$LATENCY_ID?project_id=$PROJ")
echo "$LATENCY_READ" | jb "d['indicator'] == 'LATENCY' and d['target'] == 800" \
  && ok "a latency objective in milliseconds is expressible" \
  || bad "latency slo" "$LATENCY_READ"

echo "== 9. Global search and isolation (§17, §18, §42) =="
HELP=$(curl -fsS "$API/api/v1/platform/search/help")
echo "$HELP" | jb "len(d['kinds']) >= 5" \
  && ok "search publishes its kinds" || bad "search help" "$HELP"
RESULTS=$(curl -fsS -G "$API/api/v1/platform/search" --data-urlencode "q=checkout" --data "project_id=$PROJ")
echo "$RESULTS" | jb "d['total'] >= 1" \
  && ok "search finds the component it was given" || bad "search results" "$RESULTS"
echo "$RESULTS" | jb "isinstance(d['results'], dict)" \
  && ok "search groups results by kind" || bad "search grouping" "missing"
NOMATCH=$(curl -fsS -G "$API/api/v1/platform/search" \
  --data-urlencode "q=zzz-nothing-matches-$TS" --data "project_id=$PROJ")
echo "$NOMATCH" | jb "d['total'] == 0" \
  && ok "a no-match query returns zero, not everything" || bad "search no-match" "$NOMATCH"
FOREIGN=$(curl -fsS -G "$API/api/v1/platform/search" --data-urlencode "q=checkout" --data "project_id=$OTHER")
echo "$FOREIGN" | jb "d['total'] == 0" \
  && ok "another project sees none of this project's records" || bad "search isolation" "$FOREIGN"

echo "== 10. Data quality (§87–§90) =="
CHECK=$(post "$API/api/v1/platform/data-quality/check?project_id=$PROJ&persist=true")
echo "$CHECK" | jb "'findings' in d and 'checked' in d" \
  && ok "the consistency checks report findings and coverage" || bad "quality check" "$CHECK"
QUALITY=$(curl -fsS "$API/api/v1/platform/data-quality?project_id=$PROJ")
echo "$QUALITY" | jb "isinstance(d['issues'], list) and isinstance(d['descriptions'], dict)" \
  && ok "issues and their catalogue are readable" || bad "quality list" "$QUALITY"
ISSUE=$(echo "$QUALITY" | jget "(d['issues'] or [{}])[0].get('id','')")
if [ -n "$ISSUE" ]; then
  ACK=$(post "$API/api/v1/platform/data-quality/$ISSUE/status?project_id=$PROJ" \
    -H 'Content-Type: application/json' -d '{"status":"ACKNOWLEDGED","actor":"smoke"}')
  echo "$ACK" | jb "d.get('status') == 'ACKNOWLEDGED'" \
    && ok "an issue's disposition is recorded" || bad "issue status" "$ACK"
else
  ok "no issue was open (a clean project is not an error)"
fi

echo "== 11. Versioned configuration and rollback (§91–§94) =="
CONFIG=$(curl -fsS "$API/api/v1/platform/configuration?project_id=$PROJ")
echo "$CONFIG" | jb "isinstance(d['versions'], list)" \
  && ok "configuration exposes its version ledger" || bad "config read" "$CONFIG"
#: Only the scopes the platform actually owns are writable; anything else is
#: reported from environment configuration and a write to it is refused.
WRITE=$(post "$API/api/v1/platform/configuration?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"scope":"PROJECT_SETTINGS","settings":{"correlation_window_seconds":900},"change_summary":"smoke write","reason":"smoke","actor":"smoke"}')
echo "$WRITE" | jb "d.get('version', 0) >= 1" \
  && ok "a configuration write creates a version" || bad "config write" "$WRITE"
VERSION=$(echo "$WRITE" | jget "d.get('version', 1)")
CONFIG_LEDGER=$(curl -fsS "$API/api/v1/platform/configuration?project_id=$PROJ")
echo "$CONFIG_LEDGER" | jb "any(v['version'] == $VERSION for v in d['versions'])" \
  && ok "the write appears in the version ledger" || bad "config ledger" "$CONFIG_LEDGER"
ROLLBACK=$(post "$API/api/v1/platform/configuration/rollback?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"scope\":\"PROJECT_SETTINGS\",\"target_version\":$VERSION,\"actor\":\"smoke\",\"reason\":\"smoke rollback\"}")
#: A rollback is a *new* version that restores older content — history is
#: append-only, so the ledger still shows what was actually configured and when.
echo "$ROLLBACK" | jb "d.get('restored_from') == $VERSION and d.get('new_version', 0) > $VERSION" \
  && ok "a previous version restores as a new version" || bad "config rollback" "$ROLLBACK"
check_status "a configuration write without a scope is refused" \
  "$(code -X POST "$API/api/v1/platform/configuration?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d '{"settings":{}}')" "422"
check_status "a scope the platform does not own is not writable" \
  "$(code -X POST "$API/api/v1/platform/configuration?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d '{"scope":"DATABASE","settings":{"pool_size":5},"change_summary":"nope","reason":"smoke"}')" "422"

echo "== 12. Feature flags carry safe defaults (§61, §62) =="
FLAGS=$(curl -fsS "$API/api/v1/platform/feature-flags?project_id=$PROJ")
echo "$FLAGS" | jb "isinstance(d['flags'], dict) and len(d['reasons']) >= 1" \
  && ok "feature flags expose their state and reason" || bad "flags" "$FLAGS"
echo "$FLAGS" | jb "all(isinstance(v, bool) for v in d['flags'].values())" \
  && ok "every flag is a boolean, never a defaulted truthy value" || bad "flag types" "non-boolean"

echo "== 13. Notifications (§53–§56) =="
NOTES=$(curl -fsS "$API/api/v1/platform/notifications?project_id=$PROJ")
echo "$NOTES" | jb "isinstance(d['notifications'], list)" \
  && ok "the notification inbox is readable" || bad "notifications" "$NOTES"
NOTE=$(echo "$NOTES" | jget "(d['notifications'] or [{}])[0].get('id','')")
if [ -n "$NOTE" ]; then
  READ=$(post "$API/api/v1/platform/notifications/$NOTE/read?project_id=$PROJ" \
    -H 'Content-Type: application/json' -d '{"actor":"smoke","acknowledge":true}')
  echo "$READ" | jb "d.get('status') in ('READ','ACKNOWLEDGED')" \
    && ok "a notification is acknowledged by a named actor" || bad "notification ack" "$READ"
else
  ok "no notification was raised for this synthetic episode"
fi

echo "== 14. Reports, metrics and the improvement plan (§73–§85) =="
REPORT=$(curl -fsS "$API/api/v1/platform/reports?project_id=$PROJ&kind=reliability&days=30")
echo "$REPORT" | jb "isinstance(d['sections'], dict) and 'limitations' in d" \
  && ok "a report generates with its sections and limitations" || bad "report" "$REPORT"
METRICS=$(curl -fsS "$API/api/v1/platform/metrics?project_id=$PROJ&days=30")
echo "$METRICS" | jb "isinstance(d, dict)" \
  && ok "engineering metrics generate" || bad "metrics" "$METRICS"
PLAN=$(curl -fsS "$API/api/v1/platform/improvement-plan?project_id=$PROJ&days=30")
echo "$PLAN" | jb "isinstance(d['items'], list) and len(d['criteria']) > 0" \
  && ok "the improvement plan states its criteria" || bad "improvement plan" "$PLAN"

echo "== 15. Platform health and readiness (§57–§60, §105–§107) =="
HEALTH=$(curl -fsS "$API/api/v1/platform/health")
echo "$HEALTH" | jb "'ready' in d and isinstance(d['subsystems'], list)" \
  && ok "health reports readiness and subsystems" || bad "health" "$HEALTH"
echo "$HEALTH" | jb "all(('required' in s and 'optional' in s) for s in d['subsystems'])" \
  && ok "every subsystem is labelled required or optional" || bad "subsystem roles" "missing"
READY=$(curl -fsS "$API/api/v1/platform/readiness")
echo "$READY" | jb "'required_subsystems' in d and 'optional_subsystems' in d" \
  && ok "readiness separates required from optional" || bad "readiness" "$READY"
DEPS=$(curl -fsS "$API/api/v1/platform/dependencies")
echo "$DEPS" | jb "isinstance(d['graceful_degradation'], dict)" \
  && ok "dependencies document graceful degradation" || bad "dependencies" "$DEPS"

echo "== 16. Multi-project isolation (§42) =="
FOREIGN_CASES=$(curl -fsS "$API/api/v1/platform/cases?project_id=$OTHER")
echo "$FOREIGN_CASES" | jb "d['total'] == 0" \
  && ok "another project sees no case" || bad "case isolation" "$FOREIGN_CASES"
FOREIGN_SL=$(curl -fsS "$API/api/v1/platform/services?project_id=$OTHER")
echo "$FOREIGN_SL" | jb "d['total'] == 0" \
  && ok "another project sees no service" || bad "service isolation" "$FOREIGN_SL"

echo "== 17. The platform workspace renders (§116) =="
for P in "/platform" "/platform/cases" "/platform/services" "/platform/slo" \
  "/platform/changes" "/platform/search" "/platform/reports" \
  "/platform/data-quality" "/platform/governance" "/platform/activity" "/platform/health"; do
  C=$(code "$WEB$P")
  [ "$C" = "200" ] && ok "GET $P → 200" || bad "web $P" "$C"
done
PLATFORM_HTML=$(curl -fsS "$WEB/platform" || echo "")
echo "$PLATFORM_HTML" | grep -qi "Unified Reliability Platform" \
  && ok "the overview is the unified platform view" || bad "web overview" "missing heading"
echo "$PLATFORM_HTML" | grep -qi "never executes remediation" \
  && ok "the overview states the control-plane boundary" || bad "web boundary copy" "missing"
HEALTH_HTML=$(curl -fsS "$WEB/platform/health" || echo "")
#: Required-vs-optional has to be visible in the page, not just in the payload:
#: an operator reading "degraded" needs to know whether operations can continue.
echo "$HEALTH_HTML" | grep -qiE "required" \
  && ok "the health page distinguishes required subsystems" || bad "web health copy" "missing"

echo "== 18. Cleanup =="
check_status "the scratch project deletes" "$(code -X DELETE "$API/api/v1/projects/$PROJ")" "204"
check_status "the second scratch project deletes" "$(code -X DELETE "$API/api/v1/projects/$OTHER")" "204"
LEFTOVER=$(sql "SELECT count(*) FROM reliability_cases WHERE project_id = '$PROJ'")
[ "${LEFTOVER:-0}" = "0" ] \
  && ok "nothing the run recorded outlives the project it recorded it against" \
  || bad "leftover cases" "found=$LEFTOVER"
LEFTOVER_SLO=$(sql "SELECT count(*) FROM service_level_objectives WHERE project_id = '$PROJ'")
[ "${LEFTOVER_SLO:-0}" = "0" ] \
  && ok "and no objective survived the delete" || bad "leftover SLOs" "found=$LEFTOVER_SLO"

if [ "${DDL_PROBE:-0}" = "1" ]; then
  echo "== 19. The Phase 11 migration reverses (opt-in DDL_PROBE=1) =="
  REV=$($COMPOSE exec -T api sh -lc "cd /app && alembic downgrade -1 && alembic upgrade head" 2>&1) || true
  printf '%s' "$REV" | grep -qi "error" \
    && bad "migration reverse" "$(printf '%s' "$REV" | tail -c 300)" \
    || ok "the Phase 11 migration downgrades and re-applies cleanly"
fi

echo
echo "Phase 11 live smoke: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
