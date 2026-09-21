#!/usr/bin/env bash
# ARGUS Phase 3 — live end-to-end smoke test (Anomaly & Incident Intelligence)
#
# Verifies the detection-and-correlation layer against the running compose
# stack, using the seeded ARGUS Demo Commerce project (whose entrypoint seeds the
# deterministic "Checkout Latency Incident"):
#   demo incident → summary → timeline → anomalies → evidence → components
#   → graph context → deployments/config context → anomaly explanation
#   → explainable thresholds → lifecycle + illegal transitions → isolation
#   → detection trigger (idempotency) → metrics/dashboard → Prometheus
#   → rules/suppressions/maintenance windows → async ingest→detector hook
#   → web incident UI
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
set -eu
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
# jany <url> <expr> : true when the expression holds over the fetched payload.
check_status() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected=$3 got=$2"; }

# POST/PATCH <url> <json> → http status (temp-file body, as in earlier scripts).
jreq() {
  local f method="${3:-POST}"
  f=$(mktemp)
  printf '%s' "$2" > "$f"
  curl -s -o /dev/null -w '%{http_code}' -X "$method" "$1" \
    -H 'Content-Type: application/json' -d "@$f"
  rm -f "$f"
}

echo "== 0. Locate seeded demo project =="
DEMO=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['slug']=='argus-demo-commerce'][0]['id']")
echo "  project: $DEMO"
ENVS=$(curl -fsS "$API/api/v1/projects/$DEMO/environments")
PROD=$(echo "$ENVS" | jget "[e for e in d['items'] if e['name'].lower()=='production'][0]['id']")
COMPS=$(curl -fsS "$API/api/v1/projects/$DEMO/components?page_size=100")
CO_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='Checkout Service' and c.get('environment_id')=='$PROD'][0]['id']")
INV_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='Inventory Service' and c.get('environment_id')=='$PROD'][0]['id']")
echo "  production: $PROD  checkout: $CO_ID"

echo "== 1. Demo anomalies were detected by the real detectors =="
ANOMS=$(curl -fsS "$API/api/v1/anomalies?project_id=$DEMO&page_size=100")
A_TOTAL=$(echo "$ANOMS" | jget "d['total']")
A_TYPES=$(echo "$ANOMS" | jget "sorted(set(a['anomaly_type'] for a in d['items']))")
[ "$A_TOTAL" -ge 5 ] 2>/dev/null && ok "at least 5 anomalies detected (total=$A_TOTAL)" || bad "anomaly count" "$A_TOTAL"
for T in LATENCY_SPIKE ERROR_RATE_SPIKE TRACE_FAILURE_SPIKE HEALTH_DEGRADATION METRIC_BASELINE_DEVIATION LOG_PATTERN_SPIKE; do
  echo "$ANOMS" | jb "any(a['anomaly_type']=='$T' for a in d['items'])" \
    && ok "detected $T" || bad "detector $T" "types=$A_TYPES"
done
echo "$ANOMS" | jb "all(a['fingerprint'] and a['detected_at'] for a in d['items'])" \
  && ok "every anomaly carries a fingerprint and timestamp" || bad "anomaly provenance" "missing fingerprint"

echo "== 2. The incident exists and is evidence-backed =="
INCS=$(curl -fsS "$API/api/v1/incidents?project_id=$DEMO&page_size=50")
# Identified by the demo story itself (the checkout latency spike), not by list
# position: "newest fingerprinted incident" would silently follow whatever a
# previous run left behind.
INC_ID=$(curl -fsS "$API/api/v1/anomalies?project_id=$DEMO&component_id=$CO_ID&metric_name=http.checkout.latency.p95&anomaly_type=LATENCY_SPIKE&page_size=10" \
  | jget "[a for a in d['items'] if a.get('incident_id')][0]['incident_id']")
INC_SEV=$(echo "$INCS" | jget "[i for i in d['items'] if i['id']=='$INC_ID'][0]['severity']")
INC_STATUS=$(echo "$INCS" | jget "[i for i in d['items'] if i['id']=='$INC_ID'][0]['status']")
echo "  incident: $INC_ID ($INC_SEV / $INC_STATUS)"
case "$INC_SEV" in HIGH|CRITICAL) ok "incident severity is escalated by blast radius ($INC_SEV)";; *) bad "incident severity" "$INC_SEV";; esac
echo "$INCS" | jb "all(i.get('fingerprint') for i in d['items'] if i.get('correlation_rationale'))" \
  && ok "correlated incidents store a fingerprint" || bad "incident fingerprint" "missing"

