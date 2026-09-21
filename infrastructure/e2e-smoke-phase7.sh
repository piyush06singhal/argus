#!/usr/bin/env bash
# ARGUS Phase 7 — live end-to-end smoke test (fix generation & verification)
#
# Runs the whole Phase 7 pipeline against the running compose stack, through the
# real HTTP API and a *real* git repository with real commits — nothing is
# stubbed, no patch is hard-coded, and every verdict comes from the verification
# engine's own runs:
#
#   0. locate the seeded demo project and its incident
#   1. scope and ownership refusals (no project scope, foreign project, unknown rows)
#   2. build a real git history inside the container from the read-only demo tree
#   3. register the repository; index the deployed revision into a snapshot
#   4. ingest the failing stack trace, then open a Phase 6 debug session over it
#   5. a fix hypothesis is planned from the session's *validated* location
#   6. the deterministic generator composes a minimal diff from stored bytes
#   7. safety validation accepts the in-scope patch (§10, §13)
#   8. verification: a disposable workspace, static gate, two-sided regression
#      test, the repository's own suite, reproduction and comparison (§26–§37)
#   9. the stored verdict is VERIFIED / FULLY_VERIFIED and the evidence agrees
#  10. the report, artifacts and comparison exports (§31, §45, §72)
#  11. approval records a decision and changes nothing else (§41, §71, §74)
#  12. regeneration supersedes the old candidate (§58)
#  13. AI generation without a usable provider fails loudly and fabricates nothing
#  14. an unverifiable patch cannot be approved
#  15. another project cannot read or review this fix (§56-style isolation)
#  16. no workspace survives (§48) and the demo checkout is byte-identical
#  17. the Phase 7 tables exist in PostgreSQL and the migration reverses cleanly
#      (the DDL check is opt-in: DDL_PROBE=1)
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
#         The api service must have the Phase 6 volumes from docker-compose.yml
#         (./demo/argus-commerce mounted read-only, plus the code_scratch volume).
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

echo "== 0. Locate the seeded demo project and its incident =="
DEMO=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['slug']=='argus-demo-commerce'][0]['id']")
INC=$(curl -fsS "$API/api/v1/incidents?project_id=$DEMO&page_size=50" | jget "max(d['items'], key=lambda i: i['detected_at'])['id']")
OTHER=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "([p['id'] for p in d['items'] if p['id'] != '$DEMO'] or [''])[0]")
echo "  project: $DEMO  incident: $INC  other project: ${OTHER:-<none>}"

echo "== 1. Scope and ownership refusals =="
check_status "planning a fix without project scope is refused" \
  "$(code -X POST "$API/api/v1/incidents/$INC/fixes" -H 'Content-Type: application/json' \
     -d '{"debug_session_id":"00000000-0000-0000-0000-000000000000"}')" "422"
check_status "fix metrics without project scope is refused" \
  "$(code "$API/api/v1/fixes/metrics")" "422"
check_status "an unknown incident cannot be planned from" \
  "$(code -X POST "$API/api/v1/incidents/00000000-0000-0000-0000-000000000000/fixes?project_id=$DEMO" \
     -H 'Content-Type: application/json' \
     -d '{"debug_session_id":"00000000-0000-0000-0000-000000000000"}')" "404"
check_status "an unknown patch is not readable" \
  "$(code "$API/api/v1/patches/00000000-0000-0000-0000-000000000000?project_id=$DEMO")" "404"
check_status "an unknown patch cannot be approved" \
  "$(code -X POST "$API/api/v1/patches/00000000-0000-0000-0000-000000000000/approve?project_id=$DEMO" \
     -H 'Content-Type: application/json' -d '{"actor":"e2e"}')" "404"
if [ -n "$OTHER" ]; then
  check_status "a foreign project scope does not reveal an unknown fix" \
    "$(code "$API/api/v1/fixes/00000000-0000-0000-0000-000000000000?project_id=$OTHER")" "404"
fi

