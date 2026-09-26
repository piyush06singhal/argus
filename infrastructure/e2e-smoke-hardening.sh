#!/usr/bin/env bash
# ARGUS — live authorization & tenant-isolation gate.
#
# Why this gate exists, and why it is not redundant with
# `apps/api/tests/test_auth_security.py`:
#
#   The unit suite talks to the app in-process. `BaseHTTPMiddleware` happens to
#   run the endpoint in the caller's context there, so a ContextVar set in the
#   middleware *is* visible and every test passed — while the live uvicorn
#   process resolved each request to a stale context and skipped every
#   authorization check. A scoped token could read and list every project, and
#   an ingest token could write into any project's telemetry.
#
#   That class of defect is only observable over a real HTTP server, so it is
#   asserted here, against the running stack, and nowhere else.
#
# Deliberate detail: this gate does NOT source `lib/gate-auth.sh`. That helper
# installs a `curl` shim which stamps the ADMIN credential onto every call, and
# a shim would mask the very boundary under test (a scoped request would still
# travel with an admin header). Credentials are therefore attached explicitly,
# one call at a time.
#
# Usage:
#   bash infrastructure/e2e-smoke-hardening.sh
#   API=http://host:8000 ARGUS_TOKEN=... bash infrastructure/e2e-smoke-hardening.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${API:-http://localhost:8000}"

PASS=0
FAIL=0
SKIP=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
    printf '  ok   %-56s %s\n' "$label" "$actual"
  else
    FAIL=$((FAIL + 1))
    printf '  FAIL %-56s expected=%s got=%s\n' "$label" "$expected" "$actual"
  fi
}

# A JSON field reader that never breaks the gate on an unexpected body shape.
jfield() {
  python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print('')
    sys.exit(0)
for key in sys.argv[1].split('.'):
    if isinstance(data, list):
        data = data[int(key)] if data else None
    elif isinstance(data, dict):
        data = data.get(key)
    else:
        data = None
    if data is None:
        break
print('' if data is None else data)
" "$1"
}

# --- credential-scoped request helpers -------------------------------------
# Each helper owns exactly one credential. No ambient header, ever.
status_admin() {  # status_admin METHOD PATH [curl args]
  local method="$1" path="$2"
  shift 2
  command curl -s -o /dev/null -w '%{http_code}' -X "$method" "$API$path" \
    -H "Authorization: Bearer $ARGUS_TOKEN" "$@"
}

status_scoped() {
  local method="$1" path="$2" token="$3"
  shift 3
  command curl -s -o /dev/null -w '%{http_code}' -X "$method" "$API$path" \
    -H "Authorization: Bearer $token" "$@"
}

status_ingest() {
  local method="$1" path="$2" token="$3"
  shift 3
  command curl -s -o /dev/null -w '%{http_code}' -X "$method" "$API$path" \
    -H "X-Argus-Ingest-Token: $token" "$@"
}

body_admin() {
  local path="$1"
  command curl -s "$API$path" -H "Authorization: Bearer $ARGUS_TOKEN"
}

echo "== ARGUS hardening gate: authorization & tenant isolation =="
echo "   api: $API"

#: Every DB-level assertion is scoped to rows created *during this run*, so the
#: gate answers "is isolation broken now?" and not "was it ever broken?" — a
#: residue check that fails forever after one leak teaches nobody anything.
GATE_STARTED_AT="$(date -u '+%Y-%m-%d %H:%M:%S')"

if [ -z "${ARGUS_TOKEN:-}" ] && [ -f "${TMPDIR:-/tmp}/argus-gate-token" ]; then
  ARGUS_TOKEN="$(cat "${TMPDIR:-/tmp}/argus-gate-token")"
fi
if [ -z "${ARGUS_TOKEN:-}" ]; then
  echo "  FAIL no ARGUS_TOKEN: set it, or run the onboarding gate first" >&2
  exit 1
fi

echo
echo "-- 0. authentication is enforced"
check "health/live is public" 200 "$(command curl -s -o /dev/null -w '%{http_code}' "$API/health/live")"
check "projects without a token is 401" 401 \
  "$(command curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/projects")"
check "forged token is 401" 401 \
  "$(command curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/projects" \
    -H 'Authorization: Bearer argus_forged')"
check "ingest token cannot open the general API" 401 \
  "$(command curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/projects" \
    -H 'Authorization: Bearer argus_ing_whatever')"
check "an admin token does reach the API" 200 "$(status_admin GET /api/v1/projects)"

echo
echo "-- 1. two real projects: A is the grant, B is foreign"
PROJECTS="$(body_admin '/api/v1/projects?page_size=2')"
P_A="$(printf '%s' "$PROJECTS" | jfield 'items.0.id')"
P_B="$(printf '%s' "$PROJECTS" | jfield 'items.1.id')"
if [ -z "$P_A" ] || [ -z "$P_B" ]; then
  echo "  SKIP fewer than two projects exist — run the onboarding gate first"
  exit 0
fi

echo
echo "-- 2. a scoped OPERATOR token (grant: A only)"
MINT=$(command curl -s -X POST "$API/api/v1/auth/tokens" \
  -H "Authorization: Bearer $ARGUS_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"hardening-gate-$RANDOM\",\"role\":\"OPERATOR\",\"project_ids\":[\"$P_A\"]}")
SCOPED="$(printf '%s' "$MINT" | jfield 'token')"
[ -n "$SCOPED" ] || SCOPED="$(printf '%s' "$MINT" | jfield 'raw_token')"
if [ -z "$SCOPED" ]; then
  echo "  FAIL could not mint a scoped token: $(printf '%s' "$MINT" | head -c 200)"
  exit 1
fi

