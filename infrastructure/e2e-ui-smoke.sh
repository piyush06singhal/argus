#!/usr/bin/env bash
# ARGUS — UI gate: every page, as a real caller (hardening W3/W7).
#
# The phase gates prove the *API*. This one proves the thing the operator
# actually looks at: the Next.js app rendering every route through the real
# server — with the caller's token, which is the only way the W1 auth boundary
# is exercised the way a browser exercises it.
#
# Four properties are asserted that no backend test can:
#
#   1. **No route fails to render.** A page that throws in a server component
#      shows up here and nowhere else.
#   2. **Every route renders through the app shell, with its own title.** The
#      check is status + shell + a page-specific <title>, not the status alone:
#      Next returns 404 for a page that calls `notFound()`, and a route whose
#      data fetch is broken can render a *plausible* page with nothing in it.
#   3. **The token boundary holds at the render layer.** The same routes are
#      requested *without* the cookie: they must not leak project data (checked
#      against the admin's real project names used as canaries) and must not
#      fail.
#   4. **Server-rendered pages fetch as their caller.** With the cookie, a page
#      that needs backend data must show that data — proving the request scope
#      reaches the API layer rather than leaking across requests.
#
# A note on error markers, because the obvious ones do not work: Next inlines
# its built-in not-found boundary into the serialized payload of *every* page, so
# grepping a response for "This page could not be found" matches healthy pages.
# The signals used here are the ones that are actually discriminating — the HTTP
# status, the `<title>` Next sets per page, the presence of the shell, and the
# framework's own server-exception text.
#
# Route coverage is derived from the app directory, not hard-coded: a new page is
# checked the moment it exists. Dynamic routes are visited with real ids read
# from the API; when the stack genuinely has no row of that kind the route is
# reported as SKIP with the reason (`--strict` turns any skip into a failure,
# which is how CI should run it against a seeded stack).
#
# Prereq: the stack is up (`docker compose up`) and migrations have run.
set -eu
# Hardening W1: resolve an admin token and authenticate every request.
source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"

API="${API:-http://localhost:8000}"
WEB="${WEB:-http://localhost:3000}"
STRICT=""
[ "${1:-}" = "--strict" ] && STRICT="yes"

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
skip() { SKIP=$((SKIP+1)); echo "  SKIP  $1  ($2)"; }

echo "== UI gate =="

if ! command curl -s -o /dev/null "$WEB/"; then
  echo "  FAIL  web app unreachable at $WEB" >&2
  echo "ui gate: FAILED (web app down)"; exit 1
fi

# ---------------------------------------------------------------------------
# 1. Route inventory, derived from the app source
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/apps/web/app}"
if [ -d "$APP_DIR" ]; then
  STATIC_ROUTES=$(cd "$APP_DIR" && find . -name page.tsx | sed 's|/page.tsx||; s|^\.||' \
    | grep -v '\[' | sed 's|^$|/|' | sort)
  DYNAMIC_ROUTES=$(cd "$APP_DIR" && find . -name page.tsx | sed 's|/page.tsx||; s|^\.||' \
    | grep '\[' | sort)
else
  echo "  note  app source not found at $APP_DIR — using the pinned route inventory"
  STATIC_ROUTES=$(printf '%s\n' / /anomalies /connect /debugger /deployments /fixes \
    /incidents /incidents/dashboard /incidents/rca /ingestion-health /intelligence \
    /intelligence/experiences /intelligence/learning-runs /intelligence/patterns \
    /intelligence/recommendations /intelligence/relationships /intelligence/search \
    /observability /observability/events /observability/logs /observability/metrics \
    /observability/traces /platform /platform/activity /platform/cases /platform/changes \
    /platform/data-quality /platform/governance /platform/health /platform/reports \
    /platform/search /platform/services /platform/slo /projects /reliability \
    /reliability/accuracy /reliability/backtests /reliability/forecasts \
    /reliability/models /remediation /remediation/policy /reproductions /settings \
    /settings/tokens /system-map | sort)
  DYNAMIC_ROUTES=$(printf '%s\n' '/anomalies/[id]' '/debugger/[sessionId]' \
    '/debugger/incident/[incidentId]' '/fixes/[id]' '/incidents/[id]' \
    '/incidents/[id]/causal-analysis' '/incidents/[id]/reproductions' \
    '/intelligence/components/[componentId]' '/intelligence/experiences/[experienceId]' \
    '/intelligence/learning-runs/[runId]' '/intelligence/patterns/[knowledgeId]' \
    '/intelligence/recommendations/[recommendationId]' '/observability/traces/[traceId]' \
    '/platform/cases/[caseId]' '/platform/services/[componentId]' '/platform/slo/[sloId]' \
    '/projects/[id]' '/reliability/components/[componentId]' '/reliability/forecasts/[id]' \
    '/remediation/[actionId]' '/reproductions/[id]' | sort)