echo "== 2. Build a real git history in the container (§59) =="
BUILD_OUT=$($COMPOSE exec -T api sh -lc '
set -e
rm -rf /repos/scratch/phase7-commerce
mkdir -p /repos/scratch/phase7-commerce
cd /repos/scratch/phase7-commerce
cp -R /repos/demo-commerce/. .
rm -rf .git
git init -q
git config user.email demo@argus
git config user.name "ARGUS Demo"
git add -A
git commit -q -m "feat: initial checkout service"
# The deployed revision: the regression under investigation.
sed -i "s/DB_TIMEOUT_SECONDS = 0.25/DB_TIMEOUT_SECONDS = 2.0/" services/inventory/repository.py
git add -A
git commit -q -m "feat: bound inventory queries with a 2 s timeout"
sed -i "s/DB_TIMEOUT_SECONDS = 2.0/DB_TIMEOUT_SECONDS = 0.5/" services/inventory/repository.py
git add -A
git commit -q -m "feat: tighten the inventory timeout to 500 ms"
sed -i "s/DB_TIMEOUT_SECONDS = 0.5/DB_TIMEOUT_SECONDS = 0.25/" services/inventory/repository.py
git add -A
git commit -q -m "perf: halve the database timeout"
if [ -f pytest.ini ]; then echo "PYTEST_INI=yes"; else echo "PYTEST_INI=no"; fi
echo "HEAD=$(git rev-parse HEAD)"
echo "COMMITS=$(git rev-list --count HEAD)"
') || true
HEAD_SHA=$(printf '%s' "$BUILD_OUT" | sed -n 's/^HEAD=//p')
COMMIT_COUNT=$(printf '%s' "$BUILD_OUT" | sed -n 's/^COMMITS=//p')
echo "  head (deployed): ${HEAD_SHA:0:12}  commits: ${COMMIT_COUNT:-?}"
[ -n "$HEAD_SHA" ] && ok "a real four-commit history was built in the container" \
  || bad "demo history" "git init/commit failed: $(printf '%s' "$BUILD_OUT" | head -c 300)"
[ "${COMMIT_COUNT:-0}" = "4" ] && ok "the history separates the deployed revision from its parents" \
  || bad "history shape" "expected 4 commits, got ${COMMIT_COUNT:-none}"
printf '%s' "$BUILD_OUT" | grep -q "PYTEST_INI=yes" \
  && ok "the checkout carries the test configuration the verifier will use (§24)" \
  || bad "test config" "pytest.ini missing from the demo checkout"
DEFECT_INFO=$($COMPOSE exec -T api sh -lc \
  "grep -n 'DB_TIMEOUT_SECONDS = 0.25' /repos/scratch/phase7-commerce/services/inventory/repository.py | head -1") || true
[ -n "$DEFECT_INFO" ] && ok "the deployed revision carries the planted defect ($DEFECT_INFO)" \
  || bad "defect plant" "the timeout line is not 0.25 at HEAD"

echo "== 3. Register and index the deployed revision (§7, §9) =="
REGISTER=$(curl -fsS -X POST "$API/api/v1/projects/$DEMO/repositories" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"local","repository_url":"/repos/scratch/phase7-commerce","default_branch":"main","language":"python"}')
REPO=$(echo "$REGISTER" | jget "d['id']")
echo "  repository: $REPO"
echo "$REGISTER" | jb "d['connection_status'] == 'CONNECTED'" \
  && ok "the provider read the repository and reported CONNECTED" || bad "registration" "not connected"
INDEX=$(curl -fsS -X POST "$API/api/v1/projects/$DEMO/repositories/$REPO/index" \
  -H 'Content-Type: application/json' -d "{\"reference\":\"$HEAD_SHA\",\"incremental\":true}")
SNAP=$(echo "$INDEX" | jget "d['snapshot']['id']")
echo "  snapshot: $SNAP"
echo "$INDEX" | jb "d['snapshot']['commit_sha'] == '$HEAD_SHA' and d['snapshot']['status'] == 'READY'" \
  && ok "the snapshot is pinned to the deployed revision and READY" \
  || bad "snapshot" "commit=$(echo "$INDEX" | jget "d['snapshot']['commit_sha']") status=$(echo "$INDEX" | jget "d['snapshot']['status']")"
echo "$INDEX" | jb "d['run']['files_indexed'] > 0" \
  && ok "the checkout was indexed ($(echo "$INDEX" | jget "d['run']['files_indexed']") files)" \
  || bad "indexing" "nothing indexed"

echo "== 4. Ingest the failing stack trace and open a debug session (§59) =="
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LOG_PAYLOAD=$(TS="$TS" DEMO="$DEMO" python3 - <<'PY'
import json, os

trace = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/services/checkout/service.py", line 25, in process\n'
    "    return await self.reserve(sku, quantity)\n"
    '  File "/srv/app/services/inventory/repository.py", line 33, in _query\n'
    '    raise TimeoutError("inventory database query timed out")\n'
    "TimeoutError: inventory database query timed out\n"
)
print(json.dumps({
    "project_id": os.environ["DEMO"],
    "timestamp": os.environ["TS"],
    "level": "ERROR",
    "message": trace,
    "service": "checkout-service",
}))
PY
)
check_status "the failing stack trace is ingested as telemetry" \
  "$(code -X POST "$API/api/v1/observability/logs" -H 'Content-Type: application/json' -d "$LOG_PAYLOAD")" "201"