echo "== 3. Deterministic summary — no causality claim =="
SUMMARY=$(curl -fsS "$API/api/v1/incidents/$INC_ID/summary")
S_TEXT=$(echo "$SUMMARY" | jget "d['text']")
echo "$SUMMARY" | jb "'does not establish' in d['text']" && ok "summary states the non-causality boundary" || bad "summary boundary" "$(echo "$S_TEXT" | head -c 120)"
echo "$SUMMARY" | jb "'2 minutes before the first observed anomaly' in d['text']" && ok "summary places the deployment 2 minutes before the first anomaly" || bad "summary context" "$(echo "$S_TEXT" | head -c 200)"
echo "$SUMMARY" | jb "len(d['generated_from']) > 0" && ok "summary names the evidence classes it used" || bad "generated_from" "$SUMMARY"
echo "$SUMMARY" | jb "all('does not establish' in ln.lower() for ln in d['text'].splitlines() if 'caused the incident' in ln.lower())" \
  && ok "no line asserts causality" || bad "causality wording" "assertion found"

echo "== 4. Timeline: ordered, facts vs marked context =="
TL=$(curl -fsS "$API/api/v1/incidents/$INC_ID/timeline")
echo "$TL" | jb "any(e['event_type']=='DEPLOYMENT_OCCURRED' and e['is_context_only'] for e in d['items'])" \
  && ok "deployment entry present and marked context-only" || bad "timeline deployment" "$TL"
echo "$TL" | jb "any(e['event_type']=='ANOMALY_DETECTED' for e in d['items'])" && ok "anomaly entries on the timeline" || bad "timeline anomalies" "none"
echo "$TL" | jb "any(e['event_type']=='COMPONENT_AFFECTED' for e in d['items'])" && ok "affected-component entries on the timeline" || bad "timeline components" "none"
echo "$TL" | jb "[e['occurred_at'] for e in d['items']] == sorted(e['occurred_at'] for e in d['items'])" \
  && ok "timeline is chronologically ordered (UTC)" || bad "timeline order" "not sorted"
echo "$TL" | jb "any(e['event_type']=='DEPLOYMENT_OCCURRED' and 'no causal relationship is claimed' in (e.get('description') or '') for e in d['items'])" \
  && ok "context-only entry says no causal relationship is claimed" || bad "context wording" "missing"

echo "== 5. Correlated anomalies are grouped, not causal =="
IA=$(curl -fsS "$API/api/v1/incidents/$INC_ID/anomalies")
IA_TOTAL=$(echo "$IA" | jget "d['total']")
[ "$IA_TOTAL" -ge 5 ] 2>/dev/null && ok "incident groups $IA_TOTAL anomalies" || bad "incident anomalies" "$IA_TOTAL"
echo "$IA" | jb "all(a['incident_id']=='$INC_ID' for a in d['items'])" && ok "every grouped anomaly references the incident" || bad "grouping" "mismatch"
# Extract the fields in one pass: a Python dict repr is not re-parseable JSON,
# so piping a list/dict element into a second jget would be a latent bug.
LAT_FIELDS=$(echo "$IA" | jget "'|'.join([_lat['id'], str(_lat['observed_value']), str(_lat['expected_value']), str(_lat['threshold'])]) if (_lat := next((a for a in d['items'] if a['anomaly_type']=='LATENCY_SPIKE'), None)) else ''")
ANOM_ID=$(echo "$LAT_FIELDS" | cut -d'|' -f1)
LAT_OBS=$(echo "$LAT_FIELDS" | cut -d'|' -f2)
LAT_EXP=$(echo "$LAT_FIELDS" | cut -d'|' -f3)
LAT_THR=$(echo "$LAT_FIELDS" | cut -d'|' -f4)
if [ -n "$ANOM_ID" ]; then
  ok "latency anomaly observed=$LAT_OBS expected=$LAT_EXP threshold=$LAT_THR"
  [ "$LAT_OBS" != "$LAT_EXP" ] && ok "expected value is the baseline, not the crossed threshold" || bad "threshold/expected conflation" "both $LAT_OBS"
else
  bad "latency anomaly" "not found"
fi

echo "== 6. Evidence carries provenance and a relevance reason =="
EV=$(curl -fsS "$API/api/v1/incidents/$INC_ID/evidence?page_size=100")
echo "$EV" | jb "all(e.get('provenance') and e.get('relevance_reason') for e in d['items'])" \
  && ok "every evidence row has provenance + relevance_reason" || bad "evidence provenance" "$EV"
