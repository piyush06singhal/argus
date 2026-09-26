#!/usr/bin/env bash
# ARGUS Phase 4 — live end-to-end smoke test (Root Cause & Causal Analysis)
#
# Verifies the causal layer against the running compose stack and the seeded
# demo project (§48–§50, §54):
#
#   0. locate the seeded demo project
#   1. the demo carries datastore telemetry and a span tree (the evidence the
#      engine needs to derive direction, not guess it)
#   2. POST /analyze runs the real engine and reports HIGH confidence
#   3. the primary hypothesis is the datastore, derived — never hard-coded
#   4. every candidate carries its score breakdown and evidence counts
#   5. the causal chain connects datastore → service → caller and validates
#   6. every edge can explain itself (evidence inspector)
#   7. hypotheses return supporting *and* contradicting evidence
#   8. the structured explanation cites stored facts and states limitations
#   9. idempotency: an unchanged evidence set returns the stored version
#  10. re-analysis appends a version and preserves history, with a diff
#  11. counterexample: a late deployment is refuted, not blamed (§49)
#  12. unknown: insufficient evidence yields UNKNOWN + INSUFFICIENT (§50)
#  13. isolation: out-of-scope project/analysis/relationship are 404
#  14. openapi exposes the whole Phase 4 surface
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
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
jb() { [ "$(python3 -c "import json,sys; d=json.load(sys.stdin); v=($1); print('true' if v is True else 'false')")" = "true" ]; }
check_status() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected=$3 got=$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== 0. Locate seeded demo project =="
DEMO=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['slug']=='argus-demo-commerce'][0]['id']")
ENVS=$(curl -fsS "$API/api/v1/projects/$DEMO/environments")
PROD=$(echo "$ENVS" | jget "[e for e in d['items'] if e['name'].lower()=='production'][0]['id']")
COMPS=$(curl -fsS "$API/api/v1/projects/$DEMO/components?page_size=100")
DB_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='PostgreSQL'][0]['id']")
CO_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='Checkout Service' and c.get('environment_id')=='$PROD'][0]['id']")
INV_ID=$(echo "$COMPS" | jget "[c for c in d['items'] if c['name']=='Inventory Service' and c.get('environment_id')=='$PROD'][0]['id']")
echo "  project: $DEMO  datastore: $DB_ID"

INCIDENTS=$(curl -fsS "$API/api/v1/incidents?project_id=$DEMO&page_size=50")
INC=$(echo "$INCIDENTS" | jget "max(d['items'], key=lambda i: i['detected_at'])['id']")
echo "  incident: $INC"

echo "== 1. The scenario carries the evidence direction needs =="
ANOMS=$(curl -fsS "$API/api/v1/anomalies?project_id=$DEMO&page_size=100")
echo "$ANOMS" | jb "any(a['component_id']=='$DB_ID' and a['anomaly_type']=='LATENCY_SPIKE' for a in d['items'])" \
  && ok "datastore latency anomaly was detected on the real component" \
  || bad "datastore anomaly" "no LATENCY_SPIKE on PostgreSQL"
TRACES=$(curl -fsS "$API/api/v1/observability/traces?project_id=$DEMO&page_size=50")
echo "$TRACES" | jb "d['total'] > 0" && ok "traces are ingestable/visible" || bad "traces" "none found"

echo "== 2. Run the engine (idempotent trigger) =="
ANALYZE=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/analyze?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"trigger":"smoke","requested_by":"e2e-smoke"}')
echo "$ANALYZE" | jb "d['status'] == 'COMPLETED'" && ok "analysis completed" || bad "analysis status" "$ANALYZE"
ANALYSIS_ID=$(echo "$ANALYZE" | jget "d['analysis_id']")
VERSION=$(echo "$ANALYZE" | jget "d['analysis_version']")
REUSED=$(echo "$ANALYZE" | jget "d['reused']")
echo "  analysis v$VERSION reused=$REUSED"

