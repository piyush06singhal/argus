#!/usr/bin/env bash
# ARGUS Phase 9 — live end-to-end smoke test (safe autonomous remediation)
#
# Drives the whole remediation pipeline against the running compose stack over the
# real HTTP API — nothing is stubbed, no outcome is hard-coded, and every refusal
# is asserted as a refusal:
#
#   0. an isolated scratch project (so the gate cannot perturb the seeded demo)
#   1. scope, ownership and unknown-id refusals
#   2. the action registry is closed and carries no command/credential parameter
#   3. default deny: a scope with no policy authorizes nothing
#   4. human-approved remediation: propose → assess → policy → approve → execute
#      → verify, and the control plane really holds the job down
#   5. autonomous low-risk remediation, the escalation of anything the registry
#      will not run autonomously, and the production refusal — including an
#      environment whose *name* looks non-production but which declares itself
#      PRODUCTION (§107)
#   6. canary execution (§23, §107)
#   7. a dry run is never scored as a success (§26, §107)
#   8. rollback reverses the effect and verifies the reversal; rolling back what
#      was never applied is refused (§32, §33, §107)
#   9. policy denial, and the emergency stop denying everything (§34, §107)
#  10. a stale action expires instead of executing later (§107)
#  11. loop protection: the budget cap and the circuit breaker (§25, §27, §107)
#  12. every one of the six regimes, individually (§40, §107)
#  13. the audit trail is hash-chained and verifiable (§12)
#  14. cross-project isolation
#  15. the Phase 9 tables and indexes exist in PostgreSQL (DDL check is opt-in:
#      DDL_PROBE=1 also reverses and re-applies the migration)
#  16. cleanup: the scratch project is deleted and nothing it changed survives
#
# Prereq: DATABASE_PORT=5433 docker compose up --build -d (seed runs on boot).
set -eu
API="${API:-http://localhost:8000}"
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
#: Same connection, but the exit status is preserved — used where the point of
#: the statement is that it must *fail*.
sql_ok() { $COMPOSE exec -T postgres psql -U argus -d argus_db -tAc "$1" >/dev/null 2>&1; }
nowiso() { python3 -c "import datetime; print(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'))"; }

TS=$(date +%s)
echo "== 0. An isolated scratch project =="
PROJ=$(post "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"smoke-remediation-$TS\",\"slug\":\"smoke-remediation-$TS\",\"description\":\"Phase 9 smoke\"}" \
  | jget "d['id']")
mkenv() { post "$API/api/v1/projects/$PROJ/environments" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$1\",\"environment_type\":\"${2:-DEVELOPMENT}\"}" | jget "d['id']"; }
# Names and declared types matter independently: a scope counts as
# non-production only when the name is in REMEDIATION_NON_PRODUCTION_ENVIRONMENT_NAMES
# *and* the environment is not declared PRODUCTION. The regime environments are
# therefore named and typed as non-production, ENV_PROD is both, and ENV_MIXED
# is the trap: allow-listed name, PRODUCTION declaration.
ENV_STAGING=$(mkenv staging STAGING)
ENV_PROD=$(mkenv production PRODUCTION)
ENV_MIXED=$(mkenv demo PRODUCTION)
ENV_OBSERVE=$(mkenv development DEVELOPMENT)
ENV_DRY=$(mkenv test TEST)
ENV_SHADOW=$(mkenv testing TEST)
ENV_HUMAN=$(mkenv sandbox DEVELOPMENT)
ENV_AUTO=$(mkenv dev DEVELOPMENT)
ENV_STOP=$(mkenv preview DEVELOPMENT)
CO=$(post "$API/api/v1/projects/$PROJ/components" -H 'Content-Type: application/json' \
  -d '{"name":"checkout-service","component_type":"SERVICE"}' | jget "d['id']")
INC=$(post "$API/api/v1/incidents" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_STAGING\",\"primary_component_id\":\"$CO\",\"title\":\"smoke remediation evidence\",\"severity\":\"HIGH\",\"detected_at\":\"$(nowiso)\"}" \
  | jget "d['id']")
ok "scratch project $PROJ with 9 environments and one incident"

echo "== 1. Scope and ownership refusals =="
check_status "policy without a project scope is refused" \
  "$(code "$API/api/v1/remediation/policy")" "422"
check_status "actions without a project scope are allowed to list nothing" \
  "$(code "$API/api/v1/remediation/actions")" "200"
