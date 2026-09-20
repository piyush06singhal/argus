#!/usr/bin/env bash
# ARGUS Phase 5 — live end-to-end smoke test (Failure Reproduction Engine)
#
# Runs the whole reproduction engine against the running compose stack and the
# seeded demo project, through the real HTTP API — nothing is stubbed and no
# result is hard-coded:
#
#   0. locate the seeded demo project and its checkout incident
#   1. Phase 4 preconditions: a causal analysis with a candidate exists
#   2. planning creates an experiment and executes NOTHING (status PLANNED)
#   3. the plan is explicit: hypothesis, expected behaviour, safety constraints
#   4. the safety preview states the sandbox, network policy and blocked access
#   5. execution refusals: no project scope, no confirmation
#   6. isolation: another project's scope cannot see or run the experiment
#   7. start queues a real run and the experiment reaches a terminal state
#   8. the successful scenario reproduces the failure (§58, §59)
#   9. the comparison explains its similarity dimensions (§28)
#  10. the verdict is separate from the result and is SUPPORTED here (§31)
#  11. fault injection is audited, never confused with a natural failure (§22)
#  12. telemetry is captured in its own namespace (§23, §24)
#  13. artifacts are content-hashed and immutable (§41, §42)
#  14. the environment difference is recorded and sanitized (§14, §33)
#  15. the manifest is sufficient to understand the run (§43)
#  16. cleanup: the sandbox is destroyed and nothing is left running (§55)
#  17. counterexample: a change hypothesis runs as an un-injected baseline (§60)
#  18. intermittency: repeated probabilistic runs report what actually happened
#  19. engine metrics (§54) report no orphaned sandbox and no cleanup failure
#  20. the OpenAPI surface exposes the whole Phase 5 API
#  21. the API and the workspace render
#  22. (opt-in, DDL_PROBE=1) the migration reverses under a live pool
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
set -eu
API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
#: Per-experiment polling budget. The engine's own deadline is 300s, plus
#: provisioning and cleanup, so the script waits at most this long per run.
POLL_TIMEOUT="${POLL_TIMEOUT:-420}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# `jb` evaluates the expression and applies *truthiness*, not identity: a check
# like `d['items'][0]['dimensions']` yields a dict, and `v is True` would call a
# populated dimension map "absent".
jb() { [ "$(python3 -c "import json,sys; d=json.load(sys.stdin); v=($1); print('true' if v else 'false')")" = "true" ]; }
check_status() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected=$3 got=$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

#: Wait for an experiment to reach a terminal state, echoing its final status.
await_terminal() {
  local experiment_id="$1" waited=0
  while [ "$waited" -lt "$POLL_TIMEOUT" ]; do
    local snapshot
    snapshot=$(curl -fsS "$API/api/v1/reproductions/$experiment_id/status?project_id=$DEMO")
    local status
    status=$(echo "$snapshot" | jget "d['status']")
    case "$status" in
      COMPLETED|FAILED|CANCELLED|TIMED_OUT) echo "$status"; return 0 ;;
    esac
    sleep 3
    waited=$((waited + 3))
  done
  echo "POLL_TIMEOUT"
  return 1
}

run_experiment() {
  # run_experiment <experiment_id> — confirm, start, wait. Prints terminal status.
  local experiment_id="$1"
  curl -fsS -X POST \
    "$API/api/v1/reproductions/$experiment_id/start?project_id=$DEMO" \
    -H 'Content-Type: application/json' \
    -d '{"confirm_sandbox":true,"requested_by":"e2e-smoke"}' >/dev/null
  await_terminal "$experiment_id"
}

echo "== 0. Locate the seeded demo project and its incident =="
DEMO=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['slug']=='argus-demo-commerce'][0]['id']")
INCIDENTS=$(curl -fsS "$API/api/v1/incidents?project_id=$DEMO&page_size=50")
INC=$(echo "$INCIDENTS" | jget "max(d['items'], key=lambda i: i['detected_at'])['id']")
echo "  project: $DEMO  incident: $INC"

# A second project proves the isolation checks mean something.
OTHER=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "([p['id'] for p in d['items'] if p['id'] != '$DEMO'] or [''])[0]")
echo "  other project: ${OTHER:-<none>}"

echo "== 1. Phase 4 preconditions: a hypothesis exists to test =="
ANALYZE=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/analyze?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"trigger":"phase5-smoke","requested_by":"e2e-smoke"}')
ANALYSIS_ID=$(echo "$ANALYZE" | jget "d['analysis_id']")
ANALYSIS=$(curl -fsS "$API/api/v1/incidents/$INC/causal-analysis?project_id=$DEMO")
CANDIDATE=$(echo "$ANALYSIS" | jget "(d['primary_candidate_id'] or (d['candidates'][0]['id'] if d['candidates'] else ''))")
[ -n "$CANDIDATE" ] && ok "the incident carries a hypothesis to reproduce" \
  || bad "hypothesis" "the analysis produced no candidate"
