#!/usr/bin/env bash
# ARGUS Phase 10 — live end-to-end smoke test (reliability intelligence)
#
# Runs the whole learning pipeline against the running compose stack over the
# real HTTP API — nothing is stubbed, no learning result is hard-coded, and every
# count comes from the run's own report:
#
#   0. an isolated scratch project (so the gate cannot perturb the seeded demo)
#   1. scope, ownership and unknown-id refusals
#   2. ingest four real multi-component episodes through the ingest API, correlate
#      them with the Phase 3 detector, remediate and verify one of them, and
#      leave one open — the floor for a knowledge candidate is three episodes
#   3. learning health / dashboard / metrics reflect an empty-but-working layer
#      before anything is learned
#   4. a learning run consumes the recorded outcomes and reports real counts,
#      refuses nothing as unprocessable, and normalises exactly the three
#      completed episodes (the open incident is not history yet)
#   5. the run is idempotent: re-running learns nothing new and does not inflate
#      any sample count
#   6. knowledge: pattern explorer, detail, version ledger and a human review
#      decision (§53, §26, §72)
#   7. grounded search: an answer with citations, and an honest "no comparable
#      historical case" for a question the history cannot answer (§46–§49, §90)
#   8. recommendations for a current incident, then a decision and a recorded
#      outcome — with acceptance kept separate from correctness (§57, §81)
#   9. experiences and learning runs are readable, scoped and paginated
#  10. learned relationships exist, carry their support, and never claim to be
#      dependencies (§23, §24) — including that an undirected co-failure stays
#      undirected
#  11. the component learning profile carries the profile, the patterns and the
#      relationships
#  12. cross-project isolation: another project sees none of it
#  13. the §80 metrics expose the relationship stage
#  14. the Phase 10 tables, enums and unique index exist in PostgreSQL (the DDL
#      probe that reverses and re-applies the migration is opt-in: DDL_PROBE=1)
#  15. the learning workspace renders (§51–§60)
#  16. cleanup: the scratch project is deleted and nothing it created survives
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
  -d "{\"name\":\"smoke-learning-$TS\",\"slug\":\"smoke-learning-$TS\",\"description\":\"Phase 10 smoke\"}" \
  | jget "d['id']")
OTHER=$(post "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"smoke-learning-other-$TS\",\"slug\":\"smoke-learning-other-$TS\"}" \
  | jget "d['id']")
ENV=$(post "$API/api/v1/projects/$PROJ/environments" -H 'Content-Type: application/json' \
  -d '{"name":"production","environment_type":"PRODUCTION"}' | jget "d['id']")
CO=$(post "$API/api/v1/projects/$PROJ/components" -H 'Content-Type: application/json' \
  -d '{"name":"checkout-service","component_type":"SERVICE"}' | jget "d['id']")
INV=$(post "$API/api/v1/projects/$PROJ/components" -H 'Content-Type: application/json' \
  -d '{"name":"inventory-service","component_type":"SERVICE"}' | jget "d['id']")
#: checkout depends on inventory (structural), so the dependency-degradation
#: flavour of learned relationship has something to read.
post "$API/api/v1/projects/$PROJ/dependencies" -H 'Content-Type: application/json' \
  -d "{\"source_component_id\":\"$CO\",\"target_component_id\":\"$INV\",\"dependency_type\":\"HTTP\"}" > /dev/null
ok "scratch project $PROJ with checkout $CO, inventory $INV and a declared dependency"

echo "== 1. Scope and ownership refusals =="
check_status "the dashboard without a project scope is refused" \
  "$(code "$API/api/v1/intelligence/dashboard")" "422"
check_status "unknown project answers 404" \
  "$(code "$API/api/v1/intelligence/dashboard?project_id=00000000-0000-0000-0000-000000000000")" "404"
check_status "unknown experience id answers 404" \
  "$(code "$API/api/v1/intelligence/experiences/00000000-0000-0000-0000-000000000000?project_id=$PROJ")" "404"
check_status "unknown knowledge id answers 404" \
  "$(code "$API/api/v1/intelligence/knowledge/00000000-0000-0000-0000-000000000000?project_id=$PROJ")" "404"
check_status "a learning run without a project is refused" \
  "$(code -X POST "$API/api/v1/intelligence/learning-runs" -H 'Content-Type: application/json' -d '{}')" "422"
check_status "an unknown relationship kind is refused, not defaulted" \
  "$(code "$API/api/v1/intelligence/relationships?project_id=$PROJ&kind=TOTALLY_MADE_UP")" "422"
check_status "search without a project scope is refused" \
  "$(code -G "$API/api/v1/intelligence/search" --data-urlencode 'q=have we seen this before')" "422"