echo "== 3. The datastore is derived as the most-supported explanation =="
ANALYSIS=$(curl -fsS "$API/api/v1/incidents/$INC/causal-analysis?project_id=$DEMO")
echo "$ANALYSIS" | jb "d['overall_confidence'] in ('HIGH','MEDIUM')" \
  && ok "overall confidence is HIGH/MEDIUM" || bad "overall confidence" "$(echo "$ANALYSIS" | jget "d['overall_confidence']")"
echo "$ANALYSIS" | jb "d['primary_candidate_id'] is not None" \
  && ok "a primary hypothesis was selected" || bad "primary candidate" "UNKNOWN"
echo "$ANALYSIS" | jb "any(c['id']==d['primary_candidate_id'] and c['component_name']=='PostgreSQL' for c in d['candidates'])" \
  && ok "primary hypothesis is the datastore (component PostgreSQL)" \
  || bad "primary is datastore" "$(echo "$ANALYSIS" | jget "[c['component_name'] for c in d['candidates']]")"
echo "$ANALYSIS" | jb "d['summary'] and 'evidence-supported' in d['summary']" \
  && ok "summary states the conclusion is evidence-supported" || bad "summary wording" "$(echo "$ANALYSIS" | jget "d['summary'][:80]")"

echo "== 4. Candidates are explainable, not bare numbers =="
echo "$ANALYSIS" | jb "all(c['score_breakdown'] for c in d['candidates'])" \
  && ok "every candidate carries a documented score breakdown" || bad "score breakdown" "missing"
echo "$ANALYSIS" | jb "all(c['confidence'] for c in d['candidates'])" \
  && ok "every candidate carries a confidence bucket" || bad "confidence buckets" "missing"
echo "$ANALYSIS" | jb "all(c['component_name'] or c['event_id'] for c in d['candidates'])" \
  && ok "every candidate is labelled by component or change event" || bad "candidate labels" "unlabelled"
CAUSES=$(curl -fsS "$API/api/v1/incidents/$INC/root-causes?project_id=$DEMO")
echo "$CAUSES" | jb "d['total'] == len(d['items']) and d['total'] > 0" \
  && ok "root causes list is consistent ($(echo "$CAUSES" | jget "d['total']") items)" || bad "root causes" "$CAUSES"
echo "$CAUSES" | jb "[c['score'] for c in d['items']] == sorted([c['score'] for c in d['items']], reverse=True)" \
  && ok "candidates are ranked by score" || bad "ranking" "not sorted"

echo "== 5. The causal chain is built from evidence and validates =="
CHAIN=$(curl -fsS "$API/api/v1/incidents/$INC/causal-chain?project_id=$DEMO")
echo "$CHAIN" | jb "len(d['chain']) >= 2" && ok "chain has at least two links" || bad "chain length" "$(echo "$CHAIN" | jget "len(d['chain'])")"
echo "$CHAIN" | jb "d['valid'] is True" && ok "chain validates (no temporal contradiction)" || bad "chain validation" "$(echo "$CHAIN" | jget "d['validation_notes']")"
echo "$CHAIN" | jb "all(link['evidence_count'] > 0 and link['explanation'] for link in d['chain'])" \
  && ok "every link names its evidence" || bad "chain evidence" "a link has none"
echo "$CHAIN" | jb "all(link['relationship_type'] != 'CORRELATES_WITH' for link in d['chain'])" \
  && ok "a chain never threads through a correlation-only edge" || bad "chain types" "CORRELATES_WITH in chain"

echo "== 6. Every edge can explain itself (§41) =="
GRAPH=$(curl -fsS "$API/api/v1/incidents/$INC/causal-graph?project_id=$DEMO")
EDGE_TOTAL=$(echo "$GRAPH" | jget "len(d['edges'])")
[ "$EDGE_TOTAL" -gt 0 ] && ok "causal graph has $EDGE_TOTAL edge(s)" || bad "graph edges" "none"
echo "$GRAPH" | jb "all(e['explanation'] and e['supporting_evidence_count'] > 0 for e in d['edges'])" \
  && ok "every edge carries an explanation and supporting facts" || bad "edge evidence" "an edge has none"
echo "$GRAPH" | jb "d['disclaimer'] and 'not proven' in d['disclaimer']" \
  && ok "graph ships its disclaimer" || bad "graph disclaimer" "missing"