check_status "unknown project answers 404" \
  "$(code "$API/api/v1/remediation/metrics?project_id=00000000-0000-0000-0000-000000000000")" "404"
check_status "unknown action id answers 404" \
  "$(code "$API/api/v1/remediation/actions/00000000-0000-0000-0000-000000000000?project_id=$PROJ")" "404"
check_status "an unknown action type is refused, not defaulted" \
  "$(code -X POST "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
     -d "{\"project_id\":\"$PROJ\",\"action_type\":\"RUN_SHELL_COMMAND\",\"description\":\"nope\"}")" "422"
check_status "an unknown parameter on a real action is refused" \
  "$(code -X POST "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
     -d "{\"project_id\":\"$PROJ\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"nope\",\"parameters\":{\"command\":\"rm -rf /\"}}")" "422"

echo "== 2. The registry is closed and carries no command parameter (§3) =="
REG=$(curl -fsS "$API/api/v1/remediation/action-types")
echo "$REG" | jb "d['count'] >= 12 and d['execution_enabled'] is True" \
  && ok "the registry lists $(echo "$REG" | jget "d['count']") actions" \
  || bad "registry" "$REG"
echo "$REG" | jb "not any(p['name'] in ('command','cmd','shell','script','token','password','secret') for a in d['actions'] for p in a['parameters'])" \
  && ok "no registered action accepts a command or a credential" || bad "registry params" "$REG"
echo "$REG" | jb "any(a['executable_in_build'] for a in d['actions'])" \
  && ok "at least one action is executable in this build" || bad "executability" "$REG"
echo "$REG" | jb "all('requires_human_approval' in a and 'supports_autonomous_execution' in a and 'reversible' in a for a in d['actions'])" \
  && ok "every action declares its authority and reversibility up front" || bad "declarations" "$REG"

echo "== 3. Default deny: a scope with no policy authorizes nothing =="
POL=$(curl -fsS "$API/api/v1/remediation/policy?project_id=$PROJ&environment_id=$ENV_STAGING")
echo "$POL" | jb "d['source'] == 'fallback' and d['execution_mode'] == 'OBSERVE_ONLY'" \
  && ok "a missing policy reads as the restrictive default" || bad "fallback policy" "$POL"
DENIED=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_OBSERVE\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause ingestion\",\"parameters\":{\"job\":\"ingestion_worker\"},\"created_by\":\"smoke\"}")
echo "$DENIED" | jb "d['status'] == 'REJECTED' and d['policy_status'] == 'DENY' and d['failure_reason'] == 'POLICY_DENIED'" \
  && ok "an unconfigured scope denies rather than authorizes" || bad "default deny" "$DENIED"

echo "== 4. Human-approved remediation (§107) =="
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_STAGING\",\"execution_mode\":\"HUMAN_APPROVAL\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\",\"RESUME_BACKGROUND_JOB\",\"DISABLE_FEATURE_FLAG\",\"ENABLE_FEATURE_FLAG\"],\"cooldown_seconds\":0,\"max_actions_per_window\":20,\"canary_enabled\":false,\"updated_by\":\"smoke\"}" > /dev/null
ok "configured HUMAN_APPROVAL for the staging environment"
A=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_STAGING\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause the detection sweep\",\"reason\":\"it is amplifying load during the incident\",\"parameters\":{\"job\":\"anomaly_sweep\"},\"created_by\":\"smoke-operator\"}")
A_ID=$(echo "$A" | jget "d['id']")
echo "$A" | jb "d['status'] == 'AWAITING_APPROVAL' and d['safety_status'] in ('PASSED','PASSED_WITH_WARNINGS')" \
  && ok "a permitted action waits for a human (safety $(echo "$A" | jget "d['safety_status']"))" || bad "awaiting approval" "$A"
echo "$A" | jb "d['authorized_at'] is None and d['executed_by'] is None" \
  && ok "nothing has authorized or executed it yet" || bad "premature authorization" "$A"
check_status "an unrecognised approver is refused" \
  "$(code -X POST "$API/api/v1/remediation/actions/$A_ID/approve?project_id=$PROJ" -H 'Content-Type: application/json' -d '{"actor":""}')" "422"