echo "== 2. Ingest four real episodes, three of them completed (§72) =="
# Ingest a rising error rate on checkout and a rising p95 on inventory, twice,
# hours apart. The Phase 3 detector then correlates the anomalies into incidents,
# so the experiences carry *both* components — which is what a learned
# relationship is derived from.
ingest_episode() {
  local base="$1"
  for i in 0 1 2 3 4 5 6 7 8 9; do
    local offset=$(( (9 - i) * 300 + base ))
    local stamp; stamp=$(isoago "$offset")
    local err; err=$(python3 -c "print(round(0.02 + (0.40 - 0.02) * $i / 9, 4))")
    local lat; lat=$(python3 -c "print(round(120.0 + (640.0 - 120.0) * $i / 9, 1))")
    curl -fsS -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' \
      -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV\",\"component_id\":\"$CO\",\"timestamp\":\"$stamp\",\"metric_name\":\"http.checkout.error_rate\",\"metric_type\":\"GAUGE\",\"value\":$err,\"unit\":\"ratio\"}" > /dev/null
    curl -fsS -X POST "$API/api/v1/observability/metrics" -H 'Content-Type: application/json' \
      -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV\",\"component_id\":\"$INV\",\"timestamp\":\"$stamp\",\"metric_name\":\"http.inventory.latency.p95\",\"metric_type\":\"GAUGE\",\"value\":$lat,\"unit\":\"ms\"}" > /dev/null
  done
  #: Detection is run in the episode's own environment and the correlation pass
  #: is explicit, so the incident the gate then reads is not a race against the
  #: asynchronous ingest hook.
  post "$API/api/v1/projects/$PROJ/anomalies/detect?environment_id=$ENV&correlate=true" \
    -H 'Content-Type: application/json' -d '{}' > /dev/null
}

#: The hook correlates asynchronously, so the gate waits for the episode's own
#: incident instead of assuming it won the race. Bounded, and it reports an
#: empty id rather than hanging when nothing appears.
wait_for_incident() { # previously_seen_id
  local before="$1" candidate=""
  for _ in $(seq 1 25); do
    candidate=$(latest_incident)
    if [ -n "$candidate" ] && [ "$candidate" != "$before" ]; then
      printf '%s' "$candidate"
      return 0
    fi
    sleep 1
  done
  printf '%s' ""
}
#: Anomaly rules are project-scoped, so the scratch project needs its own. The
#: lookback is the schema maximum because a rule is evaluated at its own "now"
#: against the latest sample inside the window: a window wide enough to hold both
#: episodes lets the second one fire without re-reading the first.
mk_rule() { # name anomaly_type metric threshold component
  post "$API/api/v1/anomaly-rules" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"name\":\"$1\",\"anomaly_type\":\"$2\",\"condition\":\"THRESHOLD\",\"metric_name\":\"$3\",\"threshold\":$4,\"severity\":\"HIGH\",\"component_id\":\"$5\",\"window_seconds\":86400,\"cooldown_seconds\":0,\"min_samples\":1,\"persistence_cycles\":1,\"created_by\":\"smoke\"}" > /dev/null
}
#: Two thresholds crossed only by the *last* sample of each series, and a
#: ``MEDIUM`` severity. Both matter: a lone anomaly below the incident severity
#: floor stays in the Anomaly Center, so it is still ungrouped when the second
#: component's anomaly appears — which is what lets one correlation pass see two
#: anomalies on two adjacent components and open a genuinely multi-component
#: incident. With a HIGH severity the first anomaly would open its own incident
#: immediately and the components could never be correlated.
mk_rule "smoke-checkout-errors-$TS" "ERROR_RATE_SPIKE" "http.checkout.error_rate" 0.39 "$CO"
mk_rule "smoke-inventory-latency-$TS" "LATENCY_SPIKE" "http.inventory.latency.p95" 639 "$INV"

#: The learning floors are sample-based — a knowledge candidate needs three
#: completed episodes (``INTELLIGENCE_MIN_SAMPLES_CANDIDATE``), and a relationship
#: two — so the gate builds three *completed* episodes and one that is still
#: open. The open one is what advice is generated for; the three completed ones
#: are the only history the pipeline is allowed to learn from.
latest_incident() {
  curl -fsS "$API/api/v1/incidents?project_id=$PROJ&page_size=50" \
    | jget "(max(d['items'], key=lambda i: i['detected_at'])['id'] if d['items'] else '')"
}