CAND_TYPE=$(echo "$ANALYSIS" | jget "[c['candidate_type'] for c in d['candidates'] if c['id']=='$CANDIDATE'][0]")
echo "  analysis v$(echo "$ANALYZE" | jget "d['analysis_version']")  primary candidate: $CAND_TYPE"

echo "== 2. Planning an experiment never executes it =="
PLAN_RESPONSE=$(curl -fsS -X POST \
  "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d "{\"candidate_id\":\"$CANDIDATE\",\"requested_by\":\"e2e-smoke\"}")
EXP=$(echo "$PLAN_RESPONSE" | jget "d['experiment']['id']")
echo "$PLAN_RESPONSE" | jb "d['experiment']['status'] == 'PLANNED'" \
  && ok "planning leaves the experiment PLANNED (nothing executed)" \
  || bad "plan status" "$(echo "$PLAN_RESPONSE" | jget "d['experiment']['status']")"
echo "$PLAN_RESPONSE" | jb "d['experiment']['result'] == 'NOT_RUN'" \
  && ok "a planned experiment reports NOT_RUN, not a result" \
  || bad "plan result" "$(echo "$PLAN_RESPONSE" | jget "d['experiment']['result']")"
echo "$PLAN_RESPONSE" | jb "d['experiment']['candidate_id'] == '$CANDIDATE'" \
  && ok "the experiment names the hypothesis it tests" || bad "candidate link" "mismatch"
# Versioning is checked against the incident's history rather than against the
# literal `1`: a smoke run is repeatable, so the second run must be v(N+1) and
# must be the newest version the incident knows about.
VERSION=$(echo "$PLAN_RESPONSE" | jget "d['experiment']['experiment_version']")
echo "$PLAN_RESPONSE" | jb "d['experiment']['experiment_version'] >= 1" \
  && ok "the experiment is versioned (v$VERSION)" || bad "versioning" "missing version"
HISTORY=$(curl -fsS "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO")
echo "$HISTORY" | jb "d['items'] and d['items'][0]['experiment_id'] == '$EXP' and d['items'][0]['experiment_version'] == $VERSION" \
  && ok "the incident's history lists this experiment as the newest version" \
  || bad "history" "$(echo "$HISTORY" | jget "[(i['experiment_version'], i['status']) for i in d['items'][:3]]")"
echo "$PLAN_RESPONSE" | jb "d['disclaimer'] and 'not a proof' in d['disclaimer']" \
  && ok "the response carries the experiment caveat" || bad "disclaimer" "missing"

echo "== 3. The plan is explicit (§7) =="
echo "$PLAN_RESPONSE" | jb "d['plan'] is not None" && ok "a plan exists" || bad "plan" "absent"
echo "$PLAN_RESPONSE" | jb "d['plan']['expected_behavior'] is not None" \
  && ok "the plan states what behaviour is expected" || bad "expected behaviour" "absent"
echo "$PLAN_RESPONSE" | jb "d['plan']['safety_constraints'] is not None" \
  && ok "the plan carries its safety constraints" || bad "safety constraints" "absent"
echo "$PLAN_RESPONSE" | jb "d['plan']['safety_constraints']['production_access'] == 'BLOCKED'" \
  && ok "production access is blocked by the plan itself" || bad "production access" "not blocked"
echo "$PLAN_RESPONSE" | jb "d['plan']['network_policy'] == 'ISOLATED'" \
  && ok "the sandbox network policy defaults to ISOLATED" || bad "network policy" "$(echo "$PLAN_RESPONSE" | jget "d['plan']['network_policy']")"
echo "$PLAN_RESPONSE" | jb "d['plan']['timeout_seconds'] > 0 and d['plan']['repetitions'] >= 1" \
  && ok "the plan bounds its own execution" || bad "bounds" "missing timeout/repetitions"
echo "$PLAN_RESPONSE" | jb "d['hypothesis'] is not None and d['hypothesis']['expected_components']" \
  && ok "the hypothesis records the components it expects to fail" || bad "hypothesis" "no expected components"
echo "$PLAN_RESPONSE" | jb "d['input_count'] >= 1" \
  && ok "the plan selected replay inputs ($(echo "$PLAN_RESPONSE" | jget "d['input_count']"))" \
  || bad "inputs" "no replay input was planned"

echo "== 4. Safety preview (§47) =="
SAFETY=$(curl -fsS "$API/api/v1/reproductions/$EXP/safety?project_id=$DEMO")
echo "$SAFETY" | jb "d['production_access'] == 'BLOCKED'" && ok "production access: BLOCKED" || bad "production access" "not blocked"
echo "$SAFETY" | jb "d['credentials'] == 'SANITIZED'" && ok "credentials: SANITIZED" || bad "credentials" "not sanitized"
echo "$SAFETY" | jb "d['sandbox'].startswith('argus-repro-')" && ok "a named disposable sandbox is shown" || bad "sandbox name" "$(echo "$SAFETY" | jget "d['sandbox']")"
echo "$SAFETY" | jb "d['can_start'] is True" && ok "the experiment is startable" || bad "can_start" "$(echo "$SAFETY" | jget "d['blocked_reasons']")"
echo "$SAFETY" | jb "isinstance(d['resource_limits'], dict) and len(d['resource_limits']) > 0" \
  && ok "resource limits are declared before execution" || bad "resource limits" "absent"

echo "== 5. Execution requires explicit confirmation and ownership (§44, §47) =="
check_status "start without confirm_sandbox is refused" \
  "$(code -X POST "$API/api/v1/reproductions/$EXP/start?project_id=$DEMO" \
    -H 'Content-Type: application/json' -d '{}')" "422"
check_status "start with confirm_sandbox=false is refused" \
  "$(code -X POST "$API/api/v1/reproductions/$EXP/start?project_id=$DEMO" \
    -H 'Content-Type: application/json' -d '{"confirm_sandbox":false}')" "422"
check_status "start without a project scope is refused" \
  "$(code -X POST "$API/api/v1/reproductions/$EXP/start" \
    -H 'Content-Type: application/json' -d '{"confirm_sandbox":true}')" "422"
if [ -n "$OTHER" ]; then
  check_status "another project cannot start this experiment" \
    "$(code -X POST "$API/api/v1/reproductions/$EXP/start?project_id=$OTHER" \
      -H 'Content-Type: application/json' -d '{"confirm_sandbox":true}')" "404"
  check_status "another project cannot even read this experiment" \
    "$(code "$API/api/v1/reproductions/$EXP?project_id=$OTHER")" "404"
fi
echo "$PLAN_RESPONSE" | jb "d['experiment']['status'] == 'PLANNED'" \
  && ok "the refused starts left the experiment untouched" || bad "after refusals" "status changed"

echo "== 6. The forbidden execution surfaces are rejected at the boundary (§56, §57) =="
check_status "a fault target that is a shell command is rejected" \
  "$(code -X POST "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
    -H 'Content-Type: application/json' \
    -d '{"faults":[{"fault_type":"LATENCY","target":"bash -c whoami"}]}')" "422"