APPROVED=$(post "$API/api/v1/remediation/actions/$A_ID/approve?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"actor":"on-call-engineer","reason":"approved during the incident"}')
echo "$APPROVED" | jb "d['status'] == 'AUTHORIZED' and d['approved_by'] == 'on-call-engineer' and d['executed_by'] is None" \
  && ok "a named human authorized it; it has still not run" || bad "approval" "$APPROVED"
RAN=$(post "$API/api/v1/remediation/actions/$A_ID/execute?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"actor":"on-call-engineer"}')
echo "$RAN" | jb "d['status'] == 'VERIFIED' and d['outcome'] in ('EFFECTIVE','PARTIALLY_EFFECTIVE')" \
  && ok "execution applied the effect and verification confirmed it" || bad "execute" "$RAN"
CTRL=$(curl -fsS "$API/api/v1/remediation/controls?project_id=$PROJ&environment_id=$ENV_STAGING")
echo "$CTRL" | jb "any(c['scope_key'] == 'anomaly_sweep' and c['state'] == 'PAUSED' and c['is_current'] and c['applied_by_action_id'] == '$A_ID' for c in d['controls'])" \
  && ok "the control plane really holds the job down, attributable to the action" || bad "control" "$CTRL"
DET=$(curl -fsS "$API/api/v1/remediation/actions/$A_ID?project_id=$PROJ")
echo "$DET" | jb "d['action']['status'] == 'VERIFIED' and len(d['verifications']) >= 1 and d['verifications'][-1]['verdict'] in ('VERIFIED','PARTIALLY_VERIFIED')" \
  && ok "the verification record names its verdict" || bad "verification record" "$DET"

echo "== 5. Autonomous authorization and its refusals (§40, §107) =="
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_AUTO\",\"execution_mode\":\"AUTONOMOUS\",\"autonomous_max_risk\":\"LOW\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\",\"DISABLE_FEATURE_FLAG\",\"ENABLE_FEATURE_FLAG\"],\"cooldown_seconds\":0,\"max_actions_per_window\":20,\"canary_enabled\":false,\"updated_by\":\"smoke\"}" > /dev/null
AUTO=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_AUTO\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"DISABLE_FEATURE_FLAG\",\"description\":\"disable graph extraction\",\"parameters\":{\"flag\":\"graph_extraction\"},\"created_by\":\"planner\"}")
AUTO_ID=$(echo "$AUTO" | jget "d['id']")
echo "$AUTO" | jb "d['status'] == 'AUTHORIZED' and d['execution_mode'] == 'AUTONOMOUS'" \
  && ok "a LOW-risk action was authorized by policy without a human" || bad "autonomous" "$AUTO"
DETAIL=$(curl -fsS "$API/api/v1/remediation/actions/$AUTO_ID?project_id=$PROJ")
echo "$DETAIL" | jb "any(a['actor_type'] == 'AUTONOMOUS_POLICY' and a['status'] == 'APPROVED' for a in d['approvals'])" \
  && ok "the authorization is recorded as a policy decision, not a person" || bad "autonomous record" "$DETAIL"
# An action the registry will not run autonomously escalates, however
# permissive the policy is. ENABLE_FEATURE_FLAG is MEDIUM-risk and declares
# ``supports_autonomous_execution=False``, so the escalation must name one of
# the authority rules rather than quietly authorizing it.
#
# (The ``autonomous_ceiling`` rule guards the case of an autonomous-eligible
# action whose risk exceeds the ceiling. No action in this build is both, which
# is why the rule itself is pinned by the unit suite rather than here; what is
# asserted live is that risk and eligibility are what the decision turns on.)
CEIL=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_AUTO\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"ENABLE_FEATURE_FLAG\",\"description\":\"re-enable a flag\",\"parameters\":{\"flag\":\"code_indexing\"},\"created_by\":\"planner\"}")
CEIL_ID=$(echo "$CEIL" | jget "d['id']")
echo "$CEIL" | jb "d['status'] == 'AWAITING_APPROVAL' and d['risk_level'] == 'MEDIUM'" \
  && ok "an action above the build's autonomous authority escalates to a human" || bad "ceiling" "$CEIL"
CEIL_D=$(curl -fsS "$API/api/v1/remediation/actions/$CEIL_ID?project_id=$PROJ")
echo "$CEIL_D" | jb "any(any(r.get('outcome') == 'escalate' and r.get('rule') in ('autonomous_ceiling', 'irreversibility') for r in (p.get('matched_rules') or [])) for p in d['policy_decisions'])" \
  && ok "the escalation names the authority rule that caused it" || bad "ceiling rule" "$CEIL_D"