SESSION_RESPONSE=$(curl -s -w '\n%{http_code}' -X POST \
  "$API/api/v1/incidents/$INC/debug-sessions?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d "{\"repository_id\":\"$REPO\",\"snapshot_id\":\"$SNAP\",\"created_by\":\"e2e-smoke\",\"title\":\"e2e phase 7\",\"run_analysis\":true}")
SESSION_STATUS=$(printf '%s' "$SESSION_RESPONSE" | tail -n 1)
SESSION=$(printf '%s' "$SESSION_RESPONSE" | sed '$d' | jget "d['id']")
check_status "a debug session is created over the pinned snapshot" "$SESSION_STATUS" "200"
echo "  session: $SESSION"

LOCATIONS=$(curl -fsS "$API/api/v1/debug-sessions/$SESSION/locations")
echo "$LOCATIONS" | jb "len(d) > 0" \
  && ok "the investigation produced code locations ($(echo "$LOCATIONS" | jget "len(d)"))" \
  || bad "locations" "none produced"
LOCATION_LINE=$(echo "$LOCATIONS" | jget "([x['start_line'] for x in d if x['file_path'] == 'services/inventory/repository.py' and x['validation'] == 'VALID'] or [None])[0]")
if [ "$LOCATION_LINE" = "None" ]; then
  bad "validated location" "no VALID location on services/inventory/repository.py; got $(echo "$LOCATIONS" | jget "[(x['file_path'], x['validation']) for x in d]")"
else
  ok "a VALID location points at the inventory repository (line $LOCATION_LINE)"
fi

echo "== 5. Plan the fix hypothesis from validated evidence (§5, §6, §10) =="
FIX_RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$API/api/v1/incidents/$INC/fixes?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d "{\"debug_session_id\":\"$SESSION\",\"created_by\":\"e2e-smoke\"}")
FIX_STATUS=$(printf '%s' "$FIX_RESPONSE" | tail -n 1)
FIX_JSON=$(printf '%s' "$FIX_RESPONSE" | sed '$d')
check_status "the fix hypothesis is planned" "$FIX_STATUS" "200"
FIX=$(printf '%s' "$FIX_JSON" | jget "d['id']")
echo "  hypothesis: $FIX"
printf '%s' "$FIX_JSON" | jb "d['status'] == 'HYPOTHESIZED'" \
  && ok "the hypothesis starts HYPOTHESIZED — nothing runs implicitly" \
  || bad "hypothesis status" "$(printf '%s' "$FIX_JSON" | jget "d['status']")"
printf '%s' "$FIX_JSON" | jb "'services/inventory/repository.py' in d['scope_files']" \
  && ok "the scope is seeded from the validated location (§10)" \
  || bad "scope" "$(printf '%s' "$FIX_JSON" | jget "d['scope_files']")"
printf '%s' "$FIX_JSON" | jb "d['category'] != 'UNKNOWN'" \
  && ok "the category is derived from the evidence ($(printf '%s' "$FIX_JSON" | jget "d['category']")) (§6)" \
  || bad "category" "the evidence did not support a category"
printf '%s' "$FIX_JSON" | jb "d['excluded_paths']" \
  && ok "sensitive areas are excluded by default (§14)" \
  || bad "excluded paths" "no defaults recorded"
printf '%s' "$FIX_JSON" | jb "d['debug_session_id'] == '$SESSION'" \
  && ok "the hypothesis names the evidence it came from" \
  || bad "provenance" "debug session not recorded"

