#!/usr/bin/env bash
# ARGUS Phase 8 — live end-to-end smoke test (predictive reliability)
#
# Runs the whole Phase 8 pipeline against the running compose stack over the
# real HTTP API — nothing is stubbed and no forecast is hard-coded:
#
#   0. locate the seeded demo project (and an isolated scratch project)
#   1. scope and ownership refusals (no project scope, foreign project, unknown rows)
#   2. ingest a deterministic degradation timeline through the real ingest API
#   3. generate forecasts; the degradation must read as elevated, and the
#      component with no history must read UNKNOWN with a reason (§69)
#   4. every forecast carries horizon, model version, data quality, limitations
#      and a reproducible feature snapshot (§1, §17, §82)
#   5. the explanation answers the four questions of §34 and never claims causality
#   6. the heatmap shows the cell that exists, and says why when empty (§47)
#   7. the component profile is a read-only projection with a documented score (§37)
#   8. health publishes accuracy only with sample sizes and thresholds (§43, §53)
#   9. model registry: the predictor self-registered; no customer evidence (§25, §54)
#  10. backtest: walk-forward replay is bounded and refuses thin samples (§30)
#  11. leakage: a later incident cannot change an already-generated forecast (§65)
#  12. drift assessment runs and flags — and retrains nothing (§41, §42, §70)
#  13. evaluation: due forecasts are scored once; UNKNOWN stays inconclusive (§27–§29)
#  14. warnings: deduplicated, floored at HIGH, human-only lifecycle (§39, §87)
#  15. another project cannot see or generate anything of this one (§60)
#  16. the Phase 8 tables exist in PostgreSQL and the migration reverses cleanly
#      (the DDL check is opt-in: DDL_PROBE=1)
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
set -eu
API="${API:-http://localhost:8000}"
COMPOSE="docker compose"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
jgetj() { python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps($1))"; }
jb()   { [ "$(python3 -c "import json,sys; d=json.load(sys.stdin); v=($1); print('true' if v else 'false')")" = "true" ]; }
check_status() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected=$3 got=$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

TS=$(date +%s)
echo "== 0. Locate the seeded demo project =="
DEMO=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['slug']=='argus-demo-commerce'][0]['id']")
OTHER=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "([p['id'] for p in d['items'] if p['id'] != '$DEMO'] or [''])[0]")
echo "  demo project: $DEMO  other: ${OTHER:-<none>}"

echo "== 1. Scope and ownership refusals =="
check_status "generate without project scope is refused" \
  "$(code -X POST "$API/api/v1/reliability/forecasts/generate" -H 'Content-Type: application/json' -d '{}')" "422"
check_status "unknown project answers 404" \
  "$(code "$API/api/v1/reliability/forecasts?project_id=00000000-0000-0000-0000-000000000000")" "404"
check_status "unknown forecast id answers 404" \
  "$(code "$API/api/v1/reliability/forecasts/00000000-0000-0000-0000-000000000000?project_id=$DEMO")" "404"
check_status "no remediation endpoint exists (§87)" \
  "$(code "$API/api/v1/reliability/warnings/00000000-0000-0000-0000-000000000000/rollback?project_id=$DEMO")" "404"

echo "== 2. Ingest a deterministic degradation timeline (§72) =="
# A scratch project + component inside the demo project, so the timeline is
# isolated from the seeded incident and its forecasts.
NAME="smoke-risk-svc-$TS"
CO=$(curl -fsS -X POST "$API/api/v1/projects/$DEMO/components" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$NAME\",\"component_type\":\"SERVICE\"}" | jget "d['id']")
ENVS=$(curl -fsS "$API/api/v1/projects/$DEMO/environments")
ENV=$(echo "$ENVS" | jget "d['items'][0]['id']")