FIRST_EDGE=$(echo "$GRAPH" | jget "d['edges'][0]['id']")
EDGE_EXPL=$(curl -fsS "$API/api/v1/incidents/$INC/relationships/$FIRST_EDGE/explanation?project_id=$DEMO")
echo "$EDGE_EXPL" | jb "d['explanation'] and d['relationship_id']=='$FIRST_EDGE'" \
  && ok "edge explanation endpoint explains why ARGUS believes the edge" || bad "edge explanation" "$EDGE_EXPL"
echo "$EDGE_EXPL" | jb "d['directional'] == (d['relationship_type'] != 'CORRELATES_WITH')" \
  && ok "correlation is never presented as direction" || bad "edge directionality" "mislabelled"

echo "== 7. Hypotheses split supporting and contradicting evidence (§24, §28) =="
HYP=$(curl -fsS "$API/api/v1/incidents/$INC/hypotheses?project_id=$DEMO")
echo "$HYP" | jb "d['items'] and d['items'][0]['candidate']['id'] == d['primary_candidate_id']" \
  && ok "the primary hypothesis is listed first" || bad "hypothesis ordering" "primary not first"
echo "$HYP" | jb "all(item['why_confidence_differs'] for item in d['items'])" \
  && ok "every hypothesis states why its confidence differs" || bad "confidence rationale" "missing"
echo "$HYP" | jb "all(len(item['supporting']) + len(item['contradicting']) + len(item['neutral']) == (item['candidate']['supporting_evidence_count'] + item['candidate']['contradicting_evidence_count'] + item['candidate']['neutral_evidence_count']) for item in d['items'])" \
  && ok "evidence splits reconcile with the candidate counts" || bad "evidence reconciliation" "mismatch"
EV=$(curl -fsS "$API/api/v1/incidents/$INC/evidence-analysis?project_id=$DEMO")
echo "$EV" | jb "d['candidates'] and len(d['candidates']) == $(echo "$HYP" | jget "len(d['items'])")" \
  && ok "evidence-analysis covers exactly the same candidates" || bad "evidence analysis" "$EV"
echo "$EV" | jb "(not d['missing_evidence']) or all(isinstance(x, str) and x for x in d['missing_evidence'])" \
  && ok "evidence-analysis enumerates what was missing" || bad "missing evidence" "malformed"

echo "== 8. Structured explanation cites stored facts and states limits (§30, §37) =="
EXPL=$(curl -fsS "$API/api/v1/incidents/$INC/causal-analysis/$ANALYSIS_ID/explanation?project_id=$DEMO")
echo "$EXPL" | jb "bool(d['headline']) and bool(d['narrative']) and bool(d['disclaimer'])" \
  && ok "explanation has headline, narrative and disclaimer" || bad "explanation shape" "$EXPL"
echo "$EXPL" | jb "d['primary'] and all(r.startswith('[') for r in d['primary']['reasons'])" \
  && ok "reasons are category-tagged, derived from stored facts" || bad "explanation reasons" "not category-tagged"
echo "$EXPL" | jb "bool(d['limitations'])" && ok "limitations are stated explicitly" || bad "limitations" "none stated"
echo "$EXPL" | jb "'prove' in d['disclaimer'] or 'proof' in d['disclaimer']" \
  && ok "the explanation refuses to claim proof" || bad "proof disclaimer" "$(echo "$EXPL" | jget "d['disclaimer']")"

echo "== 9. Idempotency (§34) =="
SECOND=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/analyze?project_id=$DEMO" \
  -H 'Content-Type: application/json' -d '{"trigger":"smoke-repeat"}')
echo "$SECOND" | jb "d['reused'] is True and d['analysis_version'] == $VERSION" \
  && ok "an unchanged evidence set returns the stored version" || bad "idempotency" "$SECOND"

echo "== 10. Re-analysis versions the result instead of overwriting it (§35, §43) =="
FORCED=$(curl -fsS -X POST "$API/api/v1/incidents/$INC/analyze?project_id=$DEMO&force=true" \
  -H 'Content-Type: application/json' -d '{"trigger":"smoke-force"}')
