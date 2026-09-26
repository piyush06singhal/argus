#!/usr/bin/env bash
# ARGUS — verify everything, in one command.
#
# Runs the whole validation matrix from `docs/production-readiness.md`:
# backend suites, frontend suites, and every live gate against the running
# stack. Exits non-zero if any layer fails, and prints a one-line summary so
# the result is readable at a glance.
#
# Usage:
#   bash infrastructure/verify-all.sh                 # everything
#   bash infrastructure/verify-all.sh --fast          # skip the slow suites
#   bash infrastructure/verify-all.sh --live-only     # gates only
#   GATES="phase8 hardening" bash infrastructure/verify-all.sh --live-only
#                                                     # just these gates (debugging)
#
# Prerequisites: a running stack (`docker compose up -d`). The script checks
# for it and says so rather than failing obscurely.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${API:-http://localhost:8000}"
MODE="${1:-all}"

PASSED=0
FAILED=0
declare -a RESULTS=()

record() {  # record <label> <ok|fail> <detail>
  local label="$1" state="$2" detail="$3"
  if [ "$state" = "ok" ]; then
    PASSED=$((PASSED + 1))
    printf '  \033[32mok\033[0m   %-44s %s\n' "$label" "$detail"
  else
    FAILED=$((FAILED + 1))
    printf '  \033[31mFAIL\033[0m %-44s %s\n' "$label" "$detail"
  fi
  RESULTS+=("$state $label — $detail")
}

echo "=============================================================="
echo " ARGUS verification ($MODE)"
echo "=============================================================="

if ! curl -sf "$API/health/live" >/dev/null 2>&1; then
  echo
  echo "The API is not reachable at $API."
  echo "Start the stack first:  docker compose up -d"
  exit 2
fi

# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------
if [ "$MODE" != "--live-only" ]; then
  echo
  echo "-- backend"
  cd "$ROOT/apps/api" || exit 1

  if [ -x ./.venv/bin/python ]; then
    PY=./.venv/bin/python
  else
    PY=python3
  fi

  out="$("$PY" -m pytest tests/ -q 2>&1 | tail -3)"
  line="$(printf '%s' "$out" | grep -E '[0-9]+ passed' | tail -1)"
  if printf '%s' "$out" | grep -qE '[0-9]+ failed'; then
    record "pytest (unit + integration)" fail "${line:-no summary}"
  else
    record "pytest (unit + integration)" ok "${line:-no summary}"
  fi

  if "$PY" -m ruff check app tests >/dev/null 2>&1; then
    record "ruff" ok "clean"
  else
    record "ruff" fail "see: ruff check app tests"
  fi

  if "$PY" -m mypy app >/dev/null 2>&1; then
    record "mypy" ok "clean"
  else
    record "mypy" fail "see: mypy app"
  fi
fi

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
if [ "$MODE" != "--live-only" ]; then
  echo
  echo "-- frontend"
  cd "$ROOT/apps/web" || exit 1

  out="$(npx vitest run 2>&1 | tail -6)"
  line="$(printf '%s' "$out" | grep -E 'Tests +[0-9]+ passed' | tail -1)"
  if printf '%s' "$out" | grep -qE 'Tests +[0-9]+ failed'; then
    record "vitest" fail "${line:-no summary}"
  else
    record "vitest" ok "${line:-no summary}"
  fi

  if npx tsc --noEmit >/dev/null 2>&1; then
    record "tsc" ok "clean"
  else
    record "tsc" fail "see: npx tsc --noEmit"
  fi

  if [ "$MODE" != "--fast" ]; then
    if npm run build >/dev/null 2>&1; then
      record "next build" ok "succeeds"
    else
      record "next build" fail "see: npm run build"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Live gates
# ---------------------------------------------------------------------------
echo
echo "-- live gates (real HTTP, real database)"
cd "$ROOT" || exit 1