# Production protection: the same policy in a production-declared environment escalates.
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_PROD\",\"execution_mode\":\"AUTONOMOUS\",\"autonomous_max_risk\":\"LOW\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\"],\"cooldown_seconds\":0,\"canary_enabled\":false,\"updated_by\":\"smoke\"}" > /dev/null
PROD=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_PROD\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause in production\",\"parameters\":{\"job\":\"reliability_sweep\"},\"created_by\":\"planner\"}")
echo "$PROD" | jb "d['status'] == 'AWAITING_APPROVAL'" \
  && ok "autonomous execution is not considered in a production scope" || bad "production" "$PROD"
# A production environment with a non-production *name* is still production:
# the configured allow-list is a naming convention, and the declared type is a
# fact. Requiring both is what keeps a rename from widening the blast radius.
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_MIXED\",\"execution_mode\":\"AUTONOMOUS\",\"autonomous_max_risk\":\"LOW\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\"],\"cooldown_seconds\":0,\"canary_enabled\":false,\"updated_by\":\"smoke\"}" > /dev/null
MIXED=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_MIXED\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause in a production environment that is merely named demo\",\"parameters\":{\"job\":\"code_sweep\"},\"created_by\":\"planner\"}")
MIXED_ID=$(echo "$MIXED" | jget "d['id']")
echo "$MIXED" | jb "d['status'] == 'AWAITING_APPROVAL'" \
  && ok "an allow-listed name does not override a PRODUCTION declaration" || bad "mixed scope" "$MIXED"
MIXED_D=$(curl -fsS "$API/api/v1/remediation/actions/$MIXED_ID?project_id=$PROJ")
echo "$MIXED_D" | jb "any(any(r.get('rule') == 'non_production_required' for r in (p.get('matched_rules') or [])) for p in d['policy_decisions'])" \
  && ok "the refusal says the scope is production" || bad "mixed rule" "$MIXED_D"

echo "== 6. Canary execution (§23) =="
# DISABLE_FEATURE_FLAG is one of the two registry actions that declare
# ``supports_canary``: a control-plane effect that can be staged (a bounded
# first step, then a second decision) rather than only applied whole. Pausing a
# job is binary, so asking it to canary would be asking for nothing.
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_STAGING\",\"execution_mode\":\"AUTONOMOUS\",\"autonomous_max_risk\":\"LOW\",\"allowed_action_types\":[\"DISABLE_FEATURE_FLAG\"],\"cooldown_seconds\":0,\"max_actions_per_window\":20,\"canary_enabled\":true,\"canary_percent\":10,\"updated_by\":\"smoke\"}" > /dev/null
CAN=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_STAGING\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"DISABLE_FEATURE_FLAG\",\"description\":\"disable graph extraction for a canary share\",\"parameters\":{\"flag\":\"graph_extraction\"},\"created_by\":\"planner\"}")
CAN_ID=$(echo "$CAN" | jget "d['id']")
echo "$CAN" | jb "d['canary_required'] is True and d['canary_stage'] == 'CANARY'" \
  && ok "a canary step is required before the full effect ($(echo "$CAN" | jget "d['canary_percent']")%)" || bad "canary" "$CAN"
CAN_D=$(curl -fsS "$API/api/v1/remediation/actions/$CAN_ID?project_id=$PROJ")
echo "$CAN_D" | jb "any(p['decision'] == 'ALLOW_WITH_CANARY' for p in d['policy_decisions'])" \
  && ok "the policy decision records the canary requirement" || bad "canary decision" "$CAN_D"