echo "$EV" | jb "any(e['evidence_type']=='DEPLOYMENT' and 'temporal context only' in (e.get('relevance_reason') or '') for e in d['items'])" \
  && ok "deployment evidence labelled temporal context only" || bad "deployment evidence" "missing"
echo "$EV" | jb "any(e['evidence_type']=='CONFIGURATION_CHANGE' for e in d['items'])" \
  && ok "configuration-change evidence recorded" || bad "config evidence" "missing"
echo "$EV" | jb "not any('cause' in (e.get('relevance_reason') or '').lower().replace('causal','') for e in d['items'] if e['evidence_type']!='DEPLOYMENT')" \
  && ok "relevance reasons never claim a cause" || bad "evidence wording" "causality in relevance_reason"

echo "== 7. Observed blast radius is classified, not implied =="
IC=$(curl -fsS "$API/api/v1/incidents/$INC_ID/components")
echo "$IC" | jb "any(c['classification']=='DIRECTLY_OBSERVED' and c['component_id']=='$CO_ID' for c in d['items'])" \
  && ok "checkout is DIRECTLY_OBSERVED" || bad "blast radius direct" "$IC"
echo "$IC" | jb "any(c['classification'] in ('UPSTREAM_CONTEXT','DOWNSTREAM_CONTEXT','DEPENDENCY_CONTEXT') for c in d['items'])" \
  && ok "context components labelled separately from observed" || bad "blast radius context" "$IC"
echo "$IC" | jb "all(c['classification'] and c['reason'] for c in d['items'])" \
  && ok "every affected component explains its classification" || bad "classification reason" "missing"

echo "== 8. Graph context is structural only =="
GCTX=$(curl -fsS "$API/api/v1/incidents/$INC_ID/graph")
echo "$GCTX" | jb "'not implied to be causes' in d['disclaimer']" && ok "graph context carries the non-causality disclaimer" || bad "graph disclaimer" "$GCTX"
echo "$GCTX" | jb "len(d['nodes']) >= 1" && ok "graph context returns nodes" || bad "graph nodes" "$GCTX"

echo "== 9. Deployment / configuration context =="
DEP=$(curl -fsS "$API/api/v1/incidents/$INC_ID/deployments")
echo "$DEP" | jb "len(d) >= 1" && ok "nearby deployment attached ($(echo "$DEP" | jget "len(d)"))" || bad "deployments" "$DEP"
echo "$DEP" | jb "all(i['is_context_only'] for i in d)" && ok "every deployment is flagged context-only" || bad "deployment context flag" "$DEP"
echo "$DEP" | jb "all(abs(i['seconds_before_first_anomaly'] - 120) < 5 for i in d)" \
  && ok "deployment timestamped 2 minutes before the first anomaly" || bad "deployment timing" "$DEP"
CFG=$(curl -fsS "$API/api/v1/incidents/$INC_ID/configuration-changes")
echo "$CFG" | jb "len(d) >= 1 and all(i['is_context_only'] for i in d)" && ok "configuration changes flagged context-only" || bad "config context" "$CFG"

echo "== 10. Anomaly explainability (§52) =="
DETAIL=$(curl -fsS "$API/api/v1/anomalies/$ANOM_ID")
# bool(...) is required: jb succeeds only on a *boolean* True, and a chained
# `and` would yield the non-empty string itself — a silent always-FAIL.
echo "$DETAIL" | jb "bool(d['explanation'] and d['explanation']['why_detected'])" && ok "explanation answers 'why was this detected'" || bad "explanation" "$DETAIL"
echo "$DETAIL" | jb "'THRESHOLD' in (d['explanation'].get('threshold_exceeded') or '')" && ok "threshold rules report the line that was crossed" || bad "threshold explanation" "$(echo "$DETAIL" | jget "d['explanation']")"
echo "$DETAIL" | jb "'not the probability' in (d['explanation'].get('confidence_meaning') or '')" && ok "confidence defined as evidence strength, not causal probability" || bad "confidence meaning" "$DETAIL"
echo "$DETAIL" | jb "'|' in d['explanation']['fingerprint_material']" && ok "fingerprint material is inspectable" || bad "fingerprint material" "$DETAIL"
echo "$DETAIL" | jb "len(d['observations']) >= 1" && ok "observations returned with the anomaly" || bad "observations" "$DETAIL"