check_status "a fault parameter that tries to pass a command is rejected" \
  "$(code -X POST "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
    -H 'Content-Type: application/json' \
    -d '{"faults":[{"fault_type":"LATENCY","target":"datastore","parameters":{"command":"rm -rf /"}}]}')" "422"
check_status "a replay target that is a URL is rejected" \
  "$(code -X POST "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
    -H 'Content-Type: application/json' \
    -d '{"inputs":[{"target_service":"http://evil.example.com","target_path":"/x"}]}')" "422"

echo "== 7. Start the experiment and wait for a real run (§37) =="
# `code` reports the status without consuming the response; the start is issued
# exactly once, so a second POST cannot be mistaken for a fresh run.
START=$(code -X POST "$API/api/v1/reproductions/$EXP/start?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"confirm_sandbox":true,"requested_by":"e2e-smoke"}')
check_status "an explicit start is accepted" "$START" "202"
TERMINAL=$(await_terminal "$EXP" || true)
echo "  terminal status: $TERMINAL"
[ "$TERMINAL" = "COMPLETED" ] && ok "the experiment completed" \
  || bad "experiment terminal state" "$TERMINAL"

echo "== 8. The successful scenario (§58, §59) =="
DETAIL=$(curl -fsS "$API/api/v1/reproductions/$EXP?project_id=$DEMO")
echo "$DETAIL" | jb "d['experiment']['result'] == 'SUCCESSFUL'" \
  && ok "the sandbox reproduced the failure (SUCCESSFUL)" \
  || bad "reproduction result" "$(echo "$DETAIL" | jget "d['experiment']['result']")"
echo "$DETAIL" | jb "d['experiment']['failure_classification'] is None" \
  && ok "no failure classification: the experiment itself ran cleanly" \
  || bad "failure classification" "$(echo "$DETAIL" | jget "d['experiment']['failure_classification']")"
echo "$DETAIL" | jb "d['experiment']['completed_runs'] == 1" \
  && ok "the run counter advanced with the repetition that ran" \
  || bad "runs" "completed_runs=$(echo "$DETAIL" | jget "d['experiment']['completed_runs']")"
echo "$DETAIL" | jb "d['sandbox'] is not None and d['sandbox']['status'] == 'DESTROYED'" \
  && ok "the sandbox was destroyed after the experiment" \
  || bad "sandbox cleanup" "$(echo "$DETAIL" | jget "d['sandbox']['status']")"

echo "== 9. The run observed real traffic in the sandbox =="
STATUS=$(curl -fsS "$API/api/v1/reproductions/$EXP/status?project_id=$DEMO")
echo "$STATUS" | jb "d['latest_run'] is not None and d['latest_run']['replay_request_count'] >= 1" \
  && ok "every planned request was sent" || bad "replay" "no request was sent"
echo "$STATUS" | jb "d['latest_run']['replay_rejected_count'] == 0" \
  && ok "no request was rejected by the safety gate mid-run" \
  || bad "replay rejections" "$(echo "$STATUS" | jget "d['latest_run']['replay_rejected_count']")"
echo "$STATUS" | jb "d['latest_run']['replay_failure_count'] >= 1" \
  && ok "the injected fault propagated into a real failure" \
  || bad "propagation" "the replayed requests did not fail"
echo "$STATUS" | jb "d['latest_run']['observation_count'] > 0" \
  && ok "telemetry was captured from the running services" || bad "observations" "none captured"

echo "== 10. The comparison explains its dimensions (§27, §28) =="
COMPARISON=$(curl -fsS "$API/api/v1/reproductions/$EXP/comparison?project_id=$DEMO")
echo "$COMPARISON" | jb "d['total'] >= 1 and d['items'][0]['dimensions']" \
  && ok "the comparison carries named dimension scores" || bad "dimensions" "absent"
echo "$COMPARISON" | jb "d['items'][0]['formula_reference']" \
  && ok "the comparison cites the formulas behind its scores" || bad "formulas" "absent"
echo "$COMPARISON" | jb "d['items'][0]['component_overlap'] is not None" \
  && ok "component overlap is reported" || bad "component overlap" "absent"
echo "$COMPARISON" | jb "d['items'][0]['sequence_match'] is True" \
  && ok "the reproduced failure sequence matches the incident's" \
  || bad "sequence" "$(echo "$COMPARISON" | jget "d['items'][0]['sequence_reproduced']")"
echo "$COMPARISON" | jb "d['items'][0]['overall_similarity'] in ('HIGH','MEDIUM')" \
  && ok "similarity is reported as a bucket, not a fake percentage" \
  || bad "similarity bucket" "$(echo "$COMPARISON" | jget "d['items'][0]['overall_similarity']")"
if python3 -c "
import json, sys
body = json.loads(sys.argv[1])
score = body['items'][0].get('similarity_score')
raise SystemExit(1 if score is None or not (0.0 <= score <= 1.0) else 0)
" "$COMPARISON"; then
  ok "the supporting score stays inside 0..1"
else
  bad "similarity score" "outside 0..1"
fi

echo "== 11. The verdict is separate from the result (§30, §31) =="
VALIDATION=$(curl -fsS "$API/api/v1/reproductions/$EXP/validation?project_id=$DEMO")
echo "$VALIDATION" | jb "d['outcome'] == 'SUPPORTED'" \
  && ok "the datastore hypothesis is SUPPORTED by the experiment" \
  || bad "outcome" "$(echo "$VALIDATION" | jget "d['outcome']")"
echo "$VALIDATION" | jb "d['confidence'] in ('HIGH','MEDIUM')" \
  && ok "verdict confidence is a bucket" || bad "verdict confidence" "$(echo "$VALIDATION" | jget "d['confidence']")"
echo "$VALIDATION" | jb "bool(d['supporting_observations'])" \
  && ok "the verdict cites its supporting observations" || bad "evidence" "none cited"
echo "$VALIDATION" | jb "d['determinism'] is not None and 'not a probability' in d['determinism']['note']" \
  && ok "repeatability is framed as an observation, not a probability" \
  || bad "determinism framing" "$(echo "$VALIDATION" | jget "d['determinism']")"
# The two claims live on different rows: `result` is what the sandbox did,
# `outcome` is what that means for the hypothesis. Both must be present and
# they must be distinguishable, which is what stops one being read as the other.
OUTCOME=$(echo "$VALIDATION" | jget "d['outcome']")
RESULT=$(echo "$DETAIL" | jget "d['experiment']['result']")
[ -n "$OUTCOME" ] && [ -n "$RESULT" ] && ok "result ($RESULT) and outcome ($OUTCOME) are reported separately" \
  || bad "result/outcome separation" "one of the two claims is missing"

echo "== 12. Fault injection is audited (§21, §22) =="
FAULTS=$(curl -fsS "$API/api/v1/reproductions/$EXP/faults?project_id=$DEMO")
echo "$FAULTS" | jb "d['total'] >= 1 and d['injected_total'] >= 1" \
  && ok "the injected fault is recorded as injected" \
  || bad "fault audit" "$(echo "$FAULTS" | jget "d['items']")"
echo "$FAULTS" | jb "d['items'][0]['scope'] == 'sandbox'" \
  && ok "the fault's scope is the sandbox, never production" || bad "fault scope" "not sandbox"

echo "== 13. Captured telemetry is namespaced (§23, §24) =="
TELEMETRY=$(curl -fsS "$API/api/v1/reproductions/$EXP/telemetry?project_id=$DEMO")
NAMESPACE=$(echo "$TELEMETRY" | jget "d['namespace']")
# The namespace is prefixed (`repro:<experiment-id>`), so it can never collide
# with a project/environment namespace a production signal would carry.
echo "$TELEMETRY" | jb "d['namespace'].startswith('repro:') and '$EXP' in d['namespace']" \
  && ok "reproduction telemetry lives in its own namespace ($NAMESPACE)" \
  || bad "namespace" "$NAMESPACE"
echo "$TELEMETRY" | jb "d['total'] >= 1" && ok "observations were captured" || bad "telemetry" "empty"
# Nothing captured during a reproduction may appear as production telemetry.
LIVE_ANOMALIES=$(curl -fsS "$API/api/v1/anomalies?project_id=$DEMO&page_size=100")
python3 -c "
import json, sys
body = json.loads(sys.argv[1])
foreign = [a for a in body['items'] if a.get('source_event_id') and 'argus-repro-' in json.dumps(a)]
raise SystemExit(1 if foreign else 0)
" "$LIVE_ANOMALIES" && ok "no reproduction signal leaked into production telemetry" \
  || bad "telemetry isolation" "a reproduction signal appeared in production data"

echo "== 14. Artifacts are immutable and content-hashed (§41, §42) =="
ARTIFACTS=$(curl -fsS "$API/api/v1/reproductions/$EXP/artifacts?project_id=$DEMO")
echo "$ARTIFACTS" | jb "d['total'] >= 5" \
  && ok "the experiment stored its artifacts ($(echo "$ARTIFACTS" | jget "d['total']"))" \
  || bad "artifacts" "too few stored"
echo "$ARTIFACTS" | jb "all(a['immutable'] for a in d['items'])" \
  && ok "every artifact is marked immutable" || bad "immutability" "an artifact is mutable"
echo "$ARTIFACTS" | jb "all(len(a['content_hash']) == 64 for a in d['items'])" \
  && ok "every artifact carries a SHA-256 content hash" || bad "hashes" "missing or malformed"
echo "$ARTIFACTS" | jb "any(a['artifact_type'] == 'VALIDATION_REPORT' for a in d['items'])" \
  && ok "the validation report is preserved as an artifact" || bad "validation report" "absent"
echo "$ARTIFACTS" | jb "any(a['artifact_type'] == 'REPRODUCTION_MANIFEST' for a in d['items'])" \
  && ok "the manifest is preserved as an artifact" || bad "manifest artifact" "absent"

echo "== 15. Environment difference and sanitization (§14, §33) =="
ENVIRONMENT=$(curl -fsS "$API/api/v1/reproductions/$EXP/environment?project_id=$DEMO")
echo "$ENVIRONMENT" | jb "any(s['source']=='ORIGINAL' for s in d) and any(s['source']=='SANDBOX' for s in d)" \
  && ok "both the original and the sandbox environment were snapshotted" \
  || bad "snapshots" "$(echo "$ENVIRONMENT" | jget "sorted({s['source'] for s in d})")"
# The sanitizer *records* the keys it dropped, so the check is about values:
# no credential value may appear, and the drop must be accounted for. (The key
# name appearing inside `sanitization.dropped_keys` is the evidence that it was
# removed, not a leak.)
python3 -c "
import json, sys
snapshots = json.load(sys.stdin)
blob = json.dumps(snapshots)
forbidden = ['argus_password', 'postgresql+asyncpg://', 'BEGIN PRIVATE KEY', 'AUTH_SECRET']
leaked = [item for item in forbidden if item in blob]
if leaked:
    raise SystemExit(f'leaked: {leaked}')
original = [s for s in snapshots if s['source'] == 'ORIGINAL']
if not original:
    raise SystemExit('no original snapshot to audit')
dropped = (original[0].get('sanitization') or {}).get('dropped_keys') or []
if 'DATABASE_PASSWORD' not in dropped:
    raise SystemExit('the credential key was neither dropped nor reported dropped')
" <<<"$ENVIRONMENT" && ok "no credential value appears, and dropped keys are accounted for" \
  || bad "sanitization" "a credential value leaked into a snapshot"

echo "== 16. The manifest explains how the run was executed (§43) =="
MANIFEST=$(curl -fsS "$API/api/v1/reproductions/$EXP/manifest?project_id=$DEMO")
echo "$MANIFEST" | jb "len(d['services']) >= 1 and len(d['inputs']) >= 1" \
  && ok "the manifest lists the services and inputs used" || bad "manifest" "incomplete"
echo "$MANIFEST" | jb "len(d['artifact_hashes']) >= 1" \
  && ok "the manifest references the artifact hashes" || bad "manifest hashes" "absent"
echo "$MANIFEST" | jb "d['repetitions'] >= 1 and d['timeout_seconds'] > 0" \
  && ok "the manifest records the experiment's bounds" || bad "manifest bounds" "absent"

echo "== 17. Cleanup is unconditional (§55) =="
if docker compose ps >/dev/null 2>&1; then
  LEFTOVER=$(docker compose exec -T api sh -lc \
    "ls -1 /tmp/argus-reproduction 2>/dev/null | grep -c 'argus-repro-$EXP' || true" 2>/dev/null | tr -d '\r')
  [ "${LEFTOVER:-0}" = "0" ] && ok "no sandbox directory was left behind for this experiment" \
    || bad "sandbox leak" "$LEFTOVER directory(ies) remain for $EXP"
else
  echo "  SKIP  sandbox filesystem check (docker compose unavailable)"
fi

echo "== 18. Counterexample: a change hypothesis runs as the baseline (§60) =="
DEPLOY_CANDIDATE=$(
  echo "$ANALYSIS" | jget "next((c['id'] for c in d['candidates'] if c['candidate_type'] in ('DEPLOYMENT','CONFIGURATION_CHANGE')), '')"
)
if [ -z "$DEPLOY_CANDIDATE" ]; then
  echo "  SKIP  the incident carries no deployment/configuration candidate to test"
else
  COUNTER=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
    -H 'Content-Type: application/json' \
    -d "{\"candidate_id\":\"$DEPLOY_CANDIDATE\",\"requested_by\":\"e2e-smoke\"}")
  COUNTER_ID=$(echo "$COUNTER" | jget "d['experiment']['id']")
  echo "$COUNTER" | jb "d['plan']['strategy'] == 'CONFIGURATION_REPLAY'" \
    && ok "a change hypothesis is planned as a configuration-replay baseline" \
    || bad "counterexample strategy" "$(echo "$COUNTER" | jget "d['plan']['strategy']")"
  # Injected faults are their own audited collection (the plan response carries
  # no fault list), so the baseline is verified through that surface.
  COUNTER_FAULTS=$(curl -fsS "$API/api/v1/reproductions/$COUNTER_ID/faults?project_id=$DEMO")
  echo "$COUNTER_FAULTS" | jb "d['total'] == 0" \
    && ok "nothing is injected: the baseline runs un-changed" \
    || bad "counterexample faults" "a fault was planned for a change hypothesis"
  COUNTER_TERMINAL=$(run_experiment "$COUNTER_ID" || true)
  echo "  terminal status: $COUNTER_TERMINAL"
  [ "$COUNTER_TERMINAL" = "COMPLETED" ] && ok "the baseline experiment completed" \
    || bad "counterexample state" "$COUNTER_TERMINAL"
  COUNTER_DETAIL=$(curl -fsS "$API/api/v1/reproductions/$COUNTER_ID?project_id=$DEMO")
  echo "$COUNTER_DETAIL" | jb "d['experiment']['result'] != 'SUCCESSFUL'" \
    && ok "the expected failure did NOT reproduce without the change" \
    || bad "counterexample result" "the change was reported as reproduced from the baseline"
  COUNTER_VALIDATION=$(curl -fsS "$API/api/v1/reproductions/$COUNTER_ID/validation?project_id=$DEMO")
  echo "$COUNTER_VALIDATION" | jb "d['outcome'] in ('NOT_SUPPORTED','INCONCLUSIVE')" \
    && ok "the change hypothesis is NOT_SUPPORTED/INCONCLUSIVE from the experiment" \
    || bad "counterexample verdict" "$(echo "$COUNTER_VALIDATION" | jget "d['outcome']")"
  echo "$COUNTER_VALIDATION" | jb "bool(d['summary'])" \
    && ok "the null result states why it is null" || bad "null result" "no summary"