echo "$FORCED" | jb "d['reused'] is False and d['analysis_version'] > $VERSION" \
  && ok "force=true appends a new version" || bad "forced re-analysis" "$FORCED"
HISTORY=$(curl -fsS "$API/api/v1/incidents/$INC/causal-analysis/history?project_id=$DEMO")
echo "$HISTORY" | jb "d['total'] >= 2 and d['items'][0]['analysis_version'] > d['items'][1]['analysis_version']" \
  && ok "history preserves earlier versions, newest first" || bad "history" "$HISTORY"
echo "$HISTORY" | jb "d['items'][0]['diff'] is not None and d['items'][-1]['diff'] is None" \
  && ok "history reports what changed between versions" || bad "history diff" "missing"

echo "== 11. Counterexample: a late deployment is refuted, not blamed (§49) =="
echo "$GRAPH" | jb "all(e['temporal_alignment_seconds'] is None or e['temporal_alignment_seconds'] >= 0 for e in d['edges'])" \
  && ok "no stored edge has an effect preceding its cause" || bad "edge temporal sanity" "an edge is impossible"
echo "$GRAPH" | jb "all(e['relationship_type'] != 'TRIGGERS' or e['temporal_alignment_seconds'] is not None for e in d['edges'])" \
  && ok "no directional edge is recorded without a measured time offset" || bad "edge alignment" "missing offset"
DEP_COUNT=$(curl -fsS "$API/api/v1/deployments?project_id=$DEMO&page_size=50" | jget "len(d['items'])")
echo "  demo deployments in scope: $DEP_COUNT"
# The stored contradiction itself (a deployment after onset cannot explain the
# onset) is exercised end to end by the pytest scenario suite and by the live
# scenario runner; here we assert the invariant that no *stored* edge claims a
# cause that happened after its effect.

echo "== 12. Honest outcomes are reachable: an evidence-free incident says UNKNOWN (§50) =="
SCRATCH_SLUG="smoke-causal-harness"
SCRATCH=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "next((p['id'] for p in d['items'] if p['slug']=='$SCRATCH_SLUG'), '')")
if [ -z "$SCRATCH" ]; then
  SCRATCH=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
    -d "{\"name\":\"Smoke Causal Harness\",\"slug\":\"$SCRATCH_SLUG\"}" | jget "d['id']")
fi
echo "  scratch project: $SCRATCH"
EMPTY_INC=$(curl -fsS -X POST "$API/api/v1/incidents" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$SCRATCH\",\"title\":\"Evidence-free smoke incident\",\"severity\":\"LOW\",\"detected_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)\"}" | jget "d['id']")
curl -fsS -X POST "$API/api/v1/incidents/$EMPTY_INC/analyze?project_id=$SCRATCH" \
  -H 'Content-Type: application/json' -d '{"trigger":"smoke-unknown"}' > /dev/null
EMPTY_ANALYSIS=$(curl -fsS "$API/api/v1/incidents/$EMPTY_INC/causal-analysis?project_id=$SCRATCH")
echo "$EMPTY_ANALYSIS" | jb "d['overall_confidence'] == 'INSUFFICIENT' and d['primary_candidate_id'] is None" \
  && ok "no evidence yields UNKNOWN at INSUFFICIENT confidence" || bad "unknown outcome" "$EMPTY_ANALYSIS"
echo "$EMPTY_ANALYSIS" | jb "'insufficient evidence' in d['summary'].lower()" \
  && ok "the refusal is stated in plain language" || bad "refusal wording" "$(echo "$EMPTY_ANALYSIS" | jget "d['summary'][:60]")"
echo "$EMPTY_ANALYSIS" | jb "bool(d['missing_evidence'])" \
  && ok "the refusal enumerates the missing evidence" || bad "missing evidence" "$(echo "$EMPTY_ANALYSIS" | jget "d['missing_evidence']")"

echo "== 13. Isolation (§45) =="
OTHER=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "[p for p in d['items'] if p['id']!='$DEMO'][0]['id']")
check_status "out-of-scope project on causal-analysis is 404" \
  "$(code "$API/api/v1/incidents/$INC/causal-analysis?project_id=$OTHER")" "404"