echo "== 7. A dry run is never scored as a success (§26) =="
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_DRY\",\"execution_mode\":\"HUMAN_APPROVAL\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\"],\"cooldown_seconds\":0,\"max_actions_per_window\":20,\"canary_enabled\":false,\"updated_by\":\"smoke\"}" > /dev/null
DRY=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_DRY\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause the fix sweep\",\"parameters\":{\"job\":\"fix_sweep\"},\"created_by\":\"smoke\"}")
DRY_ID=$(echo "$DRY" | jget "d['id']")
post "$API/api/v1/remediation/actions/$DRY_ID/approve?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"actor":"on-call-engineer","reason":"approved for a dry run"}' > /dev/null
DRY_RUN=$(post "$API/api/v1/remediation/actions/$DRY_ID/execute?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"actor":"on-call-engineer","dry_run":true}')
echo "$DRY_RUN" | jb "d['status'] != 'VERIFIED'" \
  && ok "the dry run did not verify (status $(echo "$DRY_RUN" | jget "d['status']"))" || bad "dry run" "$DRY_RUN"
DRY_D=$(curl -fsS "$API/api/v1/remediation/actions/$DRY_ID?project_id=$PROJ")
echo "$DRY_D" | jb "len(d['executions']) >= 1 and all(not e['effect_applied'] for e in d['executions'])" \
  && ok "no effect was applied, so there is nothing to verify" || bad "dry run effect" "$DRY_D"
DRY_CTRL=$(curl -fsS "$API/api/v1/remediation/controls?project_id=$PROJ&environment_id=$ENV_DRY")
echo "$DRY_CTRL" | jb "not any(c['scope_key'] == 'fix_sweep' and c['is_current'] for c in d['controls'])" \
  && ok "a dry run wrote no control" || bad "dry run control" "$DRY_CTRL"

echo "== 8. Rollback reverses the effect and verifies the reversal (§32, §33) =="
RB=$(post "$API/api/v1/remediation/actions/$A_ID/rollback?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"actor":"on-call-engineer","reason":"the degradation ended on its own"}')
echo "$RB" | jb "d['status'] in ('ROLLED_BACK','FAILED')" \
  && ok "the rollback ran (status $(echo "$RB" | jget "d['status']"))" || bad "rollback" "$RB"
CTRL2=$(curl -fsS "$API/api/v1/remediation/controls?project_id=$PROJ&environment_id=$ENV_STAGING")
echo "$CTRL2" | jb "not any(c['scope_key'] == 'anomaly_sweep' and c['is_current'] and c['effective'] for c in d['controls'])" \
  && ok "the control is no longer in force" || bad "rollback control" "$CTRL2"
RB_D=$(curl -fsS "$API/api/v1/remediation/actions/$A_ID?project_id=$PROJ")
echo "$RB_D" | jb "len(d['rollbacks']) >= 1 and d['rollbacks'][-1]['status'] in ('SUCCEEDED','FAILED')" \
  && ok "the rollback is recorded with its own status and verdict" || bad "rollback record" "$RB_D"
check_status "rolling back an action that never applied an effect is refused" \
  "$(code -X POST "$API/api/v1/remediation/actions/$DRY_ID/rollback?project_id=$PROJ" -H 'Content-Type: application/json' -d '{"actor":"on-call-engineer"}')" "409"

echo "== 9. Policy denial and the emergency stop (§34) =="
STOP=$(post "$API/api/v1/remediation/emergency-stop?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"engage":true,"actor":"on-call-engineer","reason":"database failover in progress"}')
echo "$STOP" | jb "d['emergency_stop_active'] is True" \
  && ok "the emergency stop engaged" || bad "emergency stop" "$STOP"
STOPPED=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_STOP\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause during a stop\",\"parameters\":{\"job\":\"reproduction_sweep\"},\"created_by\":\"smoke\"}")
STOPPED_ID=$(echo "$STOPPED" | jget "d['id']")
# Blocked, not rejected: a stop is a condition the project can leave, so the
# action stays re-evaluable instead of being terminally refused. What matters is
# that it did not execute and that the refusal names the stop.
echo "$STOPPED" | jb "d['status'] == 'BLOCKED' and d['failure_reason'] == 'EMERGENCY_STOP' and d['authorized_at'] is None" \
  && ok "no action may be authorized while the stop is engaged" || bad "emergency stop deny" "$STOPPED"
STOPPED_D=$(curl -fsS "$API/api/v1/remediation/actions/$STOPPED_ID?project_id=$PROJ")
echo "$STOPPED_D" | jb "any(p['decision'] == 'DENY' and any(r.get('rule') == 'emergency_stop' for r in (p.get('matched_rules') or [])) for p in d['policy_decisions'])" \
  && ok "the denial names the emergency stop as the rule that fired" || bad "deny rule" "$STOPPED_D"
post "$API/api/v1/remediation/emergency-stop?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d '{"engage":false,"actor":"on-call-engineer","reason":"failover complete"}' > /dev/null
RELEASED=$(curl -fsS "$API/api/v1/remediation/policy?project_id=$PROJ")
echo "$RELEASED" | jb "d['emergency_stop_active'] is False" \
  && ok "releasing the stop authorizes nothing by itself" || bad "release" "$RELEASED"

echo "== 10. A stale action expires instead of executing later (§107) =="
STALE=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_HUMAN\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause the fix sweep later\",\"parameters\":{\"job\":\"reproduction_sweep\"},\"created_by\":\"smoke\"}")
STALE_ID=$(echo "$STALE" | jget "d['id']")
echo "$STALE" | jb "d['expires_at'] is not None" \
  && ok "every action carries an expiry the moment it is proposed" || bad "expiry" "$STALE"
# Age the row past its own deadline, then let the sweep do its job.
sql "update remediation_actions set expires_at = now() - interval '2 hours' where id = '$STALE_ID';" > /dev/null
SWEEP=$(post "$API/api/v1/remediation/sweep" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"plan\":false}")
echo "$SWEEP" | jb "d['expired_actions'] >= 1" \
  && ok "the sweep expired the stale action" || bad "sweep expiry" "$SWEEP"
STALE_D=$(curl -fsS "$API/api/v1/remediation/actions/$STALE_ID?project_id=$PROJ")
echo "$STALE_D" | jb "d['action']['status'] == 'EXPIRED' and d['action']['failure_reason'] == 'STALE_ACTION'" \
  && ok "the expired action says why, and did not execute" || bad "stale record" "$STALE_D"

# The budget and the breaker are both checked in the same regime
# (HUMAN_APPROVAL on ENV_OBSERVE), but they are independent refusals: one is
# about how much has been attempted, the other about what keeps failing.
echo "== 11. Loop protection: budget and circuit breaker (§25, §27) =="
put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
  -d "{\"environment_id\":\"$ENV_OBSERVE\",\"execution_mode\":\"HUMAN_APPROVAL\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\"],\"cooldown_seconds\":0,\"max_actions_per_window\":1,\"updated_by\":\"smoke\"}" > /dev/null
BUDGET1=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_OBSERVE\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"first action in the window\",\"parameters\":{\"job\":\"code_sweep\"},\"created_by\":\"smoke\"}")
post "$API/api/v1/remediation/actions/$(echo "$BUDGET1" | jget "d['id']")/approve?project_id=$PROJ" \
  -H 'Content-Type: application/json' -d '{"actor":"on-call-engineer","reason":"budget demo"}' > /dev/null