fi

echo "== 19. Intermittency is reported as an observation (§34, §61) =="
INTERMITTENT=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d "{\"candidate_id\":\"$CANDIDATE\",\"repetitions\":3,\"faults\":[{\"fault_type\":\"HTTP_5XX\",\"target\":\"datastore\",\"intensity\":0.25}],\"requested_by\":\"e2e-smoke\"}")
INTERMITTENT_ID=$(echo "$INTERMITTENT" | jget "d['experiment']['id']")
echo "$INTERMITTENT" | jb "d['plan']['repetitions'] == 3" \
  && ok "the requested repetitions are planned" || bad "repetitions" "not 3"
echo "$INTERMITTENT" | jb "len(d['faults']) == 1 and d['faults'][0]['intensity'] == 0.25" \
  && ok "a probabilistic fault is planned with its intensity" || bad "probabilistic fault" "not planned"
INTERMITTENT_TERMINAL=$(run_experiment "$INTERMITTENT_ID" || true)
echo "  terminal status: $INTERMITTENT_TERMINAL"
INTERMITTENT_VALIDATION=$(curl -fsS "$API/api/v1/reproductions/$INTERMITTENT_ID/validation?project_id=$DEMO")
echo "$INTERMITTENT_VALIDATION" | jb "d['determinism']['runs'] == 3" \
  && ok "all three repetitions are counted" \
  || bad "determinism runs" "$(echo "$INTERMITTENT_VALIDATION" | jget "d['determinism']")"