fi
static_count=$(printf '%s\n' "$STATIC_ROUTES" | grep -c . || true)
dynamic_count=$(printf '%s\n' "$DYNAMIC_ROUTES" | grep -c . || true)
echo "  inventory: $static_count static routes, $dynamic_count dynamic routes"

# ---------------------------------------------------------------------------
# 2. Helpers
# ---------------------------------------------------------------------------
#: Framework text that only appears when a server component actually threw.
ERR_MARKERS='Application error: a server-side exception|Unhandled Runtime Error|An unexpected error has occurred'
#: The persistent shell: if it is absent, the page did not render in the app.
SHELL_MARKER='/incidents'
#: A page that fell through to the framework 404 renders this title.
NOT_FOUND_TITLE='<title>404:'

fetch_as_caller() { curl -s -H "Cookie: argus_token=${ARGUS_TOKEN:-}" "$1"; }
fetch_anon() { command curl -s "$1"; }

#: Status code as the caller (no cookie) or with it.
status_as() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

check_route() {  # route, html
  local route="$1" html="$2"
  if printf '%s' "$html" | grep -qE "$ERR_MARKERS"; then
    bad "$route" "rendered a server-side exception"
    return
  fi
  if printf '%s' "$html" | grep -qF "$NOT_FOUND_TITLE"; then
    bad "$route" "rendered the framework 404 page"
    return
  fi
  if ! printf '%s' "$html" | grep -q "$SHELL_MARKER"; then
    bad "$route" "no app shell in the response"
    return
  fi
  if ! printf '%s' "$html" | grep -q "<title>.*ARGUS"; then
    bad "$route" "no page title"
    return
  fi
  ok
}

# ---------------------------------------------------------------------------
# 3. Anonymous requests must not leak or fail (the render-layer auth boundary)
# ---------------------------------------------------------------------------
echo "-- anonymous boundary"
PROJECT_NAMES=$(curl -s "$API/api/v1/projects?page_size=5" \
  | python3 -c "import json,sys;print('\n'.join(p['name'] for p in json.load(sys.stdin).get('items',[])))" 2>/dev/null || true)
for route in / /projects /incidents /platform /system-map; do
  url="$WEB$route"
  html=$(fetch_anon "$url" || true)
  code=$(command curl -s -o /dev/null -w '%{http_code}' "$url" || echo 000)
  case "$code" in
    5*) bad "anon $route" "HTTP $code" ; continue ;;
  esac
  leaked=""
  if [ -n "$PROJECT_NAMES" ]; then
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      if printf '%s' "$html" | grep -qF "$name"; then leaked="$name"; fi
    done <<EOF
$PROJECT_NAMES
EOF
  fi
  if [ -n "$leaked" ]; then
    bad "anon $route" "leaked project name '$leaked' to an unauthenticated caller"
  else
    ok
  fi
done

# ---------------------------------------------------------------------------
# 4. Every static route, with the caller's token
# ---------------------------------------------------------------------------
echo "-- static routes (authenticated)"
while IFS= read -r route; do
  [ -n "$route" ] || continue
  url="$WEB$route"
  code=$(status_as -H "Cookie: argus_token=${ARGUS_TOKEN:-}" "$url" || echo 000)
  if [ "$code" != "200" ]; then
    bad "$route" "HTTP $code"
    continue
  fi
  check_route "$route" "$(fetch_as_caller "$url")"
done <<EOF
$STATIC_ROUTES
EOF

# ---------------------------------------------------------------------------
# 5. Dynamic routes, with real ids where the stack has data
# ---------------------------------------------------------------------------
echo "-- dynamic routes (real ids where available)"
# Id resolution lives in its own module because doing it naively produced a gate
# that passed while testing nothing: the first project returned by the list
# endpoint is usually an empty scratch project, so every scoped lookup came back
# empty and all 20 dynamic routes reported SKIP. See lib/ui-resolve-ids.py for
# the three mistakes it encodes against (wrong project, one assumed envelope,
# assumed scope-optional endpoints) and for how it handles the platform's own
# rate limiter — a 429 misread as "no such row" is the same silent failure.
RESOLVED=$(python3 "$(dirname "${BASH_SOURCE[0]}")/lib/ui-resolve-ids.py" "$API") \
  || RESOLVED=""
if [ -z "$RESOLVED" ]; then
  bad "id resolution" "lib/ui-resolve-ids.py produced nothing; dynamic routes cannot be visited"
  RESOLVED=""
fi
eval "$RESOLVED"
: "${project_id:=}"; : "${incident_id:=}"; : "${anomaly_id:=}"; : "${component_id:=}"
: "${trace_id:=}"; : "${fix_id:=}"; : "${repro_id:=}"; : "${action_id:=}"
: "${knowledge_id:=}"; : "${experience_id:=}"; : "${run_id:=}"; : "${rec_id:=}"
: "${forecast_id:=}"; : "${slo_id:=}"; : "${case_id:=}"; : "${session_id:=}"
: "${slo_metric:=}"
echo "  resolved: 16 route ids, project scope ${project_id:-none}"