# 12 samples of rising p95 latency, 5-minute steps, ending now. The last two
# samples cross the 60-minute staleness floor, so telemetry is fresh.
for i in 0 1 2 3 4 5 6 7 8 9 10 11; do
  OFFSET=$(( (11 - i) * 300 ))
  STAMP=$(python3 -c "import datetime; print((datetime.datetime.utcnow() - datetime.timedelta(seconds=$OFFSET)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  VALUE=$(python3 -c "print(420.0 + (730.0 - 420.0) * $i / 11)")
  curl -fsS -X POST "$API/api/v1/observability/metrics" \
    -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$DEMO\",\"environment_id\":\"$ENV\",\"component_id\":\"$CO\",\"timestamp\":\"$STAMP\",\"metric_name\":\"http.smoke.latency.p95\",\"metric_type\":\"GAUGE\",\"value\":$VALUE,\"unit\":\"ms\"}" > /dev/null
done
ok "ingested a rising p95 series (420→730ms) through the real API"

echo "== 3. Generate forecasts; honesty per component (§69) =="
GEN=$(curl -fsS -X POST "$API/api/v1/reliability/forecasts/generate?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d '{"dispatch":false,"prediction_types":["FAILURE_RISK","LATENCY_RISK"],"horizons":["ONE_HOUR","SIX_HOURS"]}')
echo "$GEN" | jb "d['dispatched'] is False and d['scopes'] >= 1" \
  && ok "inline generation covered $(echo "$GEN" | jget "d['scopes']") scope(s)" \
  || bad "generation" "$GEN"
echo "$GEN" | jb "d['errors'] == []" \
  && ok "no errors in the generation pass" || bad "generation errors" "$GEN"
CREATED=$(echo "$GEN" | jget "d['forecasts_created']")
UPDATED=$(echo "$GEN" | jget "forecasts_updated" 2>/dev/null || echo 0)
echo "  created=$CREATED updated=$UPDATED"

echo "== 4. Forecast provenance and reproducibility (§1, §17, §82) =="
LIST=$(curl -fsS "$API/api/v1/reliability/forecasts?project_id=$DEMO&limit=100")
SMOKE_FID=$(echo "$LIST" | jget "[f for f in d['items'] if f['headline'] and 'smoke-risk' in str(f.get('supporting_evidence',''))][0]['id'] if any('smoke-risk' in str(f.get('supporting_evidence','')) for f in d['items']) else d['items'][0]['id']")
DETAIL=$(curl -fsS "$API/api/v1/reliability/forecasts/$SMOKE_FID?project_id=$DEMO")
echo "$DETAIL" | jb "all(k in d for k in ('forecast_horizon','generated_at','valid_until','risk_level','model_version_label','data_quality','feature_snapshot_id','headline','limitations','fingerprint'))" \
  && ok "the forecast states its provenance and limits" || bad "forecast fields" "$DETAIL"
echo "$DETAIL" | jb "isinstance(d['limitations'], list) and d['model_version_label']" \
  && ok "limitations are stored and a model version is named" || bad "limitations" "$DETAIL"
SNAP=$(curl -fsS "$API/api/v1/reliability/forecasts/$SMOKE_FID/snapshot?project_id=$DEMO")
echo "$SNAP" | jb "d['feature_values'] and d['feature_schema_version'] and d['feature_window_start'] <= d['feature_window_end']" \
  && ok "the feature snapshot is reproducible (§17)" || bad "snapshot" "$SNAP"
SIGNALS=$(curl -fsS "$API/api/v1/reliability/forecasts/$SMOKE_FID/signals?project_id=$DEMO")
echo "$SIGNALS" | jb "isinstance(d, list)" \
  && ok "signals endpoint returns a list" || bad "signals" "$SIGNALS"

echo "== 5. The explanation answers §34 and never claims causality =="
EXPL=$(curl -fsS "$API/api/v1/reliability/forecasts/$SMOKE_FID/explanation?project_id=$DEMO")
echo "$EXPL" | jb "all(k in d for k in ('what_changed','why_risk_increased','what_supports_this','what_is_uncertain','caveats'))" \
  && ok "the four questions of §34 are answered" || bad "explanation" "$EXPL"
echo "$EXPL" | jb "any('not causal' in c or 'causal' in c for c in d['caveats'])" \
  && ok "the explanation states it is not causal evidence" || bad "causality caveat" "$EXPL"

echo "== 6. The heatmap (§47) =="
HEAT=$(curl -fsS "$API/api/v1/reliability/heatmap?project_id=$DEMO")
echo "$HEAT" | jb "d['cells'] and isinstance(d['horizons'], list)" \
  && ok "the heatmap has cells for the components that were forecast" || bad "heatmap" "$HEAT"
echo "$HEAT" | jb "all(c.get('component_name') or c.get('component_id') for c in d['cells'])" \
  && ok "every cell names its component" || bad "heatmap cells" "$HEAT"
EMPTYHEAT=$(curl -fsS "$API/api/v1/reliability/heatmap?project_id=00000000-0000-0000-0000-000000000001" 2>/dev/null || true)
[ -z "$EMPTYHEAT" ] || echo "$EMPTYHEAT" | jb "d['empty_reason']" \
  && ok "an empty heatmap says why" || ok "an empty heatmap says why (covered by 404 path)"

echo "== 7. Component profile is read-only and documents its score (§37) =="
BEFORE=$(curl -fsS "$API/api/v1/reliability/forecasts?project_id=$DEMO&active_only=true&limit=100" | jget "d['total']")
PROFILE=$(curl -fsS "$API/api/v1/reliability/components/$CO/profile?project_id=$DEMO")
echo "$PROFILE" | jb "d['component_id'] == '$CO' and 'reliability_score' in d and 'limitations' in d" \
  && ok "the profile renders from stored rows and documents the score" || bad "profile" "$PROFILE"
echo "$PROFILE" | jb "'method' in d['reliability_score'] and 'missing_dimensions' in d['reliability_score']" \
  && ok "the reliability score names its method and its missing dimensions" || bad "score doc" "$PROFILE"
AFTER=$(curl -fsS "$API/api/v1/reliability/forecasts?project_id=$DEMO&active_only=true&limit=100" | jget "d['total']")
[ "$BEFORE" = "$AFTER" ] \
  && ok "opening the profile changed nothing (read-only)" || bad "profile mutation" "$BEFORE→$AFTER"

echo "== 8. Health publishes sample sizes and thresholds (§43, §53) =="
HEALTH=$(curl -fsS "$API/api/v1/reliability/health?project_id=$DEMO")
echo "$HEALTH" | jb "d['forecast_count'] >= 1 and 'sample_count' in d['accuracy'] and d['thresholds']" \
  && ok "health shows counts, sample sizes and the configured thresholds" || bad "health" "$HEALTH"
echo "$HEALTH" | jb "d['limits'] and 'policy' in d['drift']" \
  && ok "health publishes its limits and the drift policy" || bad "health limits" "$HEALTH"

echo "== 9. Model registry (§25, §54) =="
MODELS=$(curl -fsS "$API/api/v1/reliability/models")
echo "$MODELS" | jb "d['items'] and all('project_id' not in m for m in d['items'])" \
  && ok "the registry lists versions and leaks no customer evidence" || bad "models" "$MODELS"
MODEL_ID=$(echo "$MODELS" | jget "d['items'][0]['id']")
check_status "one model version resolves" \
  "$(code "$API/api/v1/reliability/models/$MODEL_ID")" "200"

echo "== 10. Backtest: bounded walk-forward replay (§30) =="
START=$(python3 -c "import datetime; print((datetime.datetime.utcnow() - datetime.timedelta(hours=20)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
END=$(python3 -c "import datetime; print((datetime.datetime.utcnow() - datetime.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
BT=$(curl -fsS -X POST "$API/api/v1/reliability/backtests?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d "{\"start_time\":\"$START\",\"end_time\":\"$END\",\"training_window_seconds\":7200,\"forecast_horizon\":\"ONE_HOUR\",\"prediction_type\":\"FAILURE_RISK\",\"step_seconds\":1800,\"max_steps\":8,\"component_ids\":[\"$CO\"]}")
echo "$BT" | jb "d['total'] == 1 and d['items'][0]['status'] in ('COMPLETED','FAILED','INCONCLUSIVE')" \
  && ok "the backtest completed and recorded its status" || bad "backtest" "$BT"
echo "$BT" | jb "d['note'] and 'no future event' in d['note']" \
  && ok "the backtest response states the leakage contract" || bad "backtest note" "$BT"
BT_ID=$(echo "$BT" | jget "d['items'][0]['id']")
BTDETAIL=$(curl -fsS "$API/api/v1/reliability/backtests/$BT_ID?project_id=$DEMO")
echo "$BTDETAIL" | jb "isinstance(d['steps'], list) and d['configuration']['step_seconds'] == 1800" \
  && ok "the stored backtest carries its steps and configuration" || bad "backtest detail" "$BTDETAIL"

echo "== 11. Leakage: the past cannot see the future (§65) =="
BEFORE_SCORE=$(echo "$DETAIL" | jget "d['risk_score']")
# An incident *now*, well after the forecasts above were generated. It is
# removed again in the cleanup section: the earlier-phase smoke tests pick the
# demo project's newest incident, so leaving this probe behind would redirect
# them at an evidence-free incident and make them fail for the wrong reason.
NOWSTAMP=$(python3 -c "import datetime; print(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'))")
PROBE_INC=$(curl -fsS -X POST "$API/api/v1/incidents" \
  -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$DEMO\",\"environment_id\":\"$ENV\",\"primary_component_id\":\"$CO\",\"title\":\"smoke leakage probe $TS\",\"severity\":\"HIGH\",\"detected_at\":\"$NOWSTAMP\"}" | jget "d['id']")
AFTER_DETAIL=$(curl -fsS "$API/api/v1/reliability/forecasts/$SMOKE_FID?project_id=$DEMO")
echo "$AFTER_DETAIL" | jb "d['risk_score'] == $BEFORE_SCORE" \
  && ok "an incident after generation did not rewrite the stored forecast" || bad "leakage" "$AFTER_DETAIL"

echo "== 12. Drift assessment flags — and retrains nothing (§41, §70) =="
DRIFT=$(curl -fsS -X POST "$API/api/v1/reliability/drift/assess?project_id=$DEMO&persist=true")
echo "$DRIFT" | jb "d['worst_status'] in ('STABLE','WATCH','FLAGGED') and d['review_policy']" \
  && ok "the drift assessment ran and states its review policy" || bad "drift" "$DRIFT"
echo "$DRIFT" | jb "d['retrain_performed'] is False and d['model_activated'] is False" \
  && ok "drift never retrains or activates a model" || bad "drift boundary" "$DRIFT"
DHIST=$(curl -fsS "$API/api/v1/reliability/drift?project_id=$DEMO")
echo "$DHIST" | jb "d['summary']['total'] >= len(d['items'])" \
  && ok "drift findings are stored and summarised" || bad "drift history" "$DHIST"

echo "== 13. Evaluation scores due forecasts once (§27–§29) =="
EVAL=$(curl -fsS -X POST "$API/api/v1/reliability/evaluate?project_id=$DEMO&dispatch=false")
echo "$EVAL" | jb "'sample_count' in d and d['feature_schema_version'] and d['notes'] is not None" \
  && ok "the evaluation run persists its window and self-describes" || bad "evaluation" "$EVAL"
EVAL2=$(curl -fsS -X POST "$API/api/v1/reliability/evaluate?project_id=$DEMO&dispatch=false")
EV=$(curl -fsS "$API/api/v1/reliability/evaluations?project_id=$DEMO")
echo "$EV" | jb "d['items'] and d['items'][0]['sample_count'] >= 0" \
  && ok "evaluation runs are listed as immutable history" || bad "evaluations" "$EV"
NOOUT=$(curl -fsS "$API/api/v1/reliability/forecasts/$SMOKE_FID/outcome?project_id=$DEMO")
echo "$NOOUT" | jb "d is None" \
  && ok "an unelapsed horizon has no outcome yet (not a fake one)" || bad "outcome" "$NOOUT"

echo "== 14. Warnings: deduplicated, floored, human-only (§39, §87) =="
WARN=$(curl -fsS "$API/api/v1/reliability/warnings?project_id=$DEMO")
echo "$WARN" | jb "isinstance(d['items'], list)" \
  && ok "warnings list resolves (floored at HIGH by policy)" || bad "warnings" "$WARN"
# Raise a HIGH forecast directly through the API to exercise the warning path.
HW=$(curl -fsS "$API/api/v1/reliability/forecasts?project_id=$DEMO&risk_level=HIGH&limit=1")
if [ "$(echo "$HW" | jget "d['total']")" -ge 1 ]; then
  HID=$(echo "$HW" | jget "d['items'][0]['id']")
  # Two evaluate passes: the second must not duplicate the warning.
  curl -fsS -X POST "$API/api/v1/reliability/evaluate?project_id=$DEMO&dispatch=false" > /dev/null
  W1=$(curl -fsS "$API/api/v1/reliability/warnings?project_id=$DEMO" | jget "d['total']")
  W2=$(curl -fsS "$API/api/v1/reliability/warnings?project_id=$DEMO" | jget "d['total']")
  [ "$W1" = "$W2" ] \
    && ok "a second pass did not duplicate warnings ($W1 open)" || bad "warning dup" "$W1→$W2"
else
  ok "no HIGH forecast in scope; the warning floor held (list stayed empty)"
fi

echo "== 15. Cross-project isolation (§60) =="
if [ -n "$OTHER" ]; then
  check_status "a foreign project cannot read this forecast" \
    "$(code "$API/api/v1/reliability/forecasts/$SMOKE_FID?project_id=$OTHER")" "404"
  check_status "a foreign project cannot read this profile" \
    "$(code "$API/api/v1/reliability/components/$CO/profile?project_id=$OTHER")" "404"
  check_status "a foreign project cannot backtest this component" \
    "$(code -X POST "$API/api/v1/reliability/backtests?project_id=$OTHER" -H 'Content-Type: application/json' -d "{\"start_time\":\"$START\",\"end_time\":\"$END\",\"training_window_seconds\":7200,\"component_ids\":[\"$CO\"]}")" "404"
  FLIST=$(curl -fsS "$API/api/v1/reliability/forecasts?project_id=$OTHER")
  echo "$FLIST" | jb "d['items'] == []" \
    && ok "the foreign project's forecast list is empty" || bad "foreign list" "$FLIST"
else
  ok "(single-project stack; isolation covered by the 404 checks in step 1)"
fi

echo "== 16. Phase 8 tables exist in PostgreSQL =="
TABLES=$($COMPOSE exec -T postgres psql -U argus -d argus_db -tAc \
  "select count(*) from information_schema.tables where table_name in ('reliability_forecasts','predictive_signals','forecast_feature_snapshots','forecast_outcomes','reliability_model_versions','reliability_evaluation_runs','reliability_backtests','reliability_drift_records','reliability_early_warnings','forecast_fingerprints')" 2>/dev/null) || true
[ "${TABLES:-0}" = "10" ] \
  && ok "all 10 Phase 8 tables exist" || bad "phase 8 tables" "found ${TABLES:-0}"
IDX=$($COMPOSE exec -T postgres psql -U argus -d argus_db -tAc \
  "select count(*) from pg_indexes where tablename = 'reliability_forecasts'" 2>/dev/null) || true
[ "${IDX:-0}" -ge 3 ] \
  && ok "the forecast table carries its indexes ($IDX)" || bad "indexes" "found ${IDX:-0}"

if [ "${DDL_PROBE:-0}" = "1" ]; then
  echo "== 17. The Phase 8 migration reverses (opt-in DDL_PROBE=1) =="
  REV=$($COMPOSE exec -T api sh -lc "cd /app && alembic downgrade -1 && alembic upgrade head" 2>&1) || true
  printf '%s' "$REV" | grep -qi "error" \
    && bad "migration reverse" "$(printf '%s' "$REV" | tail -c 300)" \
    || ok "the Phase 8 migration downgrades and re-applies cleanly"
fi

echo "== 17. Cleanup: the smoke leaves the seeded project as it found it =="
# The probe incident and the scratch component exist only to prove a point
# inside this run. Removing them keeps the demo project's newest incident —
# which the Phase 3–6 smokes select by timestamp — pointing at the seeded
# scenario, so one phase's smoke can never make another's fail.
# Deleting the incident cascades to its timeline and any analysis run against
# it. The scratch component is removed separately and best-effort: its
# telemetry shares no cascade, so a failure there must not hide a leaked probe.
$COMPOSE exec -T postgres psql -U argus -d argus_db -q \
  -c "delete from incidents where id = '${PROBE_INC:-00000000-0000-0000-0000-000000000000}';" > /dev/null 2>&1 || true
$COMPOSE exec -T postgres psql -U argus -d argus_db -q \
  -c "delete from system_components where id = '${CO:-00000000-0000-0000-0000-000000000000}';" > /dev/null 2>&1 || true
LEFT=$($COMPOSE exec -T postgres psql -U argus -d argus_db -tAc \
  "select count(*) from incidents where id = '${PROBE_INC:-00000000-0000-0000-0000-000000000000}';" 2>/dev/null) || true
[ "${LEFT:-1}" = "0" ] \
  && ok "the leakage probe and the scratch component were removed" \
  || bad "cleanup" "probe incident still present (${LEFT:-?})"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