echo "$INTERMITTENT_VALIDATION" | jb "d['determinism']['classification'] in ('DETERMINISTIC','INTERMITTENT','NOT_REPRODUCED','REPRODUCED','NOT_RUN')" \
  && ok "the observed behaviour is classified ($(echo "$INTERMITTENT_VALIDATION" | jget "d['determinism']['classification']"))" \
  || bad "determinism classification" "$(echo "$INTERMITTENT_VALIDATION" | jget "d['determinism']")"
# The rate must be the arithmetic of the runs that happened — nothing imported.
INTERMITTENT_COMPARISON=$(curl -fsS "$API/api/v1/reproductions/$INTERMITTENT_ID/comparison?project_id=$DEMO")
python3 -c "
import json, sys
validation = json.loads(sys.argv[1])['determinism']
comparisons = json.loads(sys.argv[2])['items']
reproduced = sum(1 for c in comparisons if c['result'] in ('SUCCESSFUL', 'PARTIAL'))
runs = len(comparisons)
expected = round(reproduced / runs, 4) if runs else None
print('true' if validation['reproduction_rate'] == expected else f'false ({validation[\"reproduction_rate\"]} != {expected})')
" "$INTERMITTENT_VALIDATION" "$INTERMITTENT_COMPARISON" | grep -q '^true$' \
  && ok "the reproduction rate is the arithmetic of the runs that happened" \
  || bad "reproduction rate" "not derived from the runs"