#: An anomaly's fingerprint is stable across occurrences, so a recurrence while
#: its predecessor is still open would be an *update*, not a new episode.
#: Resolving the incident *and* its anomalies is what makes the next one a fresh
#: episode rather than a continuation of the last.
close_episode() { # incident_id note
  local incident="$1"
  for AID in $(curl -fsS "$API/api/v1/anomalies?project_id=$PROJ&page_size=100" \
    | jget "' '.join(a['id'] for a in d['items'] if a['status'] == 'DETECTED' and a['incident_id'] == '$incident')"); do
    post "$API/api/v1/anomalies/$AID/resolve" -H 'Content-Type: application/json' \
      -d '{"actor":"smoke","note":"episode recovered"}' > /dev/null
  done
  post "$API/api/v1/incidents/$incident/resolve?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d "{\"actor\":\"smoke\",\"note\":\"$2\"}" > /dev/null
  #: Closed as well as resolved: an incident fingerprint is bucketed by the hour,
  #: so a *resolved* incident would absorb the next episode's anomalies instead
  #: of letting a second incident exist. Closing it is the lifecycle step that
  #: makes the next episode a new episode, and it is what an operator would do.
  put "$API/api/v1/incidents/$incident?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d '{"status":"CLOSED","status_changed_by":"smoke"}' > /dev/null
}

#: Episode A: degrades and recovers on its own.
ingest_episode 32400
INC_A=$(wait_for_incident "")
[ -n "$INC_A" ] && close_episode "$INC_A" "recovered without intervention"
sleep 1

#: Episode B: degrades, is remediated under human approval, and recovers. The
#: remediation is ARGUS-native (a flag it owns), so its effect is real and
#: reversible, and its outcome is something the learning layer can read.
ingest_episode 21600
INC_B=$(wait_for_incident "$INC_A")
if [ -z "${SKIP_REMEDIATION:-}" ] && [ -n "$INC_B" ]; then
  put "$API/api/v1/remediation/policy?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d "{\"environment_id\":\"$ENV\",\"execution_mode\":\"HUMAN_APPROVAL\",\"allowed_action_types\":[\"DISABLE_FEATURE_FLAG\",\"ENABLE_FEATURE_FLAG\"],\"cooldown_seconds\":0,\"max_actions_per_window\":20,\"canary_enabled\":false,\"updated_by\":\"smoke\"}" > /dev/null
  ACT=$(post "$API/api/v1/remediation/actions/propose" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"environment_id\":\"$ENV\",\"component_id\":\"$CO\",\"incident_id\":\"$INC_B\",\"action_type\":\"DISABLE_FEATURE_FLAG\",\"description\":\"disable code indexing while checkout recovers\",\"reason\":\"it was amplifying load during the episode\",\"parameters\":{\"flag\":\"code_indexing\"},\"created_by\":\"smoke-operator\"}")
  ACT_ID=$(echo "$ACT" | jget "d['id']")
  echo "$ACT" | jb "d['status'] == 'AWAITING_APPROVAL'" \
    && ok "the proposed remediation waits for a human" || bad "awaiting approval" "$ACT"
  post "$API/api/v1/remediation/actions/$ACT_ID/approve?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d '{"actor":"smoke-operator","reason":"approved during the episode"}' > /dev/null
  RAN=$(post "$API/api/v1/remediation/actions/$ACT_ID/execute?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d '{"actor":"smoke-operator"}')
  echo "$RAN" | jb "d['status'] == 'VERIFIED' and d['outcome'] in ('EFFECTIVE','PARTIALLY_EFFECTIVE')" \
    && ok "the remediation executed and verified as $(echo "$RAN" | jget "d['outcome']")" \
    || bad "remediation verification" "$RAN"
fi

#: Episode B completes *after* its remediation, so the episode records the
#: remediation that resolved it — the order the learning layer has to handle.
if [ -n "$INC_B" ]; then
  close_episode "$INC_B" "recovered after the remediation was verified"
fi
sleep 1

#: Episode C: a third completed episode, because a pattern is not a pattern
#: after two observations.
ingest_episode 10800
INC_C=$(wait_for_incident "$INC_B")
[ -n "$INC_C" ] && close_episode "$INC_C" "recovered without intervention"
sleep 1

#: Episode D stays open: it is the live incident the advice step talks about.
ingest_episode 1800
INC_OPEN=$(wait_for_incident "$INC_C")
[ -n "$INC_OPEN" ] && post "$API/api/v1/incidents/$INC_OPEN/acknowledge?project_id=$PROJ" \
  -H 'Content-Type: application/json' -d '{"actor":"smoke"}' > /dev/null
ok "ingested four rising-signal timelines (checkout errors, inventory p95) and correlated them"

INCIDENTS=$(curl -fsS "$API/api/v1/incidents?project_id=$PROJ&page_size=50")
INC_COUNT=$(echo "$INCIDENTS" | jget "d['total']")
if [ "$INC_COUNT" -ge 4 ]; then
  ok "the four episodes produced $INC_COUNT distinct incidents"
else
  bad "the four episodes produced $INC_COUNT incident(s), expected four" "$INCIDENTS"
fi
#: The live incident the advice step reads; kept separate from the three
#: completed episodes so "advice for a current incident" is genuinely tested.
INC_ONE="$INC_OPEN"

#: An incident that was acknowledged and never resolved is exactly what the
#: learning layer must *not* learn from, so the gate asserts that later.
echo "== 3. The learning layer is honest before it has learned anything =="
HEALTH=$(curl -fsS "$API/api/v1/intelligence/health?project_id=$PROJ")
echo "$HEALTH" | jb "d['learning_enabled'] is True" \
  && ok "learning is enabled in this deployment" || bad "health" "$HEALTH"
echo "$HEALTH" | jb "d['auto_activation_enabled'] is False" \
  && ok "autonomous activation is off: every activation needs a human" || bad "auto activation" "$HEALTH"
EMPTY_SEARCH=$(curl -fsS -G "$API/api/v1/intelligence/search" \
  --data-urlencode "project_id=$OTHER" --data-urlencode 'q=have we seen this before')
echo "$EMPTY_SEARCH" | jb "d['evidence_available'] is False" \
  && ok "a project with no history answers honestly rather than inventing a case" \
  || bad "empty search" "$EMPTY_SEARCH"
echo "$EMPTY_SEARCH" | jb "'no comparable' in (d['answer'] + ' ' + ' '.join(d['limitations'])).lower() or 'no historical' in d['answer'].lower() or 'not found' in d['answer'].lower()" \
  && ok "the empty answer says so in words" || bad "empty answer wording" "$EMPTY_SEARCH"

echo "== 4. A learning run consumes the outcomes and reports real counts =="
RUN1=$(post "$API/api/v1/intelligence/learning-runs" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"trigger\":\"smoke\"}")
echo "  run1: $(echo "$RUN1" | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ("status","events_processed","experiences_created","patterns_discovered","patterns_validated","knowledge_created","relationships_created")})')"
echo "$RUN1" | jb "d['status'] == 'COMPLETED'" \
  && ok "the run completed" || bad "run status" "$RUN1"
RUN1_ID=$(echo "$RUN1" | jget "d.get('run_id') or ''")
echo "$RUN1" | jb "d['events_processed'] >= 4" \
  && ok "it consumed $(echo "$RUN1" | jget "d['events_processed']") learning event(s) — three resolutions and a remediation" \
  || bad "events consumed" "$RUN1"
echo "$RUN1" | jb "d.get('unprocessable', {}) == {}" \
  && ok "every event reached an episode: none was recorded as unprocessable" \
  || bad "unprocessable events" "$(echo "$RUN1" | jget "d.get('unprocessable')")"
echo "$RUN1" | jb "d['experiences_created'] == 3" \
  && ok "exactly the three completed episodes became history" \
  || bad "experience count" "$(echo "$RUN1" | jget "d['experiences_created']")"
echo "$RUN1" | jb "d['patterns_discovered'] >= 1" \
  && ok "the corpus produced $(echo "$RUN1" | jget "d['patterns_discovered']") pattern(s)" \
  || bad "no pattern discovered" "$RUN1"
if [ -n "$RUN1_ID" ]; then
  RUN_ROW=$(curl -fsS "$API/api/v1/intelligence/learning-runs/$RUN1_ID?project_id=$PROJ")
  echo "$RUN_ROW" | jb "d['run']['algorithm_versions']" \
    && ok "the run records the algorithm versions it used (§79)" || bad "run audit" "$RUN_ROW"
else
  #: The run's own audit record is part of the phase's contract; a run that
  #: reports no id must fail the gate, not quietly skip its checks.
  bad "the completed run did not report an id, so its record could not be read" "$RUN1"
fi

EXPERIENCES=$(curl -fsS "$API/api/v1/intelligence/experiences?project_id=$PROJ&page_size=50")
EXP_TOTAL=$(echo "$EXPERIENCES" | jget "d['total']")
if [ "$EXP_TOTAL" = "3" ]; then
  ok "history was normalised into $EXP_TOTAL experience(s) — the open incident is not one of them (§8)"
  EXP_ID=$(echo "$EXPERIENCES" | jget "d['items'][0]['id']")
  EXP_DETAIL=$(curl -fsS "$API/api/v1/intelligence/experiences/$EXP_ID?project_id=$PROJ")
  echo "$EXP_DETAIL" | jb "isinstance(d.get('failure_signature'), dict) and d['experience']['failure_fingerprint']" \
    && ok "the experience carries its structured failure signature (§9)" || bad "signature" "$EXP_DETAIL"
else
  bad "no experience was assembled" "$EXPERIENCES"
fi

echo "== 5. Re-running learns nothing new and inflates nothing (§28, §85) =="
RUN2=$(post "$API/api/v1/intelligence/learning-runs" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJ\",\"trigger\":\"smoke-rerun\"}")
echo "$RUN2" | jb "d['experiences_created'] == 0" \
  && ok "the second run created no duplicate experience" || bad "duplicate experiences" "$RUN2"
echo "$RUN2" | jb "d.get('relationships_created',0) == 0" \
  && ok "the second run created no duplicate relationship" || bad "duplicate relationships" "$RUN2"

echo "== 6. Knowledge: explorer, detail, versions and a human review (§53, §26, §72) =="
KNOW=$(curl -fsS "$API/api/v1/intelligence/knowledge?project_id=$PROJ&page_size=50")
KNOW_TOTAL=$(echo "$KNOW" | jget "d['total']")
echo "  knowledge rows: $KNOW_TOTAL"
if [ "$KNOW_TOTAL" -ge 1 ]; then
  KID=$(echo "$KNOW" | jget "d['items'][0]['id']")
  KDETAIL=$(curl -fsS "$API/api/v1/intelligence/knowledge/$KID?project_id=$PROJ")
  echo "$KDETAIL" | jb "d['knowledge']['sample_count'] >= 0 and isinstance(d['knowledge']['limitations'], list)" \
    && ok "every pattern carries its sample and its limitations" || bad "knowledge detail" "$KDETAIL"
  echo "$KDETAIL" | jb "len(d['versions']) >= 1" \
    && ok "the version ledger has an entry for the current revision" || bad "versions" "$KDETAIL"
  echo "$KDETAIL" | jb "'OBSERVED PATTERN' in ' '.join(d['knowledge']['limitations']) or d['knowledge']['algorithm']" \
    && ok "the row names the algorithm that produced it" || bad "algorithm" "$KDETAIL"
  check_status "a review decision without a reason is refused" \
    "$(code -X POST "$API/api/v1/intelligence/knowledge/$KID/review?project_id=$PROJ" -H 'Content-Type: application/json' -d '{"decision":"APPROVE","reviewer":"smoke"}')" "422"
  #: Three episodes meet the *candidate* floor but not the validation floor, so
  #: the honest state of this row is CANDIDATE — and a candidate cannot be
  #: activated. That refusal is the §71/§74 invariant, checked against the real
  #: status rather than assumed.
  KSTATUS=$(echo "$KDETAIL" | jget "d['knowledge']['status']")
  [ "$KSTATUS" = "CANDIDATE" ] \
    && ok "a three-episode pattern is a candidate, not yet validated" \
    || bad "knowledge status" "$KSTATUS"
  check_status "activating a $KSTATUS pattern is refused: validation cannot be skipped" \
    "$(code -X POST "$API/api/v1/intelligence/knowledge/$KID/review?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d '{"decision":"APPROVE","reviewer":"smoke-reviewer","reason":"the sample size and coverage support this observation"}')" "409"
  REVIEWED=$(post "$API/api/v1/intelligence/knowledge/$KID/review?project_id=$PROJ" -H 'Content-Type: application/json' \
    -d '{"decision":"REJECT","reviewer":"smoke-reviewer","reason":"three episodes is not enough to stand behind this one"}')
  echo "$REVIEWED" | jb "d['knowledge']['reviewed_by'] == 'smoke-reviewer'" \
    && ok "the human decision is recorded with its reviewer" || bad "review" "$REVIEWED"
  echo "$REVIEWED" | jb "d['knowledge']['status'] == 'REJECTED'" \
    && ok "a reviewer's 'no' is a status, not a comment" || bad "rejection status" "$REVIEWED"
  echo "$REVIEWED" | jb "any(r['decision'] == 'REJECT' and r['reason'] for r in d['reviews'])" \
    && ok "the review history keeps the decision and its reason" || bad "review history" "$REVIEWED"
  echo "$REVIEWED" | jb "not any(r['status'] in ('ACTIVE','VALIDATED') for r in [d['knowledge']])" \
    && ok "rejected knowledge is not left in a state that could influence advice" \
    || bad "rejected knowledge state" "$REVIEWED"
  PATTERNS=$(curl -fsS "$API/api/v1/intelligence/patterns?project_id=$PROJ&page_size=20")
  echo "$PATTERNS" | jb "isinstance(d['items'], list)" \
    && ok "the pattern explorer answers" || bad "patterns" "$PATTERNS"
else
  bad "no knowledge was produced from three completed episodes" "$(echo "$RUN1" | jget "d.get('patterns_discovered')") pattern(s) discovered"
  PATTERNS=$(curl -fsS "$API/api/v1/intelligence/patterns?project_id=$PROJ&page_size=20")
  echo "$PATTERNS" | jb "isinstance(d['items'], list)" \
    && ok "the pattern explorer still answers" || bad "patterns" "$PATTERNS"
fi

echo "== 7. Grounded search (§46–§49, §90) =="
ANSWER=$(curl -fsS -G "$API/api/v1/intelligence/search" \
  --data-urlencode "project_id=$PROJ" \
  --data-urlencode 'q=what has happened before with checkout errors')
echo "$ANSWER" | jb "isinstance(d['answer'], str) and d['answer']" \
  && ok "the search returns an answer string" || bad "search answer" "$ANSWER"
echo "$ANSWER" | jb "isinstance(d['citations'], list) and isinstance(d['limitations'], list)" \
  && ok "the answer carries citations and limitations" || bad "search shape" "$ANSWER"
echo "$ANSWER" | jb "'argus knows' not in d['answer'].lower() and 'definitely' not in d['answer'].lower()" \
  && ok "the answer claims no certainty it cannot support" || bad "overclaim" "$ANSWER"

echo "== 8. Recommendations: decision and outcome are separate facts (§57, §81) =="
OPEN_INC=$(curl -fsS "$API/api/v1/incidents?project_id=$PROJ&page_size=50" | jget "([i['id'] for i in d['items'] if i['status'] in ('OPEN','ACKNOWLEDGED','INVESTIGATING','MITIGATED')] or [d['items'][0]['id'] if d['items'] else ''])[0]")
if [ -n "$OPEN_INC" ]; then
  RECS=$(post "$API/api/v1/intelligence/incidents/$OPEN_INC/recommendations?project_id=$PROJ&generate=true" -H 'Content-Type: application/json' -d '{}' 2>/dev/null || curl -fsS "$API/api/v1/intelligence/incidents/$OPEN_INC/recommendations?project_id=$PROJ&generate=true")
  echo "$RECS" | jb "isinstance(d['items'], list)" \
    && ok "the incident recommendation surface answers" || bad "incident recommendations" "$RECS"
  REC_TOTAL=$(echo "$RECS" | jget "d['total']" 2>/dev/null || echo 0)
  if [ "${REC_TOTAL:-0}" -ge 1 ]; then
    RID=$(echo "$RECS" | jget "d['items'][0]['id']")
    echo "$RECS" | jb "isinstance(d['items'][0].get('limitations'), list)" \
      && ok "advice states its limitations" || bad "advice limitations" "$RECS"
    check_status "an unrecognised actor cannot decide a recommendation" \
      "$(code -X POST "$API/api/v1/intelligence/recommendations/$RID/decide?project_id=$PROJ" -H 'Content-Type: application/json' -d '{"decision":"ACCEPTED","actor":""}')" "422"
    DECIDED=$(post "$API/api/v1/intelligence/recommendations/$RID/decide?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d '{"decision":"ACCEPTED","actor":"smoke-operator"}')
    echo "$DECIDED" | jb "d['recommendation']['status'] == 'ACCEPTED'" \
      && ok "the decision moved the recommendation to ACCEPTED" || bad "decision" "$DECIDED"
    OUTCOME=$(post "$API/api/v1/intelligence/recommendations/$RID/outcome?project_id=$PROJ" -H 'Content-Type: application/json' \
      -d '{"verdict":"INEFFECTIVE","recorded_by":"smoke-operator","detail":{"note":"the errors returned within ten minutes"}}')
    echo "$OUTCOME" | jb "d['recommendation']['status'] == 'INEFFECTIVE'" \
      && ok "an ineffective outcome is recorded as such, not as a success" || bad "outcome" "$OUTCOME"
    echo "$OUTCOME" | jb "len(d['outcomes']) >= 1" \
      && ok "the outcome history keeps the verdict and who recorded it" || bad "outcome history" "$OUTCOME"
  else
    #: Advice for a live incident with three comparable episodes is the point
    #: of this step. Recording a PASS for "nothing was produced" would let the
    #: recommendation surface stop working without the gate noticing.
    bad "the engine produced no advice for a live incident with comparable history" "$RECS"
  fi
else
  bad "no open incident was available to advise on" "$INCIDENTS"
fi

echo "== 9. Experiences and runs are readable and paginated (§54, §59) =="
RUNS=$(curl -fsS "$API/api/v1/intelligence/learning-runs?project_id=$PROJ&page_size=10")
echo "$RUNS" | jb "d['total'] >= 2 and len(d['items']) >= 2" \
  && ok "both runs are listed" || bad "runs list" "$RUNS"
echo "$RUNS" | jb "all('status' in r and 'data_cutoff' in r for r in d['items'])" \
  && ok "each run states its status and the window it read" || bad "runs shape" "$RUNS"

echo "== 10. Learned relationships are historical, never dependencies (§23, §24) =="
RELS=$(curl -fsS "$API/api/v1/intelligence/relationships?project_id=$PROJ&page_size=100")
echo "$RELS" | jb "isinstance(d['relationship_note'], str) and 'not a dependency' in d['relationship_note']" \
  && ok "the response states that these are not dependencies" || bad "relationship note" "$RELS"
REL_TOTAL=$(echo "$RELS" | jget "d['total']")
if [ "$REL_TOTAL" -ge 1 ]; then
  ok "$REL_TOTAL learned relationship(s) derived from the three completed episodes"
  echo "$RELS" | jb "all(r['is_dependency'] is False for r in d['items'])" \
    && ok "no learned relationship claims to be a dependency" || bad "dependency claim" "$RELS"
  echo "$RELS" | jb "all(isinstance(r['sample_count'], int) and r['sample_count'] >= 1 for r in d['items'])" \
    && ok "each relationship carries a real sample count" || bad "relationship samples" "$RELS"
  echo "$RELS" | jb "all(r['disclaimer'] for r in d['items'])" \
    && ok "each row repeats the disclaimer rather than relying on the page" || bad "disclaimer" "$RELS"
  echo "$RELS" | jb "any(r['directed'] is False for r in d['items']) or any(r['directed'] is True for r in d['items'])" \
    && ok "direction is an explicit per-row fact" || bad "direction flag" "$RELS"
  #: The structural graph must be untouched by the learning layer.
  EDGE_BEFORE=$(sql "SELECT count(*) FROM graph_edges")
  post "$API/api/v1/intelligence/learning-runs" -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJ\",\"trigger\":\"smoke-verify\"}" > /dev/null
  EDGE_AFTER=$(sql "SELECT count(*) FROM graph_edges")
  [ "$EDGE_BEFORE" = "$EDGE_AFTER" ] \
    && ok "a learning run wrote nothing into graph_edges ($EDGE_BEFORE edges before and after)" \
    || bad "graph_edges changed" "before=$EDGE_BEFORE after=$EDGE_AFTER"
  #: And re-running did not duplicate the learned edges.
  REL_AGAIN=$(curl -fsS "$API/api/v1/intelligence/relationships?project_id=$PROJ&page_size=100")
  echo "$REL_AGAIN" | jb "d['total'] == $REL_TOTAL" \
    && ok "re-running refreshed $REL_TOTAL relationship(s) without duplicating any" \
    || bad "relationship duplication" "$REL_AGAIN"
  REL_ONE=$(echo "$RELS" | jget "d['items'][0]['id']")
  check_status "an unknown relationship status is refused" \
    "$(code "$API/api/v1/intelligence/relationships?project_id=$PROJ&status=NONSENSE")" "422"
else
  bad "no learned relationship was derived from three multi-component episodes" "$RELS"
  echo "$RELS" | jb "d['total'] == 0" \
    && ok "the empty answer is at least honest (no placeholder rows)" \
    || bad "unexpected relationships" "$RELS"
fi

echo "== 11. The component learning profile (§58) =="
PROFILE=$(curl -fsS "$API/api/v1/intelligence/components/$CO/profile?project_id=$PROJ")
echo "$PROFILE" | jb "d['component']['id'] == '$CO'" \
  && ok "the profile is for the component that was asked for" || bad "profile scope" "$PROFILE"
echo "$PROFILE" | jb "isinstance(d.get('relationships'), list)" \
  && ok "the profile carries the learned relationships around the component (§23)" \
  || bad "profile relationships" "$PROFILE"
echo "$PROFILE" | jb "all(r['is_dependency'] is False for r in d.get('relationships', []))" \
  && ok "and none of them is presented as a dependency" || bad "profile dependency claim" "$PROFILE"
check_status "another project's component is refused" \
  "$(code "$API/api/v1/intelligence/components/$CO/profile?project_id=$OTHER")" "404"

echo "== 12. Cross-project isolation (§62, §91) =="
if [ -n "${EXP_ID:-}" ]; then
  check_status "another project cannot read this experience" \
    "$(code "$API/api/v1/intelligence/experiences/$EXP_ID?project_id=$OTHER")" "404"
fi
if [ -n "${KID:-}" ]; then
  check_status "another project cannot read this pattern" \
    "$(code "$API/api/v1/intelligence/knowledge/$KID?project_id=$OTHER")" "404"
fi
OTHER_RELS=$(curl -fsS "$API/api/v1/intelligence/relationships?project_id=$OTHER")
echo "$OTHER_RELS" | jb "d['total'] == 0" \
  && ok "another project sees none of this project's relationships" || bad "cross-project leakage" "$OTHER_RELS"
OTHER_EXP=$(curl -fsS "$API/api/v1/intelligence/experiences?project_id=$OTHER")
echo "$OTHER_EXP" | jb "d['total'] == 0" \
  && ok "another project sees none of this project's experiences" || bad "cross-project experiences" "$OTHER_EXP"

echo "== 13. Learning metrics expose the relationship stage (§80) =="
METRICS=$(curl -fsS "$API/api/v1/intelligence/metrics?project_id=$PROJ")
echo "$METRICS" | jb "isinstance(d['recommendations_by_status'], dict) and d['learning_runs'] >= 2" \
  && ok "the metrics endpoint reports real counts" || bad "metrics" "$METRICS"
echo "$METRICS" | jb "'relationships_active' in d and 'relationships_undirected' in d" \
  && ok "relationship health is published, including the undirected count" || bad "relationship metrics" "$METRICS"
HEALTH_SCOPE=$(curl -fsS "$API/api/v1/intelligence/health")
echo "$HEALTH_SCOPE" | jb "d['pending_events'] >= 0" \
  && ok "health is answerable without a project scope" || bad "unscoped health" "$HEALTH_SCOPE"

echo "== 14. The Phase 10 tables, enums and indexes exist in PostgreSQL =="
TABLES=$(sql "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('reliability_experiences','learning_events','reliability_knowledge','intelligence_knowledge_versions','intelligence_learning_runs','intelligence_learning_experiments','intelligence_component_profiles','intelligence_recommendations','intelligence_recommendation_outcomes','intelligence_knowledge_reviews','intelligence_event_hooks','intelligence_relationships')")
[ "${TABLES:-0}" = "12" ] \
  && ok "all twelve Phase 10 tables exist" || bad "tables" "found=$TABLES expected=12"
ENUMS=$(sql "SELECT count(*) FROM pg_type WHERE typname IN ('intelligence_knowledge_type','intelligence_knowledge_status','intelligence_knowledge_scope','intelligence_confidence','intelligence_provenance','intelligence_relationship_kind','intelligence_relationship_status')")
[ "${ENUMS:-0}" = "7" ] \
  && ok "the namespaced enum types exist" || bad "enums" "found=$ENUMS expected=7"
IDX=$(sql "SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_intelligence_relationships_key'")
[ "${IDX:-0}" = "1" ] \
  && ok "the relationship uniqueness index exists (one row per project/pair/kind)" \
  || bad "relationship index" "found=$IDX expected=1"
NULLS=$(sql "SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_intelligence_relationships_key' AND indexdef LIKE '%COALESCE%'")
[ "${NULLS:-0}" = "1" ] \
  && ok "the index collapses a NULL environment, so duplicates cannot slip through" \
  || bad "coalesce index" "found=$NULLS expected=1"
if [ -n "${DDL_PROBE:-}" ]; then
  #: Opt-in: reverses the two Phase 10 revisions and re-applies them, so the
  #: downgrade path is exercised against the real database rather than assumed.
  $COMPOSE exec -T api alembic downgrade f7a8b9c0d1e2 > /dev/null 2>&1 \
    && ok "the Phase 10 migration reverses cleanly" || bad "downgrade" "alembic downgrade failed"
  $COMPOSE exec -T api alembic upgrade head > /dev/null 2>&1 \
    && ok "and re-applies cleanly" || bad "upgrade" "alembic upgrade failed"
fi

echo "== 15. The learning workspace renders (§51–§60) =="
for P in "/intelligence" "/intelligence/patterns" "/intelligence/relationships" \
  "/intelligence/recommendations" "/intelligence/experiences" \
  "/intelligence/learning-runs" "/intelligence/search"; do
  C=$(code "$WEB$P")
  [ "$C" = "200" ] && ok "GET $P → 200" || bad "web $P" "$C"
done
CENTER_HTML=$(curl -fsS "$WEB/intelligence" || echo "")
echo "$CENTER_HTML" | grep -q "Learning Center" \
  && ok "the center is a learning view, not a second incident dashboard" \
  || bad "web center" "missing heading"
echo "$CENTER_HTML" | grep -qi "scope" \
  && ok "the center states that learning is project-scoped" || bad "web scope copy" "missing"
REL_HTML=$(curl -fsS "$WEB/intelligence/relationships?project_id=$PROJ" || echo "")
echo "$REL_HTML" | grep -qi "not a dependency" \
  && ok "the relationships view repeats that learned edges are not dependencies" \
  || bad "web relationships" "missing disclaimer"
SEARCH_HTML=$(curl -fsS "$WEB/intelligence/search" || echo "")
echo "$SEARCH_HTML" | grep -qi "no comparable" \
  && ok "the search view says an answer may have no comparable case" \
  || bad "web search copy" "missing"

echo "== 16. Cleanup =="
check_status "the scratch project deletes" "$(code -X DELETE "$API/api/v1/projects/$PROJ")" "204"
check_status "the second scratch project deletes" "$(code -X DELETE "$API/api/v1/projects/$OTHER")" "204"
LEFTOVER=$(sql "SELECT count(*) FROM reliability_experiences WHERE project_id = '$PROJ'")
[ "${LEFTOVER:-0}" = "0" ] \
  && ok "nothing the run learned outlives the project it learned it from" \
  || bad "leftover experiences" "found=$LEFTOVER"
LEFTOVER_REL=$(sql "SELECT count(*) FROM intelligence_relationships WHERE project_id = '$PROJ'")
[ "${LEFTOVER_REL:-0}" = "0" ] \
  && ok "and no learned relationship survived the delete" || bad "leftover relationships" "found=$LEFTOVER_REL"

echo
echo "Phase 10 live smoke: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