echo "== 11. Lifecycle: legal transitions only =="
ACK=$(jreq "$API/api/v1/incidents/$INC_ID/acknowledge" '{"actor":"smoke"}' POST)
check_status "POST incident acknowledge → 200" "$ACK" "200"
IST=$(curl -fsS "$API/api/v1/incidents/$INC_ID" | jget "d['status']")
[ "$IST" = "ACKNOWLEDGED" ] && ok "incident is ACKNOWLEDGED" || bad "ack status" "$IST"
ACK_BY=$(curl -fsS "$API/api/v1/incidents/$INC_ID" | jget "d['status_changed_by']")
[ "$ACK_BY" = "smoke" ] && ok "actor recorded on the incident" || bad "actor" "$ACK_BY"
ILLEGAL=$(jreq "$API/api/v1/incidents/$INC_ID" '{"status":"OPEN"}' PUT)
check_status "illegal transition ACKNOWLEDGED→OPEN → 409" "$ILLEGAL" "409"
RES=$(jreq "$API/api/v1/incidents/$INC_ID/resolve" '{"actor":"smoke"}' POST)
check_status "POST incident resolve → 200" "$RES" "200"
RESOLVED_AT=$(curl -fsS "$API/api/v1/incidents/$INC_ID" | jget "d['resolved_at']")
[ "$RESOLVED_AT" != "None" ] && ok "resolved_at stamped" || bad "resolved_at" "$RESOLVED_AT"
REO=$(jreq "$API/api/v1/incidents/$INC_ID/reopen" '{"actor":"smoke"}' POST)
check_status "POST incident reopen → 200" "$REO" "200"
FINAL_STATUS=$(curl -fsS "$API/api/v1/incidents/$INC_ID" | jget "d['status']")
[ "$FINAL_STATUS" = "OPEN" ] && ok "reopen returns the incident to OPEN" || bad "reopen status" "$FINAL_STATUS"

# The anomaly lifecycle is exercised in §18, against the throwaway anomaly that
# run creates: acknowledging a terminal anomaly is legally a 409, so driving it
# off the demo's rows would make this section pass only once.
# Restore the demo to a live-looking state for the UI.
jreq "$API/api/v1/projects/$DEMO/anomalies/detect" '{}' POST > /dev/null

echo "== 12. Isolation: out-of-scope ids are 404, never data =="
RANDOM_PROJECT="00000000-0000-0000-0000-00000000dead"
S1=$(code "$API/api/v1/incidents/$INC_ID?project_id=$RANDOM_PROJECT")
check_status "incident with foreign project_id → 404" "$S1" "404"
S2=$(code "$API/api/v1/anomalies?project_id=$RANDOM_PROJECT")
check_status "anomalies for unknown project → 404" "$S2" "404"
S3=$(code "$API/api/v1/incidents/$INC_ID/timeline?project_id=$RANDOM_PROJECT")
check_status "timeline with foreign project_id → 404" "$S3" "404"
S4=$(code "$API/api/v1/incidents/$INC_ID/summary?project_id=$RANDOM_PROJECT")
check_status "summary with foreign project_id → 404" "$S4" "404"
S5=$(code "$API/api/v1/incidents/00000000-0000-0000-0000-000000000000")
check_status "unknown incident id → 404" "$S5" "404"

echo "== 13. Detection trigger is bounded and idempotent =="
DET=$(curl -fsS -X POST "$API/api/v1/projects/$DEMO/anomalies/detect?environment_id=$PROD")
echo "$DET" | jb "'detection' in d and 'correlation' in d" && ok "detect returns detection + correlation summaries" || bad "detect payload" "$DET"
DET_OPENED=$(echo "$DET" | jget "d['detection']['anomalies_opened']")
DET_UPDATED=$(echo "$DET" | jget "d['detection']['anomalies_updated']")
[ "$DET_OPENED" -eq 0 ] 2>/dev/null && ok "re-run opens no duplicate anomalies (updated=$DET_UPDATED)" || bad "idempotency" "opened=$DET_OPENED"
INC_COUNT=$(curl -fsS "$API/api/v1/incidents?project_id=$DEMO&page_size=50" | jget "len([i for i in d['items'] if i['id']=='$INC_ID'])")
[ "$INC_COUNT" -eq 1 ] && ok "re-run did not duplicate the incident" || bad "incident dedup" "$INC_COUNT matching incidents"

echo "== 14. Reliability metrics & dashboard =="
# The window is derived from the data under test rather than assumed. A gate
# that hard-codes 24h silently measures its own clock: on a stack seeded more
# than a day ago the seeded anomalies fall outside the window and the check
# fails for a reason that has nothing to do with the engine. Widening the
# window to cover the oldest stored anomaly keeps the assertion exactly as
# strict — "the endpoints reflect what the detector stored" — while removing
# the dependency on having just seeded.
ANOM_OLDEST=$(curl -fsS "$API/api/v1/anomalies?project_id=$DEMO&page_size=100" \
  | jget "min((a['detected_at'] for a in d['items']), default=None)")