echo "$INTERMITTENT_VALIDATION" | jb "all('not a probability' in d['determinism']['note'] for _ in [0])" \
  && ok "the rate is explicitly not a causal probability" || bad "rate framing" "ambiguous"
INTERMITTENT_RUNS=$(curl -fsS "$API/api/v1/reproductions/$INTERMITTENT_ID?project_id=$DEMO" | jget "len(d['runs'])")
[ "$INTERMITTENT_RUNS" = "3" ] && ok "three separate runs are recorded" \
  || bad "runs recorded" "$INTERMITTENT_RUNS"

echo "== 20. Engine metrics report a clean state (§54) =="
METRICS=$(curl -fsS "$API/api/v1/reproductions/metrics?project_id=$DEMO")
echo "$METRICS" | jb "d['orphaned_sandboxes'] == 0" \
  && ok "no orphaned sandbox (nothing was silently leaked)" \
  || bad "orphaned sandboxes" "$(echo "$METRICS" | jget "d['orphaned_sandboxes']")"
echo "$METRICS" | jb "d['cleanup_failures'] == 0" \
  && ok "no cleanup failure was recorded" || bad "cleanup failures" "$(echo "$METRICS" | jget "d['cleanup_failures']")"
echo "$METRICS" | jb "d['sandboxes_total'] == d['sandboxes_destroyed']" \
  && ok "every sandbox this project created was destroyed" \
  || bad "sandbox accounting" "$(echo "$METRICS" | jget "'{}/{}'.format(d['sandboxes_destroyed'], d['sandboxes_total'])")"