check_status "out-of-scope project on analyze is 404" \
  "$(code -X POST "$API/api/v1/incidents/$INC/analyze?project_id=$OTHER")" "404"
check_status "unknown analysis id is 404" \
  "$(code "$API/api/v1/incidents/$INC/causal-analysis?project_id=$DEMO&analysis_id=00000000-0000-0000-0000-000000000000")" "404"
check_status "unknown relationship id is 404" \
  "$(code "$API/api/v1/incidents/$INC/relationships/00000000-0000-0000-0000-000000000000/explanation?project_id=$DEMO")" "404"
check_status "another incident's analysis id is 404" \
  "$(code "$API/api/v1/incidents/$EMPTY_INC/causal-analysis?project_id=$DEMO&analysis_id=$ANALYSIS_ID")" "404"

echo "== 14. The whole Phase 4 surface is published =="
SPEC=$(curl -fsS "$API/openapi.json")
for PATH_SUFFIX in \
  "/incidents/{incident_id}/analyze" \
  "/incidents/{incident_id}/causal-analysis" \
  "/incidents/{incident_id}/causal-analysis/history" \
  "/incidents/{incident_id}/root-causes" \
  "/incidents/{incident_id}/causal-graph" \
  "/incidents/{incident_id}/causal-chain" \
  "/incidents/{incident_id}/hypotheses" \
  "/incidents/{incident_id}/evidence-analysis" \
  "/incidents/{incident_id}/causal-analysis/{analysis_id}/explanation" \
  "/incidents/{incident_id}/relationships/{relationship_id}/explanation" ; do
  echo "$SPEC" | jb "'/api/v1$PATH_SUFFIX' in d['paths']" \
    && ok "openapi documents $PATH_SUFFIX" || bad "openapi $PATH_SUFFIX" "missing"
done

echo "== 15. The RCA views render for a real engineer (§39–§43) =="
RCA=$(curl -fsS "$WEB/incidents/$INC/causal-analysis")
web_has() {
  if printf '%s' "$2" | grep -qF "$3"; then ok "$1"; else bad "$1" "missing '$3'"; fi
}
web_has "RCA page shows the primary candidate" "$RCA" "Primary candidate"
web_has "RCA page names the derived component" "$RCA" "PostgreSQL"
web_has "RCA page renders the causal graph" "$RCA" "Causal graph"
web_has "RCA page renders the validated chain" "$RCA" "Validated"
web_has "RCA page shows alternatives" "$RCA" "Alternative hypotheses"
web_has "RCA page shows version history" "$RCA" "Analysis history"
web_has "RCA page syncs with the timeline" "$RCA" "Select an event to highlight the graph node"
web_has "RCA page carries the proof disclaimer" "$RCA" "does not constitute mathematical proof"
INDEX=$(curl -fsS "$WEB/incidents/rca")
web_has "RCA index lists incidents" "$INDEX" "Open analysis"
web_has "RCA index carries the disclaimer" "$INDEX" "evidence-supported hypotheses"

UNANALYSED=$(curl -fsS -X POST "$API/api/v1/incidents" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$SCRATCH\",\"title\":\"Awaiting analysis\",\"severity\":\"LOW\",\"detected_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)\"}" | jget "d['id']")
EMPTY_PAGE=$(curl -fsS "$WEB/incidents/$UNANALYSED/causal-analysis")
web_has "an unanalysed incident says so instead of erroring" "$EMPTY_PAGE" "No analysis yet"
web_has "and offers to run the analysis" "$EMPTY_PAGE" "Run analysis"