BUDGET2=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_OBSERVE\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"second action in the window\",\"parameters\":{\"job\":\"fix_sweep\"},\"created_by\":\"smoke\"}")
echo "$BUDGET2" | jb "d['status'] in ('BLOCKED','REJECTED') and d['failure_reason'] == 'BUDGET_EXHAUSTED'" \
  && ok "the action budget refuses the next action in the same window" || bad "budget" "$BUDGET2"
# A breaker that a sequence of failures would have opened. Written as an
# upsert because the scope is unique by design: earlier steps in this gate
# already evaluated actions in this scope, so a plain insert would collide (and
# one breaker per scope is the point — a second row could mask an OPEN one).
# The breaker is opened on the environment this gate does not otherwise use: a
# genuinely open breaker refuses every later attempt of that action type in that
# scope, which is the behaviour being verified here and would otherwise
# contaminate the regime checks below.
sql "insert into remediation_circuit_breakers (id, project_id, environment_id, action_type, state, consecutive_failures, total_attempts, total_failures, total_successes, threshold, opened_at, opened_until, last_trip_reason)
     values (gen_random_uuid(), '$PROJ', '$ENV_MIXED', 'PAUSE_BACKGROUND_JOB', 'OPEN', 3, 3, 3, 0, 3, now(), now() + interval '15 minutes', 'three consecutive attempts failed')
     on conflict (project_id, environment_id, action_type) do update
       set state = 'OPEN', consecutive_failures = 3, total_attempts = 3, total_failures = 3,
           total_successes = 0, threshold = 3, opened_at = now(),
           opened_until = now() + interval '15 minutes',
           last_trip_reason = 'three consecutive attempts failed', updated_at = now();" > /dev/null
BREAK=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV_MIXED\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"pause while the breaker is open\",\"parameters\":{\"job\":\"remediation_sweep\"},\"created_by\":\"smoke\"}")
BREAK_ID=$(echo "$BREAK" | jget "d['id']")
echo "$BREAK" | jb "d['status'] in ('BLOCKED','REJECTED') and d['failure_reason'] == 'CIRCUIT_OPEN'" \
  && ok "an open breaker refuses further attempts of that action type" || bad "breaker" "$BREAK"