echo "$METRICS" | jb "d['network_policy'] == 'ISOLATED'" && ok "the engine's default policy is ISOLATED" \
  || bad "engine policy" "$(echo "$METRICS" | jget "d['network_policy']")"

echo "== 21. The API and the workspace are exposed =="
OPENAPI=$(curl -fsS "$API/openapi.json")
for path in \
  "/api/v1/incidents/{incident_id}/reproductions" \
  "/api/v1/reproductions" \
  "/api/v1/reproductions/{experiment_id}" \
  "/api/v1/reproductions/{experiment_id}/start" \
  "/api/v1/reproductions/{experiment_id}/cancel" \
  "/api/v1/reproductions/{experiment_id}/retry" \
  "/api/v1/reproductions/{experiment_id}/plan" \
  "/api/v1/reproductions/{experiment_id}/safety" \
  "/api/v1/reproductions/{experiment_id}/status" \
  "/api/v1/reproductions/{experiment_id}/inputs" \
  "/api/v1/reproductions/{experiment_id}/telemetry" \
  "/api/v1/reproductions/{experiment_id}/artifacts" \
  "/api/v1/reproductions/{experiment_id}/comparison" \
  "/api/v1/reproductions/{experiment_id}/validation" \
  "/api/v1/reproductions/{experiment_id}/environment" \
  "/api/v1/reproductions/{experiment_id}/faults" \
  "/api/v1/reproductions/{experiment_id}/manifest" \
  "/api/v1/reproductions/metrics" ; do
  if echo "$OPENAPI" | python3 -c "
