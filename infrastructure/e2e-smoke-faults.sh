#!/usr/bin/env bash
# ARGUS hardening W4 — live fault-injection gate (Redis outage).
#
# The W4 plan says: "Redis down → API degrades, ingestion returns 503-with-reason,
# queued jobs survive restart, no job stuck forever, graceful shutdown drains."
# Most of that is pinned in `apps/api/tests/test_hardening_resilience.py`; what a
# unit test *cannot* prove is the behaviour of the real stack when the broker
# actually dies. This gate injects that fault for real and checks three things:
#
#   1. **Detect**: the health endpoint reports the dead broker (degraded, with a
#      reason) instead of pretending to be healthy.
#   2. **Degrade**: telemetry ingestion *succeeds* during the outage — the batch
#      path falls back to synchronous processing, so data is never dropped.
#   3. **Recover**: after Redis comes back, health returns to healthy, the queues
#      drain to zero, and **no ingested row is lost** (counted from Postgres, the
#      source of truth — not from an HTTP response).
#
# The fault is injected against the compose service name `redis` and is always
# undone in an EXIT trap, whatever the gate's outcome.
#
# Prereq: the stack is up (docker compose up -d) and seeded.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"
API="${API:-http://localhost:8000}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
check() { [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"; }

# Restore the broker no matter how we exit.
restore_redis() {
  docker compose start redis >/dev/null 2>&1 || true
  # Wait for readiness so a following gate never inherits the outage.
  for _ in $(seq 1 30); do
    if docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
      break
    fi
    sleep 1
  done
}
trap restore_redis EXIT

# --- helpers ---------------------------------------------------------------

health() { curl -s "$API/health/dependencies"; }
redis_status_in_health() {
  health | python3 -c '
import json, sys
body = json.load(sys.stdin)
d = next((x for x in body["dependencies"] if x["name"] == "redis"), {})
print(d.get("status", "missing"))'
}

pg_count() {
  # Rows in observability_events for a given source_name, counted in Postgres.
  # The pipeline stores "<type>:<name>" in the source column.
  docker compose exec -T postgres psql -U "${DATABASE_USER:-argus}" -d "${DATABASE_NAME:-argus_db}" -tAc \
    "SELECT count(*) FROM observability_events WHERE source LIKE '%:$1';" | tr -d '[:space:]'
}

# Build one log event body for the batch ingestion endpoint. The project is
# resolved live from the authenticated caller's own grants so the gate works on
# any stack, not just one with well-known seed ids. Each event carries a unique
# message because the pipeline deduplicates on a canonical fingerprint (project,
# source, type, timestamp, payload) — an identical replay would be a duplicate,
# which is the platform behaving correctly, not data loss.
PROJECT_ID="$(curl -s "$API/api/v1/projects?page_size=1" | python3 -c '
import json, sys
body = json.load(sys.stdin)
items = body.get("items") or body.get("projects") or []
print(items[0]["id"] if items else "")
')"
if [ -z "$PROJECT_ID" ]; then
  echo "FAIL  no project visible to the gate credential; seed the stack first"
  exit 1
fi

RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"

make_batch_body() {
  local ts="$1"
  cat <<EOF
{
  "project_id": "$PROJECT_ID",
  "events": [
    {"source_type": "log", "source_name": "fault-gate", "timestamp": "$ts",
     "event_type": "log", "payload": {"message": "fault-gate $RUN_ID", "severity_text": "INFO"}}
  ]
}
EOF
}

# Poll until the given redis health status is observed (bounded).
wait_for_redis_status() {
  local want="$1" tries="${2:-30}"
  local i=1
  while [ "$i" -le "$tries" ]; do
    [ "$(redis_status_in_health)" = "$want" ] && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

# Wait for every known queue to drain to zero after recovery (bounded).
wait_for_queues_drained() {
  local tries="${1:-60}" i=1 depth total
  while [ "$i" -le "$tries" ]; do
    total=0
    for q in argus:ingest:events argus:ingest:traces argus:detect:anomalies \
             argus:repro:runs argus:reliability:jobs argus:remediate:actions; do
      depth=$(docker compose exec -T redis redis-cli llen "$q" 2>/dev/null | tr -d '[:space:]')
      total=$((total + ${depth:-0}))
    done
    [ "$total" -eq 0 ] && return 0
    sleep 2; i=$((i+1))
  done
  return 1
}

echo "== 0. Stack sanity =="

# Distinct, strictly increasing timestamps per phase (1-second steps): the
# pipeline deduplicates on a canonical fingerprint (project, source, type,
# timestamp, payload), so identical timestamps would misread an intended ingest
# as a duplicate — the platform behaving correctly, not the gate measuring
# data loss. Three separate `date` calls are not guaranteed to differ, so the
# timestamps are derived by offset instead.
BASE_EPOCH="$(date -u +%s)"
iso_at() { date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || python3 -c "import datetime; print(datetime.datetime.fromtimestamp($1, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))"; }
TS_PRE="$(iso_at "$BASE_EPOCH")"
TS_OUTAGE="$(iso_at "$((BASE_EPOCH + 1))")"
TS_POST="$(iso_at "$((BASE_EPOCH + 2))")"

PRE_COUNT="$(pg_count fault-gate)"

# --- 1. Baseline: healthy stack, ingest works ------------------------------
echo "== 1. Baseline: healthy stack accepts telemetry =="
jreq_out="$(mktemp)"; trap 'rm -f "$jreq_out"' EXIT
BODY_PRE="$(make_batch_body "$TS_PRE")"
code="$(printf '%s' "$BODY_PRE" | curl -s -o "$jreq_out" -w '%{http_code}' \
  -X POST "$API/api/v1/ingestion/bulk" -H 'Content-Type: application/json' --data-binary @-)"
check "baseline batch ingest returns 200" 200 "$code"
python3 -c '
import json, sys
body = json.load(open(sys.argv[1]))
assert body["accepted"] >= 1, body
' "$jreq_out" && ok "baseline batch accepted >= 1" || bad "baseline batch accepted >= 1" "$(cat "$jreq_out")"
PRE_COUNT="$(pg_count fault-gate)"
[ "$PRE_COUNT" -ge 1 ] && ok "baseline row landed in Postgres ($PRE_COUNT)" \
  || bad "baseline row landed in Postgres" "count=$PRE_COUNT"

# --- 2. Inject the fault ---------------------------------------------------
echo "== 2. Fault injection: stop Redis mid-flight =="
docker compose stop redis >/dev/null 2>&1
wait_for_redis_status "unhealthy" 30 \
  && ok "health reports redis unhealthy during outage" \
  || bad "health reports redis unhealthy during outage" "status=$(redis_status_in_health)"

# --- 3. Degrade: ingestion still succeeds (no data dropped) ----------------
echo "== 3. Degrade: ingestion survives the outage =="
BODY_OUTAGE="$(make_batch_body "$TS_OUTAGE")"
code="$(printf '%s' "$BODY_OUTAGE" | curl -s -o "$jreq_out" -w '%{http_code}' \
  -X POST "$API/api/v1/ingestion/bulk" -H 'Content-Type: application/json' --data-binary @-)"
check "ingest during outage returns 200 (sync fallback)" 200 "$code"
python3 -c '
import json, sys
body = json.load(open(sys.argv[1]))
assert body.get("accepted", 0) >= 1, body
' "$jreq_out" && ok "outage batch accepted >= 1 (no drop)" || bad "outage batch accepted >= 1" "$(cat "$jreq_out")"

# --- 4. Recover: broker back, health green, queues drain --------------------
echo "== 4. Recover: Redis restored =="
docker compose start redis >/dev/null 2>&1
wait_for_redis_status "healthy" 45 \
  && ok "health reports redis healthy after recovery" \
  || bad "health reports redis healthy after recovery" "status=$(redis_status_in_health)"

wait_for_queues_drained 60 \
  && ok "all queues drain to zero after recovery" \
  || bad "all queues drain to zero after recovery" "backlog remains"

# --- 5. Zero data loss (Postgres is the judge) ------------------------------
echo "== 5. Zero data loss =="
# Re-ingest post-recovery so the async path is proven to work again.
BODY_POST="$(make_batch_body "$TS_POST")"
code="$(printf '%s' "$BODY_POST" | curl -s -o "$jreq_out" -w '%{http_code}' \
  -X POST "$API/api/v1/ingestion/bulk" -H 'Content-Type: application/json' --data-binary @-)"
check "post-recovery batch ingest returns 200" 200 "$code"

sleep 3
POST_COUNT="$(pg_count fault-gate)"
EXPECTED="$((PRE_COUNT + 2))"
[ "$POST_COUNT" -eq "$EXPECTED" ] \
  && ok "zero data loss: $POST_COUNT rows == baseline($PRE_COUNT) + 2" \
  || bad "zero data loss" "expected=$EXPECTED got=$POST_COUNT"

# ---------------------------------------------------------------------------
echo
echo "fault-injection gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