gate() {  # gate <label> <script>
  local label="$1" script="$2" out summary failed=0 frac passed total
  out="$(bash "$script" 2>&1)"
  #: Every shape a gate's own summary takes, because a gate the harness cannot
  #: *read* is reported as a failure even when it passed with zero failures.
  #: The `passed: N` / `failed: N` pair is what phases 4, 5 and 6 print; for a
  #: while those three were reported as "no summary line (did the gate run?)"
  #: while the gates themselves were green — the harness, not the gate, was
  #: wrong, and the count it advertised was therefore false.
  summary="$(printf '%s' "$out" | grep -Ei 'RESULT:|passed, |passed: [0-9]+|PASS=[0-9]+ FAIL=[0-9]+|PASS \([0-9]+/[0-9]+\)|Phase [0-9]+ live smoke' | tail -1)"

  # Failure detection, deliberately explicit. An earlier version matched
  # `failed$`, which lit up on "... 0 failed" — every passing gate was reported
  # as a failure, which is worse than no check at all.
  #
  #   1. "N failed" where N > 0
  #   2. "failed: N" where N > 0 (phases 4/5/6's summary line)
  #   3. "FAIL=N" where N > 0
  #   4. "PASS (a/b)" where a != b
  if printf '%s' "$out" | grep -qE '[^0-9][1-9][0-9]* failed'; then failed=1; fi
  if printf '%s' "$out" | grep -qE 'failed:[[:space:]]*[1-9][0-9]*'; then failed=1; fi
  if printf '%s' "$out" | grep -qE 'FAIL=[1-9][0-9]*'; then failed=1; fi
  frac="$(printf '%s' "$out" | grep -oE 'PASS \([0-9]+/[0-9]+\)' | tail -1)"
  if [ -n "$frac" ]; then
    passed="${frac#PASS (}"; passed="${passed%%/*}"
    total="${frac##*/}"; total="${total%)}"
    [ "$passed" = "$total" ] || failed=1
  fi
  # A gate that produced no summary at all did not prove anything.
  if [ -z "$summary" ]; then failed=1; summary="no summary line (did the gate run?)"; fi

  if [ "$failed" -eq 1 ]; then
    record "$label" fail "${summary:0:60}"
  else
    record "$label" ok "${summary:0:60}"
  fi
}

# GATES="phase8 hardening" restricts the run to the named gates — for
# debugging one failure without re-driving all nineteen.
#
# Matching is exact. A substring match made `phase1` select `phase10` and
# `phase11` too, so a supposedly narrow run drove three gates and mislabelled
# the result.
wanted() {  # wanted <stem>
  [ -z "${GATES:-}" ] && return 0
  local stem="$1" name
  for name in $GATES; do
    [ "$name" = "$stem" ] && return 0
    [ "$stem" = "e2e-smoke-$name" ] && return 0
    [ "$stem" = "e2e-ui-smoke" ] && [ "$name" = "ui" ] && return 0
    [ "$stem" = "e2e-smoke-hardening" ] && [ "$name" = "security" ] && return 0
    [ "$stem" = "e2e-smoke-observability" ] && [ "$name" = "observability" ] && return 0
    [ "$stem" = "e2e-smoke-backup" ] && [ "$name" = "backup" ] && return 0
    [ "$stem" = "e2e-smoke-ha" ] && [ "$name" = "ha" ] && return 0
    [ "$stem" = "e2e-smoke-sso" ] && [ "$name" = "sso" ] && return 0
  done
  return 1
}

for spec in \
  "phase 1 — foundation & ingestion|e2e-smoke-phase1" \
  "phase 2 — knowledge graph|e2e-smoke-phase2" \
  "phase 3 — anomaly & incidents|e2e-smoke-phase3" \
  "phase 4 — causal analysis|e2e-smoke-phase4" \
  "phase 5 — reproduction|e2e-smoke-phase5" \
  "phase 6 — AI debugger|e2e-smoke-phase6" \
  "phase 7 — fix generation|e2e-smoke-phase7" \
  "phase 8 — predictive reliability|e2e-smoke-phase8" \
  "phase 9 — safe remediation|e2e-smoke-phase9" \
  "phase 10 — reliability intelligence|e2e-smoke-phase10" \
  "phase 11 — unified platform|e2e-smoke-phase11" \
  "onboarding — connect a system|e2e-smoke-onboarding" \
  "ui — every route renders|e2e-ui-smoke" \
  "security — auth & isolation|e2e-smoke-hardening" \
  "observability — dashboards & alerts|e2e-smoke-observability" \
  "faults — Redis outage recovery|e2e-smoke-faults" \
  "backup — restore rehearsal|e2e-smoke-backup" \
  "ha — replication & point-in-time recovery|e2e-smoke-ha" \
  "sso — single sign-on end to end|e2e-smoke-sso"
 do
  label="${spec%%|*}"
  stem="${spec##*|}"
  wanted "$stem" || continue
  gate "$label" "infrastructure/$stem.sh"
 done

if [ "$MODE" != "--fast" ] && { [ -z "${GATES:-}" ] || [[ " $GATES " == *" browser "* ]]; }; then
  out="$(cd "$ROOT/apps/web" && npx playwright test 2>&1 | tail -4)"
  summary="$(printf '%s' "$out" | grep -E '[0-9]+ passed' | tail -1)"
  if printf '%s' "$out" | grep -qE '[0-9]+ failed'; then
    record "browser — click paths" fail "${summary:-no summary}"
  else
    record "browser — click paths" ok "${summary:-no summary}"
  fi
fi

echo
echo "=============================================================="
printf ' %s layers verified, %s failed\n' "$PASSED" "$FAILED"
if [ "$FAILED" -gt 0 ]; then
  echo
  echo "Failures:"
  for r in "${RESULTS[@]}"; do
    case "$r" in fail*) echo "  - ${r#fail }";; esac
  done
  echo "=============================================================="
  exit 1
fi
echo " All green."
echo "=============================================================="
exit 0