WINDOW=$(python3 -c "
import datetime, sys
oldest = sys.argv[1]
if oldest in ('', 'None'):
    print(86400)
else:
    t = datetime.datetime.fromisoformat(oldest.replace('Z', '+00:00'))
    now = datetime.datetime.now(datetime.timezone.utc)
    print(max(86400, int((now - t).total_seconds()) + 3600))
" "$ANOM_OLDEST")
echo "  metrics window: ${WINDOW}s (covers the oldest stored anomaly)"
MET=$(curl -fsS "$API/api/v1/projects/$DEMO/reliability-metrics?window_seconds=$WINDOW")
echo "$MET" | jb "d['anomalies_detected'] >= 5" && ok "metrics count detected anomalies" || bad "metrics anomalies" "$MET"
echo "$MET" | jb "d['incidents_open'] >= 1" && ok "metrics count open incidents" || bad "metrics open incidents" "$MET"
echo "$MET" | jb "'resolved_at' in d['mttr_definition'] and 'acknowledged_at' in d['mtta_definition']" \
  && ok "MTTA/MTTR definitions are stated with the numbers" || bad "metric definitions" "$MET"
DASH=$(curl -fsS "$API/api/v1/projects/$DEMO/incident-dashboard?window_seconds=$WINDOW&bucket_seconds=3600")
echo "$DASH" | jb "'metrics' in d and isinstance(d['anomalies_over_time'], list)" && ok "dashboard returns metrics + series" || bad "dashboard" "$DASH"
echo "$DASH" | jb "len(d['top_affected_components']) >= 1" && ok "dashboard ranks affected components" || bad "dashboard components" "$DASH"

echo "== 15. Prometheus exposes Phase 3 series =="
PROM=$(curl -fsS "$API/metrics")
echo "$PROM" | grep -q "argus_anomalies_detected_24h" && ok "argus_anomalies_detected_24h exported" || bad "prom anomalies" "missing"
echo "$PROM" | grep -q "argus_incidents_open" && ok "argus_incidents_open exported" || bad "prom incidents" "missing"
echo "$PROM" | grep -q "argus_anomalies_deduplicated" && ok "argus_anomalies_deduplicated exported" || bad "prom dedup" "missing"

echo "== 16. Rules: validated creation, patch, hostile config rejected =="
# Everything from here on runs in a throwaway project. Creating rules, telemetry
# and incidents inside the demo project would change what §2/§13 select on the
# *next* run — a gate that only passes on a pristine database is not a gate.
TS=$(date +%s)
# Reused across runs (fixed slug/component names) so repeated gates do not pile
# up throwaway projects; only the rule is per-run, which is what proves 201.
SCRATCH=$(curl -fsS "$API/api/v1/projects?page_size=100" \
  | jget "next((p['id'] for p in d['items'] if p['slug']=='smoke-detection-harness'), '')")
[ -n "$SCRATCH" ] || SCRATCH=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d '{"name":"Smoke Detection Harness","slug":"smoke-detection-harness"}' | jget "d['id']")
_scratch_component() {
  local name="$1" id
  id=$(curl -fsS "$API/api/v1/projects/$SCRATCH/components?page_size=100" \
    | jget "next((c['id'] for c in d['items'] if c['name']=='$name'), '')")
  [ -n "$id" ] || id=$(curl -fsS -X POST "$API/api/v1/projects/$SCRATCH/components" \
    -H 'Content-Type: application/json' -d "{\"name\":\"$name\",\"component_type\":\"SERVICE\"}" \
    | jget "d['id']")
  printf '%s' "$id"
}
S_A=$(_scratch_component "suppressed-svc")
S_B=$(_scratch_component "observed-svc")
ok "isolated scratch project for config + async checks ($SCRATCH)"
RULE_NAME="smoke-rule-$TS"
RULE_BODY="{\"project_id\":\"$SCRATCH\",\"name\":\"$RULE_NAME\",\"anomaly_type\":\"LATENCY_SPIKE\",\"condition\":\"THRESHOLD\",\"metric_name\":\"smoke.latency.p95\",\"threshold\":100,\"severity\":\"MEDIUM\",\"component_id\":\"$S_B\",\"window_seconds\":600,\"cooldown_seconds\":0,\"min_samples\":1}"
R_CODE=$(jreq "$API/api/v1/anomaly-rules" "$RULE_BODY")
check_status "POST anomaly-rule → 201" "$R_CODE" "201"
RULE_ID=$(curl -fsS "$API/api/v1/anomaly-rules?project_id=$SCRATCH&page_size=100" | jget "[r for r in d['items'] if r['name']=='$RULE_NAME'][0]['id']")
P_CODE=$(jreq "$API/api/v1/anomaly-rules/$RULE_ID" '{"enabled":false}' PATCH)
check_status "PATCH anomaly-rule → 200" "$P_CODE" "200"
P_ENABLED=$(curl -fsS "$API/api/v1/anomaly-rules/$RULE_ID" | jget "d['enabled']")
[ "$P_ENABLED" = "False" ] && ok "rule disabled via PATCH" || bad "rule patch" "$P_ENABLED"
BAD_RULE="{\"project_id\":\"$SCRATCH\",\"name\":\"bad-$TS\",\"anomaly_type\":\"LATENCY_SPIKE\",\"condition\":\"BASELINE_DEVIATION\",\"metric_name\":\"x\",\"severity\":\"HIGH\"}"
B_CODE=$(jreq "$API/api/v1/anomaly-rules" "$BAD_RULE")
check_status "unevaluable rule rejected → 422" "$B_CODE" "422"