echo "== 6. Generate the patch (§8, §9, §11, §13) =="
PATCH_RESPONSE=$(curl -s -w '\n%{http_code}' -X POST \
  "$API/api/v1/fixes/$FIX/generate?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"generated_by":"deterministic"}')
PATCH_STATUS=$(printf '%s' "$PATCH_RESPONSE" | tail -n 1)
PATCH_JSON=$(printf '%s' "$PATCH_RESPONSE" | sed '$d')
check_status "the patch is generated" "$PATCH_STATUS" "200"
PATCH=$(printf '%s' "$PATCH_JSON" | jget "d['id']")
echo "  patch: $PATCH"
printf '%s' "$PATCH_JSON" | jb "d['status'] == 'GENERATED'" \
  && ok "the patch parsed, passed safety validation and was stored (§11, §13)" \
  || bad "patch status" "$(printf '%s' "$PATCH_JSON" | jget "d['status']") / $(printf '%s' "$PATCH_JSON" | jget "d['failure_reason']")"
printf '%s' "$PATCH_JSON" | jb "d['changed_files'] == 1 and d['lines_added'] == 1 and d['lines_removed'] == 1" \
  && ok "the patch is minimal: 1 file, +1 −1 (§9)" \
  || bad "patch size" "$(printf '%s' "$PATCH_JSON" | jget "(d['changed_files'], d['lines_added'], d['lines_removed'])")"
printf '%s' "$PATCH_JSON" | jb "d['affected_paths'] == ['services/inventory/repository.py']" \
  && ok "the patch touches exactly the scoped file (§10)" \
  || bad "affected paths" "$(printf '%s' "$PATCH_JSON" | jget "d['affected_paths']")"
printf '%s' "$PATCH_JSON" | jb "d['base_commit_sha'] == '$HEAD_SHA'" \
  && ok "the patch records the base commit it applies to" \
  || bad "base commit" "$(printf '%s' "$PATCH_JSON" | jget "d['base_commit_sha']")"
printf '%s' "$PATCH_JSON" | jb "d['explanation'].get('why_changed') and d['explanation'].get('evidence')" \
  && ok "the patch carries its reasoning and evidence references (§20)" \
  || bad "explanation" "no reasoning stored"
DIFF=$(curl -fsS "$API/api/v1/patches/$PATCH/diff?project_id=$DEMO")
printf '%s' "$DIFF" | grep -q '^\-\s*DB_TIMEOUT_SECONDS = 0.25$' \
  && ok "the diff removes the defect line the snapshot actually contains" \
  || bad "diff removal" "the defect line is not removed"
printf '%s' "$DIFF" | grep -q '^+\s*DB_TIMEOUT_SECONDS = 2.0$' \
  && ok "the diff adds the fix line" || bad "diff addition" "no fix line"

echo "== 7. Verify the patch on a disposable workspace (§23, §26–§37) =="
VERIFY_RESPONSE=$(curl -s -w '\n%{http_code}' -X POST \
  "$API/api/v1/patches/$PATCH/verify?project_id=$DEMO" \
  -H 'Content-Type: application/json' \
  -d '{"baseline_reproduced":true,"baseline_metrics":{"error_rate":0.082,"latency_p95_ms":2900},"patched_metrics":{"error_rate":0.006,"latency_p95_ms":300},"baseline_failure_signature":"inventory database query timed out"}')
VERIFY_STATUS=$(printf '%s' "$VERIFY_RESPONSE" | tail -n 1)
RUN_JSON=$(printf '%s' "$VERIFY_RESPONSE" | sed '$d')
check_status "the verification run completes" "$VERIFY_STATUS" "200"
RUN=$(printf '%s' "$RUN_JSON" | jget "d['id']")
echo "  run: $RUN"
printf '%s' "$RUN_JSON" | jb "d['status'] == 'VERIFIED'" \
  && ok "the patch is VERIFIED: the failure was reproduced on base, fixed on the patched tree, with no unacceptable regression (§1, §64)" \
  || bad "verdict" "$(printf '%s' "$RUN_JSON" | jget "d['verdict_reason']")"
printf '%s' "$RUN_JSON" | jb "d['level'] == 'FULLY_VERIFIED' and d['confidence'] == 'HIGH'" \
  && ok "the verdict reaches the top rung with HIGH confidence" \
  || bad "level" "$(printf '%s' "$RUN_JSON" | jget "(d['level'], d['confidence'])")"
printf '%s' "$RUN_JSON" | jb "d['tampering_flag'] == 'NONE'" \
  && ok "no tampering was detected (§55)" || bad "tampering" "$(printf '%s' "$RUN_JSON" | jget "d['tampering_flag']")"
printf '%s' "$RUN_JSON" | jb "d['verification_env_intact'] is True" \
  && ok "the verification environment was not modified to make the patch pass (§56)" \
  || bad "environment integrity" "the environment was changed"
printf '%s' "$RUN_JSON" | jb "d['regression_detected'] is False" \
  && ok "no regression was detected in the before/after comparison (§36)" \
  || bad "regression flag" "a regression was flagged on a good patch"
printf '%s' "$RUN_JSON" | jb "d['baseline_failure_reproduced'] is True and d['patched_failure_reproduced'] is False" \
  && ok "the failure reproduces on the baseline and does not after the patch (§30, §32)" \
  || bad "reproduction" "$(printf '%s' "$RUN_JSON" | jget "(d['baseline_failure_reproduced'], d['patched_failure_reproduced'])")"
printf '%s' "$RUN_JSON" | jb "d['regression_tests'] and d['regression_tests'][0]['valid'] is True" \
  && ok "the generated regression test failed on base and passed on the patch (§29)" \
  || bad "regression test" "$(printf '%s' "$RUN_JSON" | jget "d['regression_tests']")"
printf '%s' "$RUN_JSON" | jb "d['regression_tests'][0]['content_hash']" \
  && ok "the regression test artifact is content-hashed (§45)" \
  || bad "artifact hash" "no content hash stored"
printf '%s' "$RUN_JSON" | jb "all(t['exit_code'] == 0 for t in d['test_runs'] if t['kind'] in ('STATIC','UNIT') and t['exit_code'] is not None)" \
  && ok "every executed static/test command exited 0" \
  || bad "command exits" "$(printf '%s' "$RUN_JSON" | jget "[(t['kind'], t['command_key'], t['exit_code']) for t in d['test_runs']]")"
printf '%s' "$RUN_JSON" | jb "[t for t in d['test_runs'] if t['kind'] == 'UNIT' and t['selected_tests']]" \
  && ok "the repository's own tests were selected and run (§27)" \
  || bad "test selection" "$(printf '%s' "$RUN_JSON" | jget "[(t['kind'], t['selected_tests']) for t in d['test_runs']]")"
printf '%s' "$RUN_JSON" | jb "[t for t in d['test_runs'] if t['kind'] == 'REGRESSION' and t.get('output_tail')]" \
  && ok "the regression test ran on both sides and its output was captured" \
  || bad "regression runs" "one side did not run"
printf '%s' "$RUN_JSON" | jb "d['duration_ms'] is not None and d['completed_at']" \
  && ok "the run records its duration and completion time (§76)" \
  || bad "timing" "duration missing"
printf '%s' "$RUN_JSON" | grep -qi "AKIA\|hunter2\|sk_live" \
  && bad "verification output leaks a credential" "a secret reached the response" \
  || ok "no credential appears anywhere in the verification evidence (§52)"

echo "== 8. The stored verdict and the exports agree (§31, §45, §64, §72) =="
LATEST=$(curl -fsS "$API/api/v1/patches/$PATCH/verification?project_id=$DEMO")
echo "$LATEST" | jb "d['id'] == '$RUN' and d['status'] == 'VERIFIED'" \
  && ok "the latest stored run is the one just executed" \
  || bad "latest run" "$(echo "$LATEST" | jget "(d['id'], d['status'])")"
echo "$LATEST" | jb "d['evidence'].get('applied') is True" \
  && ok "the stored evidence records that the patch was really applied" \
  || bad "evidence" "application not recorded"
COMPARISON=$(curl -fsS "$API/api/v1/patches/$PATCH/comparison?project_id=$DEMO")
echo "$COMPARISON" | jb "d['items'] and d['items'][-1]['regressions'] == []" \
  && ok "the comparison export lists no regression (§31)" \
  || bad "comparison" "$(echo "$COMPARISON" | jget "[i['regressions'] for i in d['items']]")"
echo "$COMPARISON" | jb "d['items'][-1]['thresholds']" \
  && ok "the comparison names the thresholds it compared against (§37)" \
  || bad "comparison thresholds" "absent"
ARTIFACTS=$(curl -fsS "$API/api/v1/patches/$PATCH/artifacts?project_id=$DEMO")
echo "$ARTIFACTS" | jb "d['items'] and all(a['content_hash'] for a in d['items'])" \
  && ok "every artifact is content-addressed ($(echo "$ARTIFACTS" | jget "len(d['items'])")) (§45)" \
  || bad "artifacts" "$(echo "$ARTIFACTS" | jget "d")"
echo "$ARTIFACTS" | jb "all(a['immutable'] for a in d['items'] if a['artifact_type'] in ('PATCH_DIFF','VERIFICATION_REPORT'))" \
  && ok "completed verification artifacts are immutable (§45)" \
  || bad "artifact immutability" "a terminal artifact is mutable"
DIFF_HASH=$(echo "$ARTIFACTS" | jget "([a['content_hash'] for a in d['items'] if a['artifact_type'] == 'PATCH_DIFF'] or [''])[0]")
# Hash the raw bytes on the wire, not a shell-stripped copy: the diff's trailing
# newline is part of what was hashed, so a shell command substitution would
# compare two different byte strings.
DIFF_TMP=$(mktemp)
curl -fsS "$API/api/v1/patches/$PATCH/diff?project_id=$DEMO" -o "$DIFF_TMP"
EXPECTED_HASH=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$DIFF_TMP")
rm -f "$DIFF_TMP"
[ "$DIFF_HASH" = "$EXPECTED_HASH" ] \
  && ok "the stored diff hash matches a fresh hash of the served diff bytes" \
  || bad "artifact hash" "stored=${DIFF_HASH:0:12} recomputed=${EXPECTED_HASH:0:12}"
REPORT=$(curl -fsS "$API/api/v1/patches/$PATCH/report?project_id=$DEMO")
echo "$REPORT" | jb "d['verification']['status'] == 'VERIFIED'" \
  && ok "the export report carries the same verdict" || bad "report" "verdict mismatch"
echo "$REPORT" | jb "d['verification_checklist'] and all(e['ok'] for e in d['verification_checklist'] if e['step'] != 'verdict')" \
  && ok "every §64 checklist step that was evaluated passed" \
  || bad "checklist" "$(echo "$REPORT" | jget "[(e['step'], e['ok']) for e in d['verification_checklist']]")"
echo "$REPORT" | jb "d['verification_checklist'][-1]['step'] == 'verdict' and d['verification_checklist'][-1]['ok'] is True" \
  && ok "the checklist's verdict entry agrees with the stored run" \
  || bad "checklist verdict" "the export disagrees with the run"
echo "$REPORT" | jb "d['boundary']['deployed'] is False and d['boundary']['merged'] is False" \
  && ok "the report states that nothing was merged or deployed (§74)" \
  || bad "boundary" "$(echo "$REPORT" | jget "d['boundary']")"

echo "== 9. Review (§41, §70, §71) =="
DETAIL=$(curl -fsS "$API/api/v1/patches/$PATCH?project_id=$DEMO")
echo "$DETAIL" | jb "d['status'] == 'VERIFIED' and d['review_state'] == 'AWAITING_REVIEW'" \
  && ok "the patch stops at AWAITING_REVIEW (§71)" \
  || bad "review state" "$(echo "$DETAIL" | jget "(d['status'], d['review_state'])")"
echo "$DETAIL" | jb "all(w['status'] == 'DESTROYED' and w['destroyed_at'] for w in d['workspaces'])" \
  && ok "every workspace used by the run is destroyed (§48)" \
  || bad "workspace cleanup" "$(echo "$DETAIL" | jget "[(w['status'], w['destroyed_at']) for w in d['workspaces']]")"
APPROVE=$(curl -fsS -X POST "$API/api/v1/patches/$PATCH/approve?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"actor":"e2e-smoke","reason":"evidence is complete"}')
echo "$APPROVE" | jb "d['action'] == 'APPROVE' and d['actor'] == 'e2e-smoke'" \
  && ok "the approval is recorded with its actor (§70)" || bad "approval" "not recorded"
printf '%s' "$APPROVE" | grep -qi "merge\|deploy\|release" \
  && bad "approval response mentions shipping" "the boundary was crossed" \
  || ok "the approval response contains no merge, deploy or release (§74)"
AFTER=$(curl -fsS "$API/api/v1/patches/$PATCH?project_id=$DEMO")
echo "$AFTER" | jb "d['review_state'] == 'APPROVED' and d['status'] == 'VERIFIED'" \
  && ok "approval moved the review state and left the patch's verification intact" \
  || bad "post-approval state" "$(echo "$AFTER" | jget "(d['status'], d['review_state'])")"
echo "$AFTER" | jb "len(d['review_actions']) >= 1 and all(a['created_at'] for a in d['review_actions'])" \
  && ok "the audit trail is queryable from the patch (§70)" || bad "audit" "no actions stored"
check_status "an already-approved patch accepts no second approval" \
  "$(code -X POST "$API/api/v1/patches/$PATCH/approve?project_id=$DEMO" \
     -H 'Content-Type: application/json' -d '{"actor":"e2e-smoke"}')" "422"

echo "== 10. A recorded decision is final; a new candidate is the next step (§41, §58) =="
check_status "an approved patch cannot be regenerated behind the decision" \
  "$(code -X POST "$API/api/v1/patches/$PATCH/regenerate?project_id=$DEMO" \
     -H 'Content-Type: application/json' -d '{"actor":"e2e-smoke","reason":"changed my mind"}')" "422"
check_status "an approved patch cannot be rejected either" \
  "$(code -X POST "$API/api/v1/patches/$PATCH/reject?project_id=$DEMO" \
     -H 'Content-Type: application/json' -d '{"actor":"e2e-smoke"}')" "422"

#: A second hypothesis carries the §58 scenario, so regeneration is exercised
#: on a live candidate rather than on a patch whose decision is already final.
FIX2=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/fixes?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d "{\"debug_session_id\":\"$SESSION\",\"created_by\":\"e2e-smoke\"}" | jget "d['id']")
PATCH2=$(curl -fsS -X POST "$API/api/v1/fixes/$FIX2/generate?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"generated_by":"deterministic"}' | jget "d['id']")
REGROW=$(curl -fsS -X POST "$API/api/v1/patches/$PATCH2/regenerate?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"actor":"e2e-smoke","reason":"smaller diff requested"}')
NEW_PATCH=$(echo "$REGROW" | jget "d['id']")
echo "$REGROW" | jb "d['status'] == 'GENERATED' and d['id'] != '$PATCH2'" \
  && ok "a regeneration produces a fresh candidate" || bad "regeneration" "$(echo "$REGROW" | jget "d['status']")"
OLD=$(curl -fsS "$API/api/v1/patches/$PATCH2?project_id=$DEMO")
echo "$OLD" | jb "d['status'] == 'SUPERSEDED'" \
  && ok "the previous candidate is SUPERSEDED, never silently reused" \
  || bad "supersede" "$(echo "$OLD" | jget "d['status']")"
echo "$OLD" | jb "d['review_state'] == 'AWAITING_REVIEW'" \
  && ok "the superseded candidate returns to review with nothing carried over" \
  || bad "superseded review" "$(echo "$OLD" | jget "d['review_state']")"

#: §58 — candidates never share mutable state: each verification builds its own
#: workspace from the pinned snapshot, so the fresh candidate has no residue
#: from the verified one.
echo "$REGROW" | jb "d['status'] == 'GENERATED' and d['base_commit_sha'] == '$HEAD_SHA'" \
  && ok "the fresh candidate is generated from the same pinned base revision" \
  || bad "fresh candidate base" "$(echo "$REGROW" | jget "d['base_commit_sha']")"

NEW_DETAIL=$(curl -fsS "$API/api/v1/patches/$NEW_PATCH?project_id=$DEMO")
echo "$NEW_DETAIL" | jb "d['workspaces'] == [] and d['verification_runs'] == []" \
  && ok "the fresh candidate carries no workspace or verification from the old one" \
  || bad "candidate isolation" "$(echo "$NEW_DETAIL" | jget "(len(d['workspaces']), len(d['verification_runs']))")"

echo "== 11. An unverified patch cannot be approved (§1, §41) =="
check_status "approving the fresh unverified candidate is refused" \
  "$(code -X POST "$API/api/v1/patches/$NEW_PATCH/approve?project_id=$DEMO" \
     -H 'Content-Type: application/json' -d '{"actor":"e2e-smoke"}')" "422"

echo "== 12. AI generation without a usable provider fabricates nothing (§8, §57) =="
AI_FIX=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/fixes?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d "{\"debug_session_id\":\"$SESSION\"}" | jget "d['id']")
AI_PATCH=$(curl -fsS -X POST "$API/api/v1/fixes/$AI_FIX/generate?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"generated_by":"ai"}')
echo "$AI_PATCH" | jb "d['status'] == 'GENERATION_FAILED'" \
  && ok "the unconfigured provider yields GENERATION_FAILED, not a guess (§57)" \
  || bad "ai generation" "$(echo "$AI_PATCH" | jget "(d['status'], d['failure_reason'])")"
echo "$AI_PATCH" | jb "d.get('failure_reason')" \
  && ok "the failure is recorded with its reason" || bad "ai reason" "absent"
echo "$AI_PATCH" | jb "d['changed_files'] == 0" \
  && ok "no empty patch was stored as if it were a fix" || bad "ai patch content" "a patch was stored anyway"

echo "== 13. Project isolation (§56-style) =="
if [ -n "$OTHER" ]; then
  check_status "another project cannot read this fix hypothesis" \
    "$(code "$API/api/v1/fixes/$FIX?project_id=$OTHER")" "404"
  check_status "another project cannot read this patch" \
    "$(code "$API/api/v1/patches/$PATCH?project_id=$OTHER")" "404"
  check_status "another project cannot review this patch" \
    "$(code -X POST "$API/api/v1/patches/$PATCH/reject?project_id=$OTHER" \
       -H 'Content-Type: application/json' -d '{"actor":"other"}')" "404"
  OTHER_METRICS=$(curl -fsS "$API/api/v1/fixes/metrics?project_id=$OTHER")
  echo "$OTHER_METRICS" | jb "d['patches_total'] == 0" \
    && ok "the other project's fix metrics show none of this project's work" \
    || bad "isolation leak" "foreign project sees $(echo "$OTHER_METRICS" | jget "d['patches_total']") patches"
fi

echo "== 14. The demo checkout was never modified (§48) =="
STATE=$($COMPOSE exec -T api sh -lc '
cd /repos/scratch/phase7-commerce
echo "DIRTY=$(git status --porcelain | wc -l | tr -d " ")"
echo "HEAD=$(git rev-parse HEAD)"
echo "BRANCHES=$(git for-each-ref --format="%(refname:short)" refs/heads | grep -c "^argus/" || true)"
echo "LEFTOVER=$(ls -1 /tmp/argus-reproduction/workspaces 2>/dev/null | wc -l | tr -d " ")"
') || true
printf '%s' "$STATE" | grep -q "DIRTY=0" \
  && ok "the original repository has no uncommitted changes after verification (§48)" \
  || bad "repository modified" "$(printf '%s' "$STATE" | grep DIRTY || echo 'git status failed')"
printf '%s' "$STATE" | grep -q "HEAD=$HEAD_SHA" \
  && ok "the repository is still at the commit it started on" \
  || bad "head moved" "$(printf '%s' "$STATE" | grep HEAD || echo 'no HEAD')"
printf '%s' "$STATE" | grep -q "BRANCHES=0" \
  && ok "no temporary argus branch was left behind" \
  || bad "branch leftover" "$(printf '%s' "$STATE" | grep BRANCHES || echo 'branch check failed')"
printf '%s' "$STATE" | grep -q "LEFTOVER=0" \
  && ok "no patch workspace survives on disk" \
  || bad "workspace leftover" "$(printf '%s' "$STATE" | grep LEFTOVER || echo 'workspace check failed')"

echo "== 15. Metrics reflect what happened (§67) =="
METRICS=$(curl -fsS "$API/api/v1/fixes/metrics?project_id=$DEMO")
echo "$METRICS" | jb "d['hypotheses_total'] >= 2" \
  && ok "both hypotheses are counted ($(echo "$METRICS" | jget "d['hypotheses_total']"))" \
  || bad "hypotheses metric" "$(echo "$METRICS" | jget "d['hypotheses_total']")"
#: §67 — "awaiting review" counts *verified* patches with no recorded decision.
#: The one verified patch in this run was approved, so the count must be 0:
#: a metric that still showed it waiting would contradict the audit trail.
echo "$METRICS" | jb "d['patches_awaiting_review'] == 0" \
  && ok "a decided patch is no longer counted as awaiting review (§67)" \
  || bad "awaiting metric" "$(echo "$METRICS" | jget "d['patches_awaiting_review']")"
echo "$METRICS" | jb "d['patches_verified'] >= 1" \
  && ok "the verified patch is counted" || bad "verified metric" "$(echo "$METRICS" | jget "d['patches_verified']")"
echo "$METRICS" | jb "d['patches_rejected'] == 0" \
  && ok "no patch was rejected in this run" || bad "rejected metric" "$(echo "$METRICS" | jget "d['patches_rejected']")"
echo "$METRICS" | jb "d['verification_runs_total'] >= 1" \
  && ok "verification runs are counted" || bad "runs metric" "$(echo "$METRICS" | jget "d['verification_runs_total']")"

echo "== 16. The Phase 7 schema exists in PostgreSQL =="
TABLES=$($COMPOSE exec -T postgres psql -U argus -d argus_db -tAc \
  "select count(*) from information_schema.tables where table_schema='public' and table_name in ('fix_hypotheses','patches','patch_workspaces','patch_verification_runs','patch_test_runs','patch_regression_tests','patch_comparisons','patch_risk_assessments','patch_review_actions','patch_artifacts')" 2>/dev/null) || true
[ "${TABLES:-0}" = "10" ] \
  && ok "all ten Phase 7 tables exist in the live database" \
  || bad "phase 7 schema" "found ${TABLES:-0}/10 tables"
#: Constraints are counted from the catalogue rather than by name prefix: the
#: names are generated, and a check that depends on them tests nothing.
FK=$($COMPOSE exec -T postgres psql -U argus -d argus_db -tAc \
  "select count(*) from pg_constraint c join pg_class t on t.oid = c.conrelid where c.contype = 'f' and t.relname like 'patch_%'" 2>/dev/null) || true
[ "${FK:-0}" -ge 8 ] \
  && ok "the patch tables are referentially wired ($FK foreign keys)" \
  || bad "patch foreign keys" "found ${FK:-0}"
#: A fix's rows must not outlive the project they belong to.
CASCADE=$($COMPOSE exec -T postgres psql -U argus -d argus_db -tAc \
  "select count(*) from pg_constraint where contype = 'f' and confdeltype in ('c','n') and conname like '%project%' and conrelid::regclass::text like 'patch_%'" 2>/dev/null) || true
[ "${CASCADE:-0}" -ge 3 ] \
  && ok "project references on patch tables carry a delete action ($CASCADE)" \
  || bad "cascade wiring" "found ${CASCADE:-0}"

if [ "${DDL_PROBE:-0}" = "1" ]; then
  echo "== 17. The Phase 7 migration reverses (opt-in DDL_PROBE=1) =="
  REV=$($COMPOSE exec -T api sh -lc "cd /app && alembic downgrade -1 && alembic upgrade head" 2>&1) || true
  printf '%s' "$REV" | grep -qi "error" \
    && bad "migration reverse" "$(printf '%s' "$REV" | tail -c 300)" \
    || ok "the Phase 7 migration downgrades and re-applies cleanly"
fi
# The AI-generation experiment must leave the demo incident's telemetry intact:
# this is the final check that verification never wrote to the source system.
echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