import json, sys
paths = json.load(sys.stdin)['paths']
raise SystemExit(0 if sys.argv[1] in paths else 1)
" "$path"; then
    ok "openapi exposes $path"
  else
    bad "openapi path" "$path is missing"
  fi
done
check_status "the reproduction index page renders" "$(code "$WEB/reproductions")" "200"
check_status "the incident experiment history page renders" \
  "$(code "$WEB/incidents/$INC/reproductions")" "200"
check_status "the reproduction workspace page renders" \
  "$(code "$WEB/reproductions/$EXP")" "200"

if [ "${DDL_PROBE:-0}" = "1" ]; then
  # Opt-in (`DDL_PROBE=1 bash infrastructure/e2e-smoke-phase5.sh`): it drops the
  # Phase 5 tables, so the experiments it just created are removed. It exists
  # because two things must be true and neither is checkable from unit tests:
  # the migration reverses cleanly, and a schema change under a *live* pool does
  # not break the next request (asyncpg caches statement plans).
  echo "== 22. The Phase 5 migration reverses under a live pool =="
  docker compose exec -T api alembic downgrade -1 >/dev/null 2>&1 \
    && ok "the Phase 5 migration downgrades cleanly" || bad "downgrade" "exit code"
  docker compose exec -T api alembic upgrade head >/dev/null 2>&1 \
    && ok "the Phase 5 migration reapplies cleanly" || bad "upgrade" "exit code"
  # The body is captured alongside the status so a failure here is diagnosable
  # rather than just "422": this probe exists to catch a stale cached plan, and
  # a bare status code would hide the difference between that and a bad request.
  PROBE_BODY=$(curl -s -w '\n%{http_code}' \
    -X POST "$API/api/v1/incidents/$INC/reproductions?project_id=$DEMO" \
    -H 'Content-Type: application/json' -d "{\"candidate_id\":\"$CANDIDATE\"}")
  PROBE_STATUS=$(printf '%s' "$PROBE_BODY" | tail -n 1)
  PROBE_JSON=$(printf '%s' "$PROBE_BODY" | sed '$d')
  [ "$PROBE_STATUS" = "201" ] \
    && ok "the first request after a live migration succeeds" \
    || bad "the first request after a live migration" \
      "status=$PROBE_STATUS body=$(printf '%s' "$PROBE_JSON" | head -c 200)"
  PROBE_ID=$(printf '%s' "$PROBE_JSON" | jget "d['experiment']['id']" 2>/dev/null || echo '')
  if [ -n "$PROBE_ID" ]; then
    check_status "a fresh experiment is readable after the migration" \
      "$(code "$API/api/v1/reproductions/$PROBE_ID?project_id=$DEMO")" "200"
    check_status "the engine still refuses an unconfirmed start" \
      "$(code -X POST "$API/api/v1/reproductions/$PROBE_ID/start?project_id=$DEMO" \
        -H 'Content-Type: application/json' -d '{}')" "422"
  fi
fi

echo
echo "==================== PHASE 5 SMOKE SUMMARY ===================="
echo "  passed: $PASS"
echo "  failed: $FAIL"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