echo "== 17. Suppressions & maintenance windows are auditable =="
# Scoped to component A while §18 observes component B: the suppression must be
# auditable without silencing the rest of the project, so §18 doubles as proof
# that suppression is component-scoped rather than a blanket project mute.
SUP_BODY="{\"project_id\":\"$SCRATCH\",\"component_id\":\"$S_A\",\"reason\":\"smoke maintenance (component A) $TS\",\"starts_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
SUP_CODE=$(jreq "$API/api/v1/anomaly-suppressions" "$SUP_BODY")
check_status "POST anomaly-suppression → 201" "$SUP_CODE" "201"
SUP_TOTAL=$(curl -fsS "$API/api/v1/anomaly-suppressions?project_id=$SCRATCH&active_only=true" | jget "len([s for s in d['items'] if s['reason'].startswith('smoke maintenance')])")
[ "$SUP_TOTAL" -ge 1 ] 2>/dev/null && ok "suppression stored with a reason (active=$SUP_TOTAL)" || bad "suppression list" "$SUP_TOTAL"
# A *future* window: a window is environment-wide (it has no component scope),
# so an active one would legitimately mute §18's anomaly. Scheduling it ahead
# proves the window is accepted, stored and auditable without altering now.
MW_BODY="{\"project_id\":\"$SCRATCH\",\"name\":\"smoke-window-$TS\",\"starts_at\":\"$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+1H +%Y-%m-%dT%H:%M:%SZ)\",\"ends_at\":\"$(date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+2H +%Y-%m-%dT%H:%M:%SZ)\",\"downgrade_severity\":true}"
MW_CODE=$(jreq "$API/api/v1/maintenance-windows" "$MW_BODY")
check_status "POST maintenance-window → 201" "$MW_CODE" "201"
MW_LIST=$(curl -fsS "$API/api/v1/maintenance-windows?project_id=$SCRATCH&page_size=100")
# Activeness is derived from the window bounds (the list endpoint returns them,
# not a boolean), so the check compares timestamps rather than trusting a flag.
echo "$MW_LIST" | jb "any(w['name']=='smoke-window-$TS' and __import__('datetime').datetime.fromisoformat(w['starts_at'].replace('Z','+00:00')) > __import__('datetime').datetime.now(__import__('datetime').timezone.utc) for w in d['items'])" \
  && ok "maintenance window stored as scheduled, not active" || bad "maintenance window" "$MW_LIST"
MW_ACTIVE=$(curl -fsS "$API/api/v1/maintenance-windows?project_id=$SCRATCH&active_only=true&page_size=100" | jget "len([w for w in d['items'] if w['name']=='smoke-window-$TS'])")
[ "$MW_ACTIVE" = "0" ] && ok "active_only filter excludes the future window" || bad "window activity filter" "$MW_ACTIVE"