echo "== 16. Several systems at once must not contaminate each other (§45) =="
BEFORE=$(curl -fsS "$API/api/v1/incidents/$INC/causal-analysis?project_id=$DEMO")
BEFORE_PRIMARY=$(echo "$BEFORE" | jget "(d['primary_candidate_id'] or '')")
BEFORE_CONF=$(echo "$BEFORE" | jget "d['overall_confidence']")
# Two projects are analysed in the same run. The demo project has a fully
# explained incident; a second project holding an incident with no evidence of
# its own must still say UNKNOWN — if the engine read another project's
# telemetry, that incident would come back with candidates.
SECOND_SLUG="smoke-causal-harness-2"
SECOND=$(curl -fsS "$API/api/v1/projects?page_size=100" | jget "next((p['id'] for p in d['items'] if p['slug']=='$SECOND_SLUG'), '')")
if [ -z "$SECOND" ]; then
  SECOND=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
    -d "{\"name\":\"Smoke Causal Harness Two\",\"slug\":\"$SECOND_SLUG\"}" | jget "d['id']")
fi
OTHER_INC=$(curl -fsS -X POST "$API/api/v1/incidents" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$SECOND\",\"title\":\"Second system incident\",\"severity\":\"HIGH\",\"detected_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)\"}" | jget "d['id']")
curl -fsS -X POST "$API/api/v1/incidents/$OTHER_INC/analyze?project_id=$SECOND" \
  -H 'Content-Type: application/json' -d '{"trigger":"smoke-multisystem"}' > /dev/null
OTHER_ANALYSIS=$(curl -fsS "$API/api/v1/incidents/$OTHER_INC/causal-analysis?project_id=$SECOND")
echo "$OTHER_ANALYSIS" | jb "d['primary_candidate_id'] is None and not d['candidates']" \
  && ok "a second system's evidence-free incident stays UNKNOWN" \
  || bad "cross-project leakage" "candidates appeared from another project"
echo "$OTHER_ANALYSIS" | jb "d['project_id'] == '$SECOND'" \
  && ok "the second analysis is owned by the second project" || bad "analysis ownership" "wrong project"
# The demo system's own analysis must reference only its own components.
OWNER_COMPS=$(curl -fsS "$API/api/v1/projects/$DEMO/components?page_size=100" | jget "json.dumps([c['id'] for c in d['items']])")
if python3 -c "
import json, sys
owned = set(json.loads(sys.argv[2]))
candidates = json.loads(sys.argv[1])['candidates']
foreign = [c['component_id'] for c in candidates if c.get('component_id') and c['component_id'] not in owned]
raise SystemExit(1 if foreign else 0)
" "$ANALYSIS" "$OWNER_COMPS"; then
  ok "every candidate belongs to the project being analysed"
else
  bad "cause locality" "a candidate referenced another system's component"
fi
# Both systems coexist: the demo's analysis is unaffected by the second run.
AFTER=$(curl -fsS "$API/api/v1/incidents/$INC/causal-analysis?project_id=$DEMO")
echo "$AFTER" | jb "(d['primary_candidate_id'] or '') == '$BEFORE_PRIMARY' and d['overall_confidence'] == '$BEFORE_CONF'" \
  && ok "the first system's conclusion is unchanged by the second" \
  || bad "cross-project interference" "the demo's analysis changed"

if [ "${DDL_PROBE:-0}" = "1" ]; then
  # Opt-in (`DDL_PROBE=1 bash infrastructure/e2e-smoke-phase4.sh`): it resets the
  # causal tables, so it is not part of the default run. It exists because a
  # schema change under a *live* pool used to make the next request 500 —
  # asyncpg's cached statement plans were invalidated and the error surfaced to
  # the caller. The engine now disables that cache, and this pins the behaviour.
  echo "== 17. A migration under a live pool must not fail the next request =="
  docker compose exec -T api alembic downgrade -1 >/dev/null 2>&1 \
    && ok "phase 4 migration downgrades cleanly" || bad "downgrade" "exit code"
  docker compose exec -T api alembic upgrade head >/dev/null 2>&1 \
    && ok "phase 4 migration reapplies cleanly" || bad "upgrade" "exit code"
  PROBE=$(code -X POST "$API/api/v1/incidents/$INC/analyze?project_id=$DEMO&force=true" \
    -H 'Content-Type: application/json' -d '{"trigger":"ddl-probe"}')
  check_status "the first request after a live migration succeeds" "$PROBE" "200"
fi

echo
echo "==================== PHASE 4 SMOKE SUMMARY ===================="
echo "  passed: $PASS"
echo "  failed: $FAIL"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
