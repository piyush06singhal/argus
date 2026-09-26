#!/usr/bin/env bash
# ARGUS live-gate authentication (hardening W1).
#
# The live gates talk to an auth-enforcing API by default, exactly like a real
# operator. This helper resolves one ADMIN token, caches it for the run, and
# installs a `curl` shim so **every** request in the sourcing script carries
# the credential — including calls made through gate-local helpers such as
# `code()` and `jreq()`. One `source` line per gate keeps ~350 curl call sites
# correct without touching any of them.
#
# Resolution order:
#   1. $ARGUS_TOKEN (explicit — CI and operators who manage tokens themselves)
#   2. the run cache (so the 11 gates share one credential per run instead of
#      minting a recovery token each time)
#   3. `docker compose exec api python -m app.cli bootstrap-token`
#      (mints one on first use, prints it once — the documented operator path)
#
# When none of those resolve, the gates continue with a loud warning: a stack
# running with AUTH_DISABLED=true (the documented local-dev escape hatch) works
# unchanged, and nothing fails silently.
#
# Exit codes are preserved: the shim forwards curl's status, so gates that
# remote only the status code (`-o /dev/null -w '%{http_code}'`) behave exactly
# as before.

ARGUS_GATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARGUS_TOKEN_CACHE="${ARGUS_TOKEN_CACHE:-${TMPDIR:-/tmp}/argus-gate-token}"

if [ -z "${ARGUS_TOKEN:-}" ] && [ -f "$ARGUS_TOKEN_CACHE" ]; then
  ARGUS_TOKEN="$(cat "$ARGUS_TOKEN_CACHE" 2>/dev/null || true)"
fi

if [ -z "${ARGUS_TOKEN:-}" ] && command -v docker >/dev/null 2>&1; then
  if [ -f "$ARGUS_GATE_ROOT/docker-compose.yml" ]; then
    ARGUS_TOKEN="$(
      cd "$ARGUS_GATE_ROOT" \
        && docker compose exec -T api python -m app.cli bootstrap-token 2>/dev/null \
        | tail -1 | tr -d '\r'
    )" || ARGUS_TOKEN=""
    # bootstrap-token prints a recovery notice to stderr when an admin token
    # already exists; stdout carries only the secret.
    case "$ARGUS_TOKEN" in
      argus_*) ;;
      *) ARGUS_TOKEN="" ;;
    esac
  fi
fi

if [ -n "${ARGUS_TOKEN:-}" ]; then
  printf '%s' "$ARGUS_TOKEN" > "$ARGUS_TOKEN_CACHE" 2>/dev/null || true
  # Export so nested subshells (`$(...)`) and the shim see it.
  export ARGUS_TOKEN
  # The credential travels in two shapes because the platform has two edges:
  # the API reads an ``Authorization: Bearer`` header, and the Next.js app
  # reads the same token from a cookie (``argus_token``) so that a
  # *server-rendered* page fetches as its caller — the property that stops a
  # scoped viewer from being shown an administrator's view. A gate that asserts
  # web content is therefore only meaningful with both, and without the cookie
  # every page assertion would silently test the anonymous render.
  # Retry on 429.
  #
  # One gate makes hundreds of calls with one credential and the edge rate
  # limiter (600/min, burst 120) is part of the product, so a long run can
  # legitimately trip it. A 429 means the server *rejected* the request —
  # nothing happened — so a retry is always safe. Without this, a throttled
  # call read as "the page rendered no copy" and a gate failed for a reason
  # unrelated to what it was asserting.
  #
  # Only 429 is retried, and the final attempt's answer is returned verbatim
  # (body, status via the caller's own `-w`, and exit code), so an assertion
  # that *expects* a 429 still observes one.
  curl() {
    local attempt=1 out rc
    while :; do
      out="$(command curl \
        -H "Authorization: Bearer ${ARGUS_TOKEN}" \
        -H "Cookie: argus_token=${ARGUS_TOKEN}" \
        "$@")"
      rc=$?
      case "$out" in
        *"Rate limit exceeded"* | 429 | *$'\n'429)
          if [ "$attempt" -lt 4 ]; then
            sleep "$((attempt * 2))"
            attempt=$((attempt + 1))
            continue
          fi
          ;;
      esac
      printf '%s' "$out"
      return "$rc"
    done
  }
  export -f curl 2>/dev/null || true
else
  echo "warn: no ARGUS_TOKEN resolved; assuming auth is disabled on this stack" >&2
  echo "      (set ARGUS_TOKEN, or run: docker compose exec api python -m app.cli bootstrap-token)" >&2
fi