echo "== 18. Async hook: ingested telemetry reaches the detectors =="
# §16 left the rule disabled on purpose; detection can only fire while it is on,
# so this both re-enables it and proves PATCH toggles in *both* directions.
REENABLE=$(jreq "$API/api/v1/anomaly-rules/$RULE_ID" '{"enabled":true}' PATCH)
check_status "PATCH anomaly-rule enabled=true → 200" "$REENABLE" "200"
R_ENABLED=$(curl -fsS "$API/api/v1/anomaly-rules/$RULE_ID" | jget "d['enabled']")
[ "$R_ENABLED" = "True" ] && ok "rule re-enabled for the async hook" || bad "rule re-enable" "$R_ENABLED"
NOW_NS=$(python3 -c "import time; print(int(time.time()*1e9))")
METRIC_BODY="{\"project_id\":\"$SCRATCH\",\"component_id\":\"$S_B\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"metric_name\":\"smoke.latency.p95\",\"metric_type\":\"GAUGE\",\"value\":5000,\"unit\":\"ms\"}"
M_CODE=$(jreq "$API/api/v1/observability/metrics" "$METRIC_BODY")
check_status "POST observability/metrics (new rule's metric) → 201" "$M_CODE" "201"
HOOK=no
for _ in $(seq 1 30); do
  sleep 1
  FOUND=$(curl -fsS "$API/api/v1/anomalies?project_id=$SCRATCH&metric_name=smoke.latency.p95&page_size=10" 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print('yes' if d['total'] >= 1 else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo no)
  [ "$FOUND" = "yes" ] && HOOK=yes && break
done
[ "$HOOK" = "yes" ] && ok "ingest → worker → detector opened the anomaly (async)" || bad "async detection hook" "no anomaly after 30s"
if [ "$HOOK" = "yes" ]; then
  HOOKED=$(curl -fsS "$API/api/v1/anomalies?project_id=$SCRATCH&metric_name=smoke.latency.p95&page_size=10")
  echo "$HOOKED" | jb "all(a['suppressed'] is False for a in d['items'])" \
    && ok "component-scoped suppression did not silence another component" || bad "suppression scope" "a component-scoped suppression suppressed another component"
  echo "$HOOKED" | jb "all(a['component_id']=='$S_B' for a in d['items'])" \
    && ok "anomaly is attributed to the observed component" || bad "anomaly component" "wrong component"

  # Lifecycle on this run's own anomaly: the demo's rows are left untouched, so
  # the checks hold on every run rather than only the first.
  S_ANOM=$(echo "$HOOKED" | jget "next((a['id'] for a in d['items'] if a['status']=='DETECTED'), '')")
  if [ -n "$S_ANOM" ]; then
    AACK=$(jreq "$API/api/v1/anomalies/$S_ANOM/acknowledge" '{"actor":"smoke"}' POST)
    check_status "POST anomaly acknowledge → 200" "$AACK" "200"
    AST=$(curl -fsS "$API/api/v1/anomalies/$S_ANOM" | jget "d['status']")
    [ "$AST" = "ACKNOWLEDGED" ] && ok "anomaly is ACKNOWLEDGED" || bad "anomaly ack status" "$AST"
    ARES=$(jreq "$API/api/v1/anomalies/$S_ANOM/resolve" '{"actor":"smoke"}' POST)
    check_status "POST anomaly resolve → 200" "$ARES" "200"
    AST=$(curl -fsS "$API/api/v1/anomalies/$S_ANOM" | jget "d['status']")
    [ "$AST" = "RESOLVED" ] && ok "anomaly reached RESOLVED" || bad "anomaly status" "$AST"
    REACK=$(jreq "$API/api/v1/anomalies/$S_ANOM/acknowledge" '{"actor":"smoke"}' POST)
    check_status "illegal transition RESOLVED→ACKNOWLEDGED → 409" "$REACK" "409"
  else
    bad "anomaly lifecycle" "no DETECTED anomaly for the scratch project"
  fi
fi

echo "== 19. Suppressions and windows deactivate, they are not deleted =="
# The gate deactivates what it created (and anything a previous run left
# active). Without this, each run would leave the demo environment muted for the
# next one — a self-sabotaging gate, and a false sense that suppression "works".
SUP_IDS=$(curl -fsS "$API/api/v1/anomaly-suppressions?project_id=$SCRATCH&active_only=true&page_size=100" \
  | jget "' '.join(s['id'] for s in d['items'] if s['reason'].startswith('smoke maintenance'))")
SUP_PATCHED=0
for SID in $SUP_IDS; do
  C=$(jreq "$API/api/v1/anomaly-suppressions/$SID" '{"enabled":false}' PATCH)
  [ "$C" = "200" ] && SUP_PATCHED=$((SUP_PATCHED+1)) || bad "PATCH suppression $SID" "$C"
done
[ "$SUP_PATCHED" -ge 1 ] && ok "active smoke suppression deactivated ($SUP_PATCHED)" || bad "suppression deactivation" "none patched"
SUP_LEFT=$(curl -fsS "$API/api/v1/anomaly-suppressions?project_id=$SCRATCH&active_only=true&page_size=100" \
  | jget "len([s for s in d['items'] if s['reason'].startswith('smoke maintenance')])")
[ "$SUP_LEFT" = "0" ] && ok "no active smoke suppression remains" || bad "suppression leak" "$SUP_LEFT still active"
SUP_ON_FILE=$(curl -fsS "$API/api/v1/anomaly-suppressions?project_id=$SCRATCH&page_size=100" \
  | jget "len([s for s in d['items'] if s['reason'].startswith('smoke maintenance')])")
[ "$SUP_ON_FILE" -ge 1 ] 2>/dev/null && ok "deactivated suppression is still on file (audit preserved)" || bad "suppression audit" "row vanished"

MW_IDS=$(curl -fsS "$API/api/v1/maintenance-windows?project_id=$SCRATCH&page_size=100" \
  | jget "' '.join(w['id'] for w in d['items'] if w['name'].startswith('smoke-window') and w['enabled'])")
MW_PATCHED=0
for WID in $MW_IDS; do
  C=$(jreq "$API/api/v1/maintenance-windows/$WID" '{"enabled":false}' PATCH)
  [ "$C" = "200" ] && MW_PATCHED=$((MW_PATCHED+1)) || bad "PATCH window $WID" "$C"
done
[ "$MW_PATCHED" -ge 1 ] && ok "smoke maintenance window deactivated ($MW_PATCHED)" || bad "window deactivation" "none patched"
MW_LEFT=$(curl -fsS "$API/api/v1/maintenance-windows?project_id=$SCRATCH&active_only=true&page_size=100" \
  | jget "len([w for w in d['items'] if w['name'].startswith('smoke-window')])")
[ "$MW_LEFT" = "0" ] && ok "no active smoke maintenance window remains" || bad "window leak" "$MW_LEFT still active"
# Leave the rule itself disabled too, so the scratch project stops evaluating.
RULE_OFF=$(jreq "$API/api/v1/anomaly-rules/$RULE_ID" '{"enabled":false}' PATCH)
[ "$RULE_OFF" = "200" ] && ok "scratch rule left disabled after the check" || bad "rule cleanup" "$RULE_OFF"
DEMO_SUP=$(curl -fsS "$API/api/v1/anomaly-suppressions?project_id=$DEMO&active_only=true&page_size=100" \
  | jget "len([s for s in d['items'] if s['reason'].startswith('smoke maintenance')])")
[ "$DEMO_SUP" = "0" ] && ok "demo project left unmuted by the gate" || bad "demo suppression leak" "$DEMO_SUP active"
DEMO_MW=$(curl -fsS "$API/api/v1/maintenance-windows?project_id=$DEMO&active_only=true&page_size=100" \
  | jget "len([w for w in d['items'] if w['name'].startswith('smoke-window')])")
[ "$DEMO_MW" = "0" ] && ok "demo project has no active smoke window" || bad "demo window leak" "$DEMO_MW active"
if [ -n "$MW_IDS" ]; then
  # Both effects off would be an inert row that claims to be a window.
  FIRST_MW=$(echo "$MW_IDS" | cut -d' ' -f1)
  MW_NOOP=$(jreq "$API/api/v1/maintenance-windows/$FIRST_MW" '{"suppress_anomalies":false,"downgrade_severity":false}' PATCH)
  [ "$MW_NOOP" = "422" ] && ok "window patch keeps the must-do-something invariant" || bad "window invariant" "expected 422 got $MW_NOOP"
fi

echo "== 20. Web incident intelligence UI =="
for P in "/incidents" "/incidents/dashboard" "/anomalies" "/incidents/$INC_ID"; do
  C=$(code "$WEB$P")
  [ "$C" = "200" ] && ok "GET $P → 200" || bad "web $P" "$C"
done
DETAIL_HTML=$(curl -fsS "$WEB/incidents/$INC_ID" || echo "")
echo "$DETAIL_HTML" | grep -q "temporal context" && ok "incident page renders deployment context wording" || bad "web detail" "no context wording"
echo "$DETAIL_HTML" | grep -qi "does not determine root-cause" && ok "incident page still declines to claim a root cause" || bad "web detail" "no scope statement"
echo "$DETAIL_HTML" | grep -q "/causal-analysis" && ok "incident page hands root-cause work to the Phase 4 view" || bad "web detail" "no RCA handoff"
ANOM_HTML=$(curl -fsS "$WEB/anomalies" || echo "")
echo "$ANOM_HTML" | grep -qi "detected by" && ok "anomaly center states anomalies are detector-derived" || bad "web anomalies" "missing copy"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "PASS ($PASS/$PASS)"
  exit 0
else
  echo "FAIL ($PASS passed, $FAIL failed)"
  exit 1
fi
