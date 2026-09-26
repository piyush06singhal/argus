#!/usr/bin/env bash
# ARGUS hardening W2 — live single-sign-on (OIDC) gate.
#
# The unit suite drives the real OIDC client against an in-process provider over
# `httpx.MockTransport`. That proves the client's logic and nothing about the
# assembled system: no socket is opened, no browser redirect is followed, and the
# running API's middleware never sees the session that was minted. A regression in
# any of those — a config value that no longer reaches the process, a redirect URI
# that stopped matching, a token the edge refuses — would pass every unit test.
#
# So this gate runs two real processes over TCP:
#
#   * `infrastructure/lib/oidc-stub.py` — a strict OpenID Connect provider that
#     publishes discovery + JWKS, refuses a non-S256 challenge, refuses a
#     redirect_uri that is not the registered callback, requires client
#     authentication, spends each authorization code exactly once, and verifies
#     the PKCE verifier against the challenge it recorded;
#   * a second ARGUS API process with `OIDC_ENABLED=true`, pointed at that
#     provider and at the same database and Redis as the running stack.
#
# The API runs as a host process on its own port rather than as a container,
# because that is what makes the gate independent of the stack's deployment
# environment: the main `docker compose` api keeps serving on :8000 with SSO
# off, and this gate never has to mutate a running container's configuration
# and put it back. Both processes are torn down in an EXIT trap.
#
# What it asserts, in order:
#
#   1. the public config probe is anonymous, truthful, and leaks no secret
#   2. `/login` redirects to the provider with PKCE S256, a state and a nonce —
#      and the verifier never appears in the URL
#   3. the provider returns the code to the *registered* callback
#   4. the callback mints a session whose role came from the claims
#   5. **that session works**: the edge admits it to an ADMIN-only route, and
#      refuses the same route without it and with a non-admin session
#   6. the provider saw a valid PKCE exchange and Basic client auth, and had to
#      refuse nothing
#   7. replaying a spent state is refused, and a forged state is refused, each
#      with its stable error code
#   8. an unverified email is refused before any credential exists
#   9. a claim naming no known group gets the floor role, never ADMIN
#  10. disabling an identity revokes every session it holds — including one the
#      admin did not present — and blocks the next login
#  11. re-enabling restores sign-in but does *not* resurrect the dead session
#  12. a revocation at the provider takes effect at the next login, and signing
#      in again revokes the previous session (single-session mode)
#  13. the counters the alerts hang off moved, and `argus_oidc_enabled` is 1
#
# Prereqs: the stack is up (`docker compose up -d`) so Postgres and Redis are
# reachable from the host, and the database is at the migration head (the
# compose api applies migrations on boot).
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="${SSO_API_PORT:-8098}"
IDP_PORT="${SSO_IDP_PORT:-8099}"
BASE="http://127.0.0.1:$API_PORT"
IDPBASE="http://127.0.0.1:$IDP_PORT"

ISSUER="$IDPBASE"
CLIENT_ID="${SSO_CLIENT_ID:-argus-console}"
CLIENT_SECRET="${SSO_CLIENT_SECRET:-sso-gate-shared-secret-0123456789abcdef}"
REDIRECT_URI="${SSO_REDIRECT_URI:-http://localhost:3000/auth/callback}"
PROVIDER_NAME="SSO Gate IdP"

PG_USER="${DATABASE_USER:-argus}"
PG_DB="${DATABASE_NAME:-argus_db}"
PG_PORT="${DATABASE_PORT:-5433}"
DATABASE_URL="${SSO_DATABASE_URL:-postgresql+asyncpg://${PG_USER}:${DATABASE_PASSWORD:-argus_password}@localhost:${PG_PORT}/${PG_DB}}"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

ADMIN_SUBJECT="sso-gate-admin"
VIEWER_SUBJECT="sso-gate-viewer"

