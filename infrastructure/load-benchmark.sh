#!/usr/bin/env bash
# ARGUS Phase 1 — ingestion load benchmark (spec §83.15, DoD #45)
#
# Measures the async ingestion path end to end:
#   1. Creates a throwaway project.
#   2. POSTs `BATCHES` batches (default 10) of `BATCH_SIZE` events (default 50
#      → 500 raw events) to the Redis queue as fast as curl can fire them.
#   3. Reports enqueue wall-time + ingest rate.
#   4. Polls the events endpoint until the worker drains them (or timeout),
#      then reports drain latency and verifies the count arrived.
#
# Results are deliberately conservative: a single uvicorn worker drains one
# batch per poll cycle. Run with API/api defaults against the compose stack.
set -eu
API="${API:-http://localhost:8000}"
BATCHES="${BATCHES:-10}"
BATCH_SIZE="${BATCH_SIZE:-50}"
TIMEOUT_S="${TIMEOUT_S:-60}"
TOTAL=$((BATCHES * BATCH_SIZE))

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }

TS=$(date +%s)
SLUG="bench-$TS"
PROJ=$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Benchmark $TS\",\"slug\":\"$SLUG\"}")
PID=$(echo "$PROJ" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "benchmark project: $PID   events=$TOTAL (${BATCHES}x${BATCH_SIZE})"

echo "== enqueueing to Redis queue =="
START=$(date +%s.%N)
for b in $(seq 1 "$BATCHES"); do
  BODY=$(python3 -c "
import json, sys, uuid
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()
events = [{
  'source_type': 'mock', 'source_name': 'bench', 'timestamp': now,
  'event_type': 'SYSTEM_EVENT', 'payload': {'batch': $b, 'i': i, 'message': 'bench'},
} for i in range($BATCH_SIZE)]
print(json.dumps({'project_id': '$PID', 'events': events}))
")
  RESP=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/ingestion/queue" \
    -H 'Content-Type: application/json' -d "$BODY")
  if [ "$RESP" != "202" ]; then
    echo "  batch $b got HTTP $RESP (expected 202)"
    bad "enqueue batch $b" "202"; exit 1
  fi
done
END=$(date +%s.%N)
ENQ_SEC=$(python3 -c "print(f'{$END - $START:.2f}')")
ENQ_RATE=$(python3 -c "print(f'{$TOTAL / ($END - $START):.0f}')")
ok "enqueued $TOTAL events in ${ENQ_SEC}s (~${ENQ_RATE}/s)"

echo "== waiting for worker drain =="
SEEN=0
for i in $(seq 1 "$TIMEOUT_S"); do
  SEEN=$(curl -fsS "$API/api/v1/observability/events?project_id=$PID&page_size=1" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))")
  if [ "$SEEN" -ge "$TOTAL" ]; then
    break
  fi
  sleep 1
done
DRAIN_SEC=$((i))
ok "worker drained ${SEEN}/${TOTAL} in ~${DRAIN_SEC}s"
[ "$SEEN" -ge "$TOTAL" ] && ok "drain complete" || bad "drain complete" "expected >= $TOTAL got $SEEN"

echo "== sync bulk path comparison (50 events) =="
BODY=$(python3 -c "
import json
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()
events = [{
  'source_type': 'mock', 'source_name': 'bench-sync', 'timestamp': now,
  'event_type': 'SYSTEM_EVENT', 'payload': {'i': i, 'message': 'sync'},
} for i in range(50)]
print(json.dumps({'project_id': '$PID', 'events': events}))
")
START=$(date +%s.%N)
RESP=$(curl -fsS -X POST "$API/api/v1/ingestion/bulk" -H 'Content-Type: application/json' -d "$BODY")
END=$(date +%s.%N)
SYNC_SEC=$(python3 -c "print(f'{$END - $START:.3f}')")
ACCT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('accepted',0))")
ok "bulk sync accepted=${ACCT} in ${SYNC_SEC}s"

echo ""
echo "=============================="
echo "LOAD BENCHMARK RESULT: $PASS passed, $FAIL failed"
echo "=============================="
[ "$FAIL" = 0 ]