check "scoped token reads its own project" 200 \
  "$(status_scoped GET "/api/v1/projects/$P_A" "$SCOPED")"
# The load-bearing assertion: this returned 200 while the live server was
# resolving every request to a stale ADMIN context.
check "scoped token CANNOT read a foreign project" 404 \
  "$(status_scoped GET "/api/v1/projects/$P_B" "$SCOPED")"

LISTED_N=$(command curl -s "$API/api/v1/projects?page_size=100" \
  -H "Authorization: Bearer $SCOPED" | python3 -c "
import json, sys
try:
    print(len(json.load(sys.stdin).get('items', [])))
except Exception:
    print(-1)
")
check "scoped token's project list holds exactly its grant" 1 "$LISTED_N"

# Bodies are built into variables, never inlined inside `$( ... )` within a
# double-quoted string: there the shell re-consumes the `\"` escapes and splits
# the JSON into fragments, which reads as a 422 and looks like a failed
# security assertion while actually being a broken gate.
ENV_BODY='{"name":"must-not-exist","environment_type":"STAGING"}'
check "scoped token CANNOT write a foreign project" 404 \
  "$(status_scoped POST "/api/v1/projects/$P_B/environments" "$SCOPED" \
    -H 'Content-Type: application/json' -d "$ENV_BODY")"

echo
echo "-- 3. OTLP ingestion scope (an ingest token names exactly one project)"
SOURCE=$(command curl -s -X POST "$API/api/v1/ingestion/sources" \
  -H "Authorization: Bearer $ARGUS_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$P_A\",\"name\":\"hardening-gate-$RANDOM\",\"source_type\":\"OTEL\"}")
S_ID="$(printf '%s' "$SOURCE" | jfield 'id')"
if [ -z "$S_ID" ]; then
  echo "  FAIL could not register an ingestion source: $(printf '%s' "$SOURCE" | head -c 200)"
  exit 1
fi
ING=$(command curl -s -X POST "$API/api/v1/ingestion/sources/$S_ID/rotate-token" \
  -H "Authorization: Bearer $ARGUS_TOKEN" | jfield 'ingest_token')
if [ -z "$ING" ]; then
  echo "  FAIL could not mint an ingest token"
  exit 1
fi

# One JSON body per verb, built once. Each drill function takes the project the
# body should *name*, so the only thing that changes between the allowed and the
# refused call is the project — which is the point of the test.
SPAN_TEMPLATE='{"projectId":"%s","resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"isolation-gate"}}]},"scopeSpans":[{"spans":[{"traceId":"0af7651916cd43dd8448eb211c80319c","spanId":"b7ad6b7169203331","name":"GET /isolation-gate","startTimeUnixNano":"1758600000000000000","endTimeUnixNano":"1758600000100000000"}]}]}]}'
LOG_TEMPLATE='{"projectId":"%s","resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"isolation-gate"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"1758600000000000000","severityNumber":17,"body":{"stringValue":"isolation-gate"}}]}]}]}'
METRIC_TEMPLATE='{"projectId":"%s","resourceMetrics":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"isolation-gate"}}]},"scopeMetrics":[{"metrics":[{"name":"isolation_gate","gauge":{"dataPoints":[{"timeUnixNano":"1758600000000000000","asDouble":1}]}}]}]}]}'

drill() {  # drill <verb-path> <projectId>
  local path="$1" project="$2" template="$3" body
  body="$(printf "$template" "$project")"
  status_ingest POST "$path" "$ING" -H 'Content-Type: application/json' -d "$body"
}

# The shape a stock OpenTelemetry exporter actually sends: no top-level
# projectId (it cannot add one), so the credential must supply the project.
NO_PROJECT_SPANS='{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"isolation-gate"}}]},"scopeSpans":[{"spans":[{"traceId":"0af7651916cd43dd8448eb211c80319d","spanId":"b7ad6b7169203332","name":"GET /isolation-gate","startTimeUnixNano":"1758600000000000000","endTimeUnixNano":"1758600000100000000"}]}]}]}'
check "a stock collector body (no projectId) is accepted" 200 \
  "$(status_ingest POST /api/v1/otlp/v1/traces "$ING" \
    -H 'Content-Type: application/json' -d "$NO_PROJECT_SPANS")"

check "ingest token writes its own project" 200 \
  "$(drill /api/v1/otlp/v1/traces "$P_A" "$SPAN_TEMPLATE")"
check "ingest token CANNOT write a foreign project" 403 \
  "$(drill /api/v1/otlp/v1/traces "$P_B" "$SPAN_TEMPLATE")"
check "OTLP logs apply the same scope" 403 \
  "$(drill /api/v1/otlp/v1/logs "$P_B" "$LOG_TEMPLATE")"
check "OTLP metrics apply the same scope" 403 \
  "$(drill /api/v1/otlp/v1/metrics "$P_B" "$METRIC_TEMPLATE")"

echo
echo "-- 4. nothing landed in the foreign project"
FOREIGN=$(docker compose -f "$ROOT/docker-compose.yml" exec -T postgres \
  psql -U argus -d argus_db -t -A -c \
  "SELECT count(*) FROM observability_events WHERE project_id = '$P_B' AND payload::text LIKE '%isolation-gate%' AND created_at >= '$GATE_STARTED_AT'::timestamptz;" \
  2>&1 | tr -d '[:space:]')
case "$FOREIGN" in
  '' | ERROR* | *[!0-9]*)
    SKIP=$((SKIP + 1))
    echo "  (postgres not reachable from here — DB-level assertion skipped)"
    ;;
  *)
    check "no isolation-gate telemetry in the foreign project" 0 "$FOREIGN"
    ;;
esac

echo
echo "== hardening gate: $PASS passed, $FAIL failed, $SKIP skipped =="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