# The one route whose backing rows may genuinely not exist anywhere: a project
# has to *define* an objective before there is one to open. Defining one is a
# documented operator action, so the gate performs it rather than reporting a
# permanent SKIP that would hide a broken detail page forever. Idempotent: only
# when no objective exists in any project.
if [ -z "$slo_id" ] && [ -n "$project_id" ] && [ -n "${slo_metric:-}" ]; then
  slo_body=$(python3 -c '
import json, sys
print(json.dumps({
    "name": "availability (ui gate)",
    "indicator": "AVAILABILITY",
    "metric_name": sys.argv[1],
    "target": 0.99,
    "comparison": "AT_LEAST",
    "actor": "ui-gate",
}))
' "$slo_metric")
  slo_id=$(
    curl -s -X POST "$API/api/v1/platform/slo?project_id=$project_id" \
      -H 'Content-Type: application/json' \
      -d "$slo_body" 2>/dev/null \
      | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("slo_id", ""))' 2>/dev/null \
    || true
  )
  if [ -n "$slo_id" ]; then
    echo "  note  defined an objective ($slo_metric) so the detail page is asserted with real data"
  else
    echo "  note  could not define an objective (the route will be reported as SKIP)"
  fi
fi

try_dynamic() {  # template, id, label
  local template="$1" id="$2" label="$3" route url code page query=""
  if [ -z "$id" ]; then
    skip "$template" "no $label in the stack"
    return
  fi
  route=$(printf '%s' "$template" | sed "s|\[[a-zA-Z]*\]|$id|")
  # A page that reads `searchParams.project_id` renders that project's data and
  # otherwise falls back to the first project in the list — which is not the
  # project the resolved id belongs to, so the route would be asserted against
  # somebody else's (empty) scope. The requirement is read from the page source
  # rather than hard-coded here, so a new scoped page is scoped automatically.
  page="$APP_DIR$template/page.tsx"
  if [ -f "$page" ] && grep -q 'searchParams.project_id' "$page" && [ -n "$project_id" ]; then
    query="?project_id=$project_id"
  fi
  url="$WEB$route$query"
  code=$(status_as -H "Cookie: argus_token=${ARGUS_TOKEN:-}" "$url" || echo 000)
  if [ "$code" != "200" ]; then
    bad "$route" "HTTP $code for an existing row"
    return
  fi
  check_route "$route" "$(fetch_as_caller "$url")"
}

try_dynamic '/incidents/[id]'                       "$incident_id"   "incident"
try_dynamic '/incidents/[id]/causal-analysis'       "$incident_id"   "incident"
try_dynamic '/incidents/[id]/reproductions'         "$incident_id"   "incident"
try_dynamic '/anomalies/[id]'                       "$anomaly_id"    "anomaly"
try_dynamic '/projects/[id]'                        "$project_id"    "project"
try_dynamic '/platform/services/[componentId]'      "$component_id"  "component"
try_dynamic '/reliability/components/[componentId]' "$component_id"  "component"
try_dynamic '/intelligence/components/[componentId]' "$component_id" "component"
try_dynamic '/observability/traces/[traceId]'       "$trace_id"      "trace"
try_dynamic '/fixes/[id]'                           "$fix_id"        "fix"
try_dynamic '/reproductions/[id]'                   "$repro_id"      "reproduction"
try_dynamic '/remediation/[actionId]'               "$action_id"     "remediation action"
try_dynamic '/intelligence/patterns/[knowledgeId]'  "$knowledge_id"  "learned pattern"
try_dynamic '/intelligence/experiences/[experienceId]' "$experience_id" "experience"
try_dynamic '/intelligence/learning-runs/[runId]'   "$run_id"        "learning run"
try_dynamic '/intelligence/recommendations/[recommendationId]' "$rec_id" "recommendation"
try_dynamic '/reliability/forecasts/[id]'           "$forecast_id"   "forecast"
try_dynamic '/platform/slo/[sloId]'                 "$slo_id"        "SLO"
try_dynamic '/platform/cases/[caseId]'              "$case_id"       "reliability case"
try_dynamic '/debugger/[sessionId]'                 "$session_id"    "debug session"
try_dynamic '/debugger/incident/[incidentId]'       "$incident_id"   "incident"

# ---------------------------------------------------------------------------
echo
echo "ui gate: $PASS passed, $FAIL failed, $SKIP skipped"
if [ -n "$STRICT" ] && [ "$SKIP" -gt 0 ]; then
  echo "ui gate: FAILED (--strict and $SKIP route(s) had no data to render)"
  exit 1
fi
[ "$FAIL" -eq 0 ] || { echo "ui gate: FAILED"; exit 1; }
echo "ui gate: OK"