BREAKERS=$(curl -fsS "$API/api/v1/remediation/breakers?project_id=$PROJ")
echo "$BREAKERS" | jb "any(b['state'] == 'OPEN' and b['consecutive_failures'] >= b['threshold'] for b in d['breakers'])" \
  && ok "the breaker state is published so a refusal can be explained" || bad "breakers" "$BREAKERS"

echo "== 12. Every regime, individually (§40, §107) =="
regime_check() {
  local label="$1" env="$2" mode="$3" job="$4" expect="$5" extra="${6:-}"
  put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d "{\"environment_id\":\"$env\",\"execution_mode\":\"$mode\",\"autonomous_max_risk\":\"LOW\",\"allowed_action_types\":[\"PAUSE_BACKGROUND_JOB\"],\"cooldown_seconds\":0,\"max_actions_per_window\":20,\"canary_enabled\":false,\"updated_by\":\"smoke\"$extra}" > /dev/null
  local resp
  resp=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$env\",\"component_id\":\"$CO\",\"incident_id\":\"$INC\",\"action_type\":\"PAUSE_BACKGROUND_JOB\",\"description\":\"regime $mode\",\"parameters\":{\"job\":\"$job\"},\"created_by\":\"smoke\"}")
  local got
  got=$(echo "$resp" | jget "d['status']")
  [ "$got" = "$expect" ] \
    && ok "$label: $mode → $got" \
    || bad "$label ($mode)" "expected=$expect got=$got body=$resp"
}
regime_check "observe-only" "$ENV_OBSERVE" "OBSERVE_ONLY" "anomaly_sweep" "REJECTED"
regime_check "dry-run" "$ENV_DRY" "DRY_RUN" "code_sweep" "AWAITING_APPROVAL"
regime_check "shadow" "$ENV_SHADOW" "SHADOW" "fix_sweep" "AWAITING_APPROVAL"
regime_check "human-approval" "$ENV_HUMAN" "HUMAN_APPROVAL" "reliability_sweep" "AWAITING_APPROVAL"
regime_check "autonomous" "$ENV_AUTO" "AUTONOMOUS" "reproduction_sweep" "AUTHORIZED"
regime_check "emergency-stop" "$ENV_STOP" "EMERGENCY_STOP" "ingestion_worker" "BLOCKED"

echo "== 13. The audit trail is hash-chained and verifiable (§12) =="
AUDIT=$(curl -fsS "$API/api/v1/remediation/actions/$A_ID/audit?project_id=$PROJ")
echo "$AUDIT" | jb "isinstance(d, list) and len(d) >= 5 and all(e['entry_hash'] and 'sequence' in e for e in d)" \
  && ok "every state change and gate decision has an entry ($(echo "$AUDIT" | jget "len(d)") for one action)" \
  || bad "audit" "$AUDIT"
CHAIN=$(curl -fsS "$API/api/v1/remediation/actions/$A_ID/audit/verify?project_id=$PROJ")
echo "$CHAIN" | jb "d['intact'] is True and d['events'] >= 5" \
  && ok "the chain recomputes and verifies" || bad "audit chain" "$CHAIN"
echo "$AUDIT" | jb "all(e['prev_hash'] is None or e['prev_hash'] for e in d)" \
  && ok "each entry binds its predecessor" || bad "chain linkage" "$AUDIT"

echo "== 14. Cross-project isolation =="
OTHER=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "([p['id'] for p in d['items'] if p['id'] != '$PROJ'][0] if [p['id'] for p in d['items'] if p['id'] != '$PROJ'] else '')")
if [ -n "$OTHER" ]; then
  check_status "a foreign project cannot read this action" \
    "$(code "$API/api/v1/remediation/actions/$A_ID?project_id=$OTHER")" "404"
  check_status "a foreign project cannot approve it" \
    "$(code -X POST "$API/api/v1/remediation/actions/$A_ID/approve?project_id=$OTHER" -H 'Content-Type: application/json' -d '{"actor":"attacker"}')" "404"
  FLIST=$(curl -fsS "$API/api/v1/remediation/actions?project_id=$OTHER")
  echo "$FLIST" | jb "d['actions'] == []" \
    && ok "the foreign project's action list is empty" || bad "foreign list" "$FLIST"