PASS=0; FAIL=0
ok()    { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()   { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
check() { [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"; }
contains() { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2' in: ${3:0:160}" ;; esac; }

# The interpreter that has the API's dependencies. The repository's own venv is
# the local path; CI installs the same requirements globally.
if [ -x "$ROOT/apps/api/.venv/bin/python" ]; then
  PY="$ROOT/apps/api/.venv/bin/python"
else
  PY="python3"
fi

WORK="$(mktemp -d)"
IDP_PID=""
API_PID=""

# ---------------------------------------------------------------------------
# Teardown — processes first, then the rows this gate created.
# ---------------------------------------------------------------------------
psql_admin() {  # psql_admin <sql> [database]
  docker compose exec -T postgres psql -U "$PG_USER" -d "${2:-$PG_DB}" -q -c "$1" >/dev/null 2>&1
}

psql_scalar() {  # psql_scalar <sql> — one value, whitespace stripped
  docker compose exec -T postgres psql -U "$PG_USER" -d "${2:-$PG_DB}" -tAc "$1" 2>/dev/null \
    | tr -d '[:space:]'
}

purge_gate_rows() {
  # Every row this gate can create hangs off the issuer string, so one predicate
  # removes all of it. Run at startup *and* at exit: a previous run killed
  # between "disable" and "re-enable" would otherwise make the next run fail on
  # an identity that is still disabled.
  local issuer_sql="${ISSUER//\'/\'\'}"
  psql_admin "delete from authentication_audit where token_id in (select id from api_tokens where created_by = 'oidc:$issuer_sql')" || true
  psql_admin "delete from api_token_projects where token_id in (select id from api_tokens where created_by = 'oidc:$issuer_sql')" || true
  psql_admin "delete from api_tokens where created_by = 'oidc:$issuer_sql'" || true
  psql_admin "delete from external_identities where provider = '$issuer_sql'" || true
  psql_admin "delete from oidc_login_states where provider = '$issuer_sql'" || true
}

cleanup() {
  local pid
  for pid in "$API_PID" "$IDP_PID"; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
  done
  for pid in "$API_PID" "$IDP_PID"; do
    [ -n "$pid" ] || continue
    wait "$pid" 2>/dev/null || true
  done
  purge_gate_rows
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "== ARGUS SSO gate: a sign-in that was actually driven end to end =="

# ---------------------------------------------------------------------------
# 0. Prerequisites
# ---------------------------------------------------------------------------
echo
echo "-- 0. prerequisites"

if ! "$PY" - <<'PY' >/dev/null 2>&1
import uvicorn, fastapi, jwt, sqlalchemy, cryptography  # noqa: F401
PY
then
  echo "error: '$PY' cannot import the API's dependencies." >&2
  echo "       run: pip install -r apps/api/requirements-dev.txt" >&2
  exit 2
fi

port_free() {  # port_free <port> <what>
  if "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1])))!=0 else 1)" "$1"; then
    return 0
  fi
  echo "error: port $1 is already in use; this gate needs it for $2." >&2
  echo "       set SSO_API_PORT / SSO_IDP_PORT to free ports and retry." >&2
  exit 2
}
port_free "$IDP_PORT" "the stub identity provider"
port_free "$API_PORT" "the SSO-enabled API process"

if ! docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc 'select 1' >/dev/null 2>&1; then
  echo "error: postgres is not reachable via docker compose — is the stack up?" >&2
  exit 2
fi
ok "prerequisites: interpreter, free ports, postgres reachable"

# A clean slate: any identity/token from a previous, interrupted run goes first.
purge_gate_rows

# ---------------------------------------------------------------------------
# Start the stub provider and the SSO-enabled API
# ---------------------------------------------------------------------------
echo
echo "-- starting the stub identity provider and an SSO-enabled API"

"$PY" infrastructure/lib/oidc-stub.py \
  --host 127.0.0.1 --port "$IDP_PORT" \
  --issuer "$ISSUER" \
  --client-id "$CLIENT_ID" \
  --client-secret "$CLIENT_SECRET" \
  --redirect-uri "$REDIRECT_URI" \
  >"$WORK/idp.log" 2>&1 &
IDP_PID=$!

idp_ready=0
for _ in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$IDPBASE/health" || true)" = "200" ]; then
    idp_ready=1; break
  fi
  sleep 0.5
done
if [ "$idp_ready" -ne 1 ]; then
  echo "error: the stub identity provider never became ready" >&2
  sed -n '1,40p' "$WORK/idp.log" >&2
  exit 2
fi
ok "stub identity provider is serving discovery, JWKS and tokens"

(
  cd "$ROOT/apps/api" && exec env \
    DATABASE_URL="$DATABASE_URL" \
    REDIS_URL="$REDIS_URL" \
    API_ENVIRONMENT=development \
    API_DEBUG=false \
    LOG_LEVEL=WARNING \
    BACKGROUND_JOBS_ENABLED=false \
    SEED_DEMO=false \
    OIDC_ENABLED=true \
    OIDC_PROVIDER_NAME="$PROVIDER_NAME" \
    OIDC_ISSUER="$ISSUER" \
    OIDC_CLIENT_ID="$CLIENT_ID" \
    OIDC_CLIENT_SECRET="$CLIENT_SECRET" \
    OIDC_REDIRECT_URI="$REDIRECT_URI" \
    OIDC_ROLE_CLAIM=groups \
    OIDC_ADMIN_CLAIM_VALUES='["argus-admins"]' \
    OIDC_OPERATOR_CLAIM_VALUES='["argus-operators"]' \
    OIDC_REQUIRE_VERIFIED_EMAIL=true \
    OIDC_DEFAULT_ROLE=VIEWER \
    OIDC_PROJECT_CLAIM=argus_projects \
    OIDC_SINGLE_SESSION=true \
    OIDC_SESSION_TTL_SECONDS=3600 \
    "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --log-level warning
) >"$WORK/api.log" 2>&1 &
API_PID=$!

api_ready=0
for _ in $(seq 1 120); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health/live" || true)" = "200" ]; then
    api_ready=1; break
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then break; fi
  sleep 1
done
if [ "$api_ready" -ne 1 ]; then
  echo "error: the SSO-enabled API never became ready" >&2
  sed -n '1,60p' "$WORK/api.log" >&2
  exit 2
fi
ok "an SSO-enabled API is serving on :$API_PORT"

# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
RESP="$WORK/resp.json"

jv() {  # jv <python-expression over `d`> — reads the last response body
  "$PY" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
v = $1
sys.stdout.write('' if v is None else str(v))
" "$RESP" 2>/dev/null || true
}

jfile() {  # jfile <file> <python-expression over `d`>
  "$PY" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
v = $2
sys.stdout.write('' if v is None else str(v))
" "$1" 2>/dev/null || true
}

header_location() {  # header_location <headers-file>
  #: `tolower($1)` rather than `/^[Ll]ocation:/`: the response may come from
  #: uvicorn (lowercase header names) or from the stub, and a case-insensitive
  #: regex needs `IGNORECASE`, which BSD/mawk awk silently treats as false — the
  #: first version of this helper only ever matched one of the two servers.
  awk 'tolower($1) == "location:" {sub(/\r$/, ""); sub(/^[^:]*:[[:space:]]*/, ""); print}' "$1" | tail -1
}

api_get() {  # api_get <path> [token]
  if [ -n "${2:-}" ]; then
    curl -s -o "$RESP" -w '%{http_code}' -H "Authorization: Bearer $2" "$BASE$1"
  else
    curl -s -o "$RESP" -w '%{http_code}' "$BASE$1"
  fi
}

api_post() {  # api_post <path> <json> [token]
  if [ -n "${3:-}" ]; then
    curl -s -o "$RESP" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' -H "Authorization: Bearer $3" -d "$2" "$BASE$1"
  else
    curl -s -o "$RESP" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' -d "$2" "$BASE$1"
  fi
}

control() {  # control <json> — change what the next login will assert
  local status
  status="$(curl -s -o "$RESP" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$1" "$IDPBASE/_control")"
  [ "$status" = "200" ] || bad "provider control accepted" "status=$status"
}

#: Drive the first two legs of the flow: the API's redirect to the provider, then
#: the provider's redirect back to the registered callback. Sets LOC, CODE, STATE,
#: BEGIN_STATUS and AUTH_STATUS.
begin_login() {
  BEGIN_STATUS="$(curl -s -o /dev/null -D "$WORK/begin.hdr" -w '%{http_code}' \
    "$BASE/api/v1/auth/oidc/login${1:-}")"
  LOC="$(header_location "$WORK/begin.hdr")"
  CODE=""; STATE=""; AUTH_LOC=""; AUTH_STATUS=""
  case "$LOC" in
    "$IDPBASE"/*) ;;
    *) return 0 ;;
  esac
  AUTH_STATUS="$(curl -s -o /dev/null -D "$WORK/auth.hdr" -w '%{http_code}' "$LOC")"
  AUTH_LOC="$(header_location "$WORK/auth.hdr")"
  read -r CODE STATE < <("$PY" -c "
import sys, urllib.parse as u
q = u.parse_qs(u.urlparse(sys.argv[1]).query)
print(q.get('code', [''])[0], q.get('state', [''])[0])
" "$AUTH_LOC")
}

#: The third leg: the callback page's POST. Sets CB_STATUS, CB_ERROR, TOKEN, ROLE.
finish_login() {
  CB_STATUS="$(api_post /api/v1/auth/oidc/callback "{\"code\":\"$CODE\",\"state\":\"$STATE\"}")"
  CB_ERROR="$(jv "((d.get('detail') or {}) if isinstance(d.get('detail'), dict) else {}).get('error_code','')")"
  TOKEN="$(jv "d.get('token','')")"
  ROLE="$(jv "d.get('role','')")"
}

ctx() {  # ctx <subject> <email> <verified> <groups-json> — the next login's claims
  control "{\"sub\":\"$1\",\"email\":\"$2\",\"email_verified\":$3,\"name\":\"$1\",\"preferred_username\":\"$1\",\"groups\":$4}"
}

identity_id() {  # identity_id <email> <admin-token> — the provisioned person's id
  api_get /api/v1/auth/oidc/identities "$2" >/dev/null
  jv "next((i.get('id') for i in d.get('items', []) if i.get('email') == '$1'), '')"
}

# ---------------------------------------------------------------------------
# 1-3. The redirect, the PKCE, and the code
# ---------------------------------------------------------------------------
echo
echo "-- 1. the public configuration probe"

status="$(api_get /api/v1/auth/oidc/config)"
check "config probe is reachable anonymously (200)" "200" "$status"
check "config probe reports SSO enabled" "True" "$(jv "d.get('enabled')")"
check "config probe names the provider" "$PROVIDER_NAME" "$(jv "d.get('provider_name','')")"
check "config probe shows the registered callback" "$REDIRECT_URI" "$(jv "d.get('redirect_uri','')")"
case "$(cat "$RESP")" in
  *client_secret*) bad "config probe leaks no client secret" "the anonymous probe exposed a secret field" ;;
  *) ok "config probe leaks no client secret" ;;
esac

echo
echo "-- 2. begin: the browser is sent to the provider"

ctx "$ADMIN_SUBJECT" "sso-gate-admin@example.test" true '["argus-admins"]'
begin_login
check "login responds with a redirect" "302" "$BEGIN_STATUS"
contains "the redirect targets the configured issuer" "$IDPBASE/authorize" "$LOC"
contains "the authorization request uses PKCE S256" "code_challenge_method=S256" "$LOC"
contains "the authorization request carries a nonce" "nonce=" "$LOC"
contains "the authorization request carries a state" "state=" "$LOC"
case "$LOC" in
  *code_verifier*) bad "the PKCE verifier never appears in the URL" "found code_verifier in $LOC" ;;
  *) ok "the PKCE verifier never appears in the URL" ;;
esac

echo
echo "-- 3. the provider returns the code to the registered callback"

check "the provider accepted the authorization request" "302" "$AUTH_STATUS"
contains "the provider redirects to the registered callback" "$REDIRECT_URI?" "$AUTH_LOC"
[ -n "$CODE" ] && ok "the callback carries an authorization code" \
  || bad "the callback carries an authorization code" "no code in $AUTH_LOC"

# ---------------------------------------------------------------------------
# 4-6. The session, the edge, and what the provider saw
# ---------------------------------------------------------------------------
echo
echo "-- 4. the callback mints a session from the verified claims"

finish_login
check "the callback returns a session (200)" "200" "$CB_STATUS"
case "$TOKEN" in argus_*) ok "a bearer session was minted" ;; *) bad "a bearer session was minted" "token='${TOKEN:0:12}'" ;; esac
check "the role came from the admin claim value" "ADMIN" "$ROLE"
check "the session is reported as unrestricted (admin)" "True" "$(jv "d.get('unrestricted')")"
check "the session names the provider" "$ISSUER" "$(jv "d.get('provider','')")"
ADMIN_TOKEN="$TOKEN"
ADMIN_ID="$(jv "d.get('token_id','')")"

echo
echo "-- 5. the minted session is accepted by the edge"

check "the admin route is refused without a credential (401)" "401" "$(api_get /api/v1/auth/oidc/identities)"
status="$(api_get /api/v1/auth/oidc/identities "$ADMIN_TOKEN")"
check "the SSO session is admitted to the admin route (200)" "200" "$status"
listed_role="$(jv "next((i.get('role') for i in d.get('items', []) if i.get('email') == 'sso-gate-admin@example.test'), '')")"
check "the person is provisioned with the claim-derived role" "ADMIN" "$listed_role"
listed_count="$(jv "next((i.get('login_count') for i in d.get('items', []) if i.get('email') == 'sso-gate-admin@example.test'), 0)")"
[ "${listed_count:-0}" -ge 1 ] && ok "the login is recorded on the identity (login_count=$listed_count)" \
  || bad "the login is recorded on the identity" "login_count=$listed_count"

#: An admin can see the active-session count, which is the blast radius the
#: disable endpoint reports. Asserted now so section 10 is a comparison.
active_admin_sessions="$(jv "next((i.get('active_sessions') for i in d.get('items', []) if i.get('email') == 'sso-gate-admin@example.test'), 0)")"
[ "${active_admin_sessions:-0}" -ge 1 ] && ok "the admin roster reports an active session" \
  || bad "the admin roster reports an active session" "active_sessions=$active_admin_sessions"

echo
echo "-- 6. what the provider actually saw"

curl -s -o "$WORK/idp-stats.json" "$IDPBASE/_stats" || true
check "the token exchange succeeded" "True" \
  "$("$PY" -c "import json;print(json.load(open('$WORK/idp-stats.json'))['stats']['exchanges'] >= 1)")"
check "PKCE verified on every exchange (0 failures)" "0" \
  "$(jfile "$WORK/idp-stats.json" "d['stats']['pkce_failures']")"
check "client authentication succeeded (0 failures)" "0" \
  "$(jfile "$WORK/idp-stats.json" "d['stats']['client_auth_failures']")"
check "the redirect URI matched the registration (0 mismatches)" "0" \
  "$(jfile "$WORK/idp-stats.json" "d['stats']['redirect_mismatches']")"
check "the provider refused no request" "0" \
  "$(jfile "$WORK/idp-stats.json" "d['stats']['bad_requests']")"

# ---------------------------------------------------------------------------
# 7. Replay and forgery
# ---------------------------------------------------------------------------
echo
echo "-- 7. a spent state and a forged state are both refused"

status="$(api_post /api/v1/auth/oidc/callback "{\"code\":\"$CODE\",\"state\":\"$STATE\"}")"
check "replaying a spent state is refused (400)" "400" "$status"
check "the replay refusal carries its stable code" "state_already_used" \
  "$(jv "((d.get('detail') or {}) if isinstance(d.get('detail'), dict) else {}).get('error_code','')")"

status="$(api_post /api/v1/auth/oidc/callback '{"code":"whatever","state":"forged"}')"
check "a forged state is refused (400)" "400" "$status"
check "the forged-state refusal carries its stable code" "state_unknown" \
  "$(jv "((d.get('detail') or {}) if isinstance(d.get('detail'), dict) else {}).get('error_code','')")"

# ---------------------------------------------------------------------------
# 8-9. The authorization policy, over real HTTP
# ---------------------------------------------------------------------------
echo
echo "-- 8. an unverified email is refused before a credential exists"

ctx "$VIEWER_SUBJECT" "sso-gate-viewer@example.test" false '["argus-operators"]'
begin_login
finish_login
check "an unverified email is refused (400)" "400" "$CB_STATUS"
check "the refusal carries its stable code" "email_not_verified" "$CB_ERROR"

echo
echo "-- 9. a claim with no known group gets the floor, never admin"

ctx "$VIEWER_SUBJECT" "sso-gate-viewer@example.test" true '["everyone"]'
begin_login
finish_login
check "the login succeeds (200)" "200" "$CB_STATUS"
check "the unknown group yields the configured floor role" "VIEWER" "$ROLE"
VIEWER_TOKEN="$TOKEN"

status="$(api_get /api/v1/auth/oidc/identities "$VIEWER_TOKEN")"
check "a non-admin SSO session is refused on the admin route (403)" "403" "$status"

# ---------------------------------------------------------------------------
# 10-12. Revocation, re-enable, and the provider as the authority
# ---------------------------------------------------------------------------
echo
echo "-- 10. disabling an identity revokes every session it holds"

viewer_identity="$(identity_id sso-gate-viewer@example.test "$ADMIN_TOKEN")"
[ -n "$viewer_identity" ] && ok "the viewer identity is listed for an admin" \
  || bad "the viewer identity is listed for an admin" "no match for sso-gate-viewer@example.test"

status="$(api_post "/api/v1/auth/oidc/identities/$viewer_identity/disable" '{}' "$ADMIN_TOKEN")"
check "disable returns 200" "200" "$status"
check "disable reports the sessions it revoked" "True" "$(jv "int(d.get('revoked_sessions', 0)) >= 1")"
check "the revoked session is refused by the edge (401)" "401" "$(api_get /api/v1/auth/oidc/identities "$VIEWER_TOKEN")"

echo
echo "-- 11. a disabled identity cannot sign in again"

ctx "$VIEWER_SUBJECT" "sso-gate-viewer@example.test" true '["argus-operators"]'
begin_login
finish_login
check "the disabled identity is refused at login (403)" "403" "$CB_STATUS"
check "the refusal carries its stable code" "identity_disabled" "$CB_ERROR"

echo
echo "-- 12. re-enabling restores sign-in but not the dead session"

status="$(api_post "/api/v1/auth/oidc/identities/$viewer_identity/enable" '{}' "$ADMIN_TOKEN")"
check "enable returns 200" "200" "$status"
check "enable reports no sessions resurrected" "0" "$(jv "d.get('revoked_sessions')")"

ctx "$VIEWER_SUBJECT" "sso-gate-viewer@example.test" true '["argus-operators"]'
begin_login
finish_login
check "the re-enabled identity can sign in again (200)" "200" "$CB_STATUS"
check "the new session carries the current claim role" "OPERATOR" "$ROLE"
check "the old session is still refused by the edge (401)" "401" "$(api_get /api/v1/auth/oidc/identities "$VIEWER_TOKEN")"

# ---------------------------------------------------------------------------
# 13. The provider is the authority, and single-session holds
# ---------------------------------------------------------------------------
echo
echo "-- 13. a provider-side revocation takes effect at the next login"

ctx "$ADMIN_SUBJECT" "sso-gate-admin@example.test" true '[]'
begin_login
finish_login
check "the admin can sign in again (200)" "200" "$CB_STATUS"
check "the removed admin group is no longer ADMIN" "VIEWER" "$ROLE"
check "the session is no longer unrestricted" "False" "$(jv "d.get('unrestricted')")"
check "the previous session was revoked (single session)" "401" "$(api_get /api/v1/auth/oidc/identities "$ADMIN_TOKEN")"

#: "Updated, not duplicated" is only observable in the row itself — the login
#: arrived with the same ``sub`` and must have matched the existing identity
#: rather than provisioning a second person for the same human.
row_count="$(psql_scalar "select count(*) from external_identities where provider = '$ISSUER' and subject = '$ADMIN_SUBJECT'")"
check "the returning person is one row, not two" "1" "$row_count"
row_logins="$(psql_scalar "select login_count from external_identities where provider = '$ISSUER' and subject = '$ADMIN_SUBJECT'")"
[ "${row_logins:-0}" -ge 2 ] && ok "the identity records every login (login_count=$row_logins)" \
  || bad "the identity records every login" "login_count=${row_logins:-absent}"
row_role="$(psql_scalar "select role from external_identities where provider = '$ISSUER' and subject = '$ADMIN_SUBJECT'")"
check "the stored role matches the last login's claims" "VIEWER" "$row_role"
row_audit="$(psql_scalar "select count(*) from authentication_audit where reason like 'oidc-login:%' and token_id in (select id from api_tokens where created_by = 'oidc:$ISSUER')")"
[ "${row_audit:-0}" -ge 4 ] && ok "every minted session left an audit trail ($row_audit rows)" \
  || bad "every minted session left an audit trail" "${row_audit:-0} oidc-login rows"

# ---------------------------------------------------------------------------
# 14. The counters the alerts depend on
# ---------------------------------------------------------------------------
echo
echo "-- 14. the identity-provider state is exported"

curl -s -o "$WORK/metrics.txt" "$BASE/metrics" || true
metric() { awk -v n="$1" '$1 == n {v=$2} END{if (v != "") print v}' "$WORK/metrics.txt"; }

enabled="$(metric argus_oidc_enabled)"
check "argus_oidc_enabled is 1" "yes" \
  "$(awk -v v="$enabled" 'BEGIN{print (v != "" && v+0 == 1) ? "yes" : "no"}')"
logins="$(metric argus_oidc_logins_total)"
failures="$(metric argus_oidc_login_failures_total)"
[ "${logins:-0}" -ge 4 ] && ok "successful logins were counted ($logins)" \
  || bad "successful logins were counted" "argus_oidc_logins_total=${logins:-absent}"
[ "${failures:-0}" -ge 4 ] && ok "refused logins were counted ($failures)" \
  || bad "refused logins were counted" "argus_oidc_login_failures_total=${failures:-absent}"

#: The registered callback must be a route the web app actually serves. Checked
#: against the source tree so it holds regardless of which web image is running.
callback_path="$("$PY" -c "import sys, urllib.parse as u; print(u.urlparse(sys.argv[1]).path)" "$REDIRECT_URI")"
if [ -f "apps/web/app${callback_path}/page.tsx" ]; then
  ok "the registered callback has a page in the web app ($callback_path)"
else
  bad "the registered callback has a page in the web app" "no apps/web/app${callback_path}/page.tsx"
fi

#: And, when the web tier is up, that the running build serves it. A 404 here
#: means the running web image predates the page — `docker compose build web`
#: — and saying so in the failure detail is the difference between a five-minute
#: fix and an afternoon.
if curl -sf -o /dev/null "http://localhost:3000/" 2>/dev/null; then
  status="$(curl -s -L -o /dev/null -w '%{http_code}' "http://localhost:3000${callback_path}")"
  check "the running web app serves the callback page" "200" "$status"
else
  echo "  note  the web app is not running; skipped the callback-page render check"
fi

echo
echo "== SSO gate: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  echo
  echo "-- api log (last 40 lines)"
  tail -40 "$WORK/api.log" 2>/dev/null || true
  exit 1
fi
exit 0