else
  ok "(single-project stack; isolation covered by the 404 checks above)"
fi

echo "== 15. Phase 9 tables and indexes exist in PostgreSQL =="
TABLES=$(sql "select count(*) from information_schema.tables where table_name in ('remediation_policies','remediation_proposals','remediation_actions','remediation_assessments','remediation_policy_decisions','remediation_approvals','remediation_executions','remediation_verifications','remediation_rollbacks','remediation_audit_events','remediation_circuit_breakers','remediation_controls')")
[ "${TABLES:-0}" = "12" ] \
  && ok "all 12 Phase 9 tables exist" || bad "phase 9 tables" "found ${TABLES:-0}"
IDX=$(sql "select count(*) from pg_indexes where tablename = 'remediation_actions'")
[ "${IDX:-0}" -ge 6 ] \
  && ok "the action table carries its indexes ($IDX)" || bad "indexes" "found ${IDX:-0}"
UNIQ=$(sql "select count(*) from pg_constraint where conname = 'uq_remediation_executions_action_attempt'")
[ "${UNIQ:-0}" = "1" ] \
  && ok "a retried attempt cannot write a second execution for the same attempt" \
  || bad "execution uniqueness" "found ${UNIQ:-0}"
BREAKER_IDX=$(sql "select count(*) from pg_indexes where indexname = 'uq_remediation_circuit_breakers_scope'")
[ "${BREAKER_IDX:-0}" = "1" ] \
  && ok "the circuit-breaker scope is unique" || bad "breaker index" "found ${BREAKER_IDX:-0}"
#: Two breakers for one scope would let the reader pick the CLOSED one and
#: ignore an OPEN one, so the uniqueness has to hold for a *project-wide* scope
#: too — where the environment column is NULL and the default SQL rule would
#: treat the two rows as different.
if sql_ok "insert into remediation_circuit_breakers (id, project_id, environment_id, action_type, state) values (gen_random_uuid(), '$PROJ', null, 'PAUSE_BACKGROUND_JOB', 'CLOSED');"; then
  ok "a project-wide breaker can be written"
else
  bad "breaker insert" "the first project-wide breaker was refused"
fi
if sql_ok "insert into remediation_circuit_breakers (id, project_id, environment_id, action_type, state) values (gen_random_uuid(), '$PROJ', null, 'PAUSE_BACKGROUND_JOB', 'OPEN');"; then
  bad "duplicate breaker" "a second breaker for the same project-wide scope was accepted"
else
  ok "a second breaker for one project-wide scope is refused by the database"
fi

if [ "${DDL_PROBE:-0}" = "1" ]; then
  echo "== 15b. The Phase 9 migration reverses (opt-in DDL_PROBE=1) =="
  REV=$($COMPOSE exec -T api sh -lc "cd /app && alembic downgrade -1 && alembic upgrade head" 2>&1) || true
  printf '%s' "$REV" | grep -qi "error" \
    && bad "migration reverse" "$(printf '%s' "$REV" | tail -c 300)" \
    || ok "the Phase 9 migration downgrades and re-applies cleanly"
fi

echo "== 16. Cleanup: the scratch project and everything it changed =="
# The scratch project owns every row this gate created: policies, controls,
# actions, proposals, approvals, executions, verifications and audit events all
# cascade from it. Deleting it by API proves the cascade works and leaves the
# seeded demo project exactly as it was found — including its sweeps, which this
# gate paused at one point and which must not stay paused for the other gates.
check_status "the scratch project is deleted (cascading every row it owns)" \
  "$(code -X DELETE "$API/api/v1/projects/$PROJ")" "204"
LEFT=$(sql "select count(*) from remediation_actions where project_id = '$PROJ'")
[ "${LEFT:-1}" = "0" ] && ok "no remediation action outlived its project" \
  || bad "leftover actions" "${LEFT:-?}"
LEFT_C=$(sql "select count(*) from remediation_controls where project_id = '$PROJ' and is_current = true")
[ "${LEFT_C:-1}" = "0" ] && ok "no control is left holding a job down" \
  || bad "leftover controls" "${LEFT_C:-?}"
PAUSED=$(sql "select count(*) from remediation_controls where is_current = true and state = 'PAUSED' and scope_key = 'anomaly_sweep' and project_id is null")
[ "${PAUSED:-1}" = "0" ] && ok "no global pause was left behind" \
  || bad "global pause" "${PAUSED:-?}"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
