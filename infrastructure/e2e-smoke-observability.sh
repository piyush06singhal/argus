#!/usr/bin/env bash
# ARGUS hardening W10 — self-observability gate.
#
# The gap this closes: ARGUS could be scraped, but nothing scraped it, and there
# were no alert rules of its own. Shipping rules, however, is exactly the kind of
# change that *looks* done and is not: PromQL is not type-checked, and a rule
# naming a misremembered series loads without complaint and then never fires.
# A rule that can never fire reads as coverage, which is worse than no rule.
#
# So this gate treats the monitoring configuration as code under test:
#
#   1. structure   → every rule has an expr, a `for`, a severity, a summary and
#                    a runbook link; names are unique
#   2. references  → every `argus_*` series named in a rule or on the dashboard
#                    exists in the live scrape, or is declared by the exporter
#                    but legitimately absent because the data it summarises does
#                    not exist yet (see the note next to the check). Not
#                    "should exist": scraped from the running API, or shown to
#                    be a real name the code emits rather than a typo.
#   3. coverage    → every series the API emits is either alerted on, on the
#                    dashboard, or listed below with a reason. An unmonitored
#                    series is a decision, never an accident
#   4. dashboard   → the JSON parses, every panel target has an expr, and every
#                    panel points at the provisioned datasource uid
#   5. live rules  → the rules load into a real Prometheus, and *every* expr is
#                    evaluated against real data via the query API. This is what
#                    catches a typo that YAML validation cannot see
#   6. scrape      → Prometheus reports the API target up, proving the whole
#                    path (API → /metrics → Prometheus) rather than the config
#   7. dashboard   → Grafana serves the provisioned dashboard over its API
#
# Prereq: the stack is up (`docker compose up -d`). Prometheus and Grafana are
# started by this gate and left running, because they are part of the deployment
# a gate is verifying.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

API="${API:-http://localhost:8000}"
PROM="${PROMETHEUS_URL:-http://localhost:${PROMETHEUS_PORT:-9090}}"
GRAFANA="${GRAFANA_URL:-http://localhost:${GRAFANA_PORT:-3001}}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-argus_grafana_local}"

ALERTS="infrastructure/observability/prometheus/alerts.yml"
PROM_CONFIG="infrastructure/observability/prometheus/prometheus.yml"
DASHBOARD="infrastructure/observability/grafana/dashboards/argus-overview.json"
DATASOURCE="infrastructure/observability/grafana/provisioning/datasources/prometheus.yml"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }

#: Series the API emits that are deliberately not alerted on and not on the
#: dashboard, each with the reason it is a decision rather than an oversight.
#: Empty is the goal; anything added here has to justify itself in a sentence.
UNMONITORED=()

echo "=============================================================="
echo " ARGUS observability gate (self-monitoring)"
echo "=============================================================="

if ! curl -sf "$API/metrics" >/dev/null 2>&1; then
  echo
  echo "The API is not reachable at $API (or /metrics is not public)."
  echo "Start the stack first:  docker compose up -d"
  exit 2
fi

# ---------------------------------------------------------------------------
# 1. rule structure
# ---------------------------------------------------------------------------
echo
echo "-- 1. rule structure"

structure="$(awk '
  function flush() {
    if (name == "") return
    missing = ""
    if (!has_expr) missing = missing " expr"
    if (!has_for)  missing = missing " for"
    if (!has_sev)  missing = missing " severity"
    if (!has_sum)  missing = missing " summary"
    if (!has_run)  missing = missing " runbook_url"
    if (missing != "") print "MISSING " name ":" missing
    print "NAME " name
  }
  /^[[:space:]]*- alert:/ {
    flush()
    name = $0
    sub(/.*- alert:[[:space:]]*/, "", name)
    has_expr = has_for = has_sev = has_sum = has_run = 0
    next
  }
  /^[[:space:]]*expr:/        { has_expr = 1 }
  /^[[:space:]]*for:/         { has_for = 1 }
  /^[[:space:]]*severity:/    { has_sev = 1 }
  /^[[:space:]]*summary:/     { has_sum = 1 }
  /runbook_url:/              { has_run = 1 }
  END { flush() }
' "$ALERTS")"

rule_count="$(printf '%s\n' "$structure" | grep -c '^NAME ' || true)"
if [ "${rule_count:-0}" -gt 0 ]; then
  ok "$rule_count alert rules are present"
else
  bad "no alert rules found in $ALERTS"
fi

if printf '%s\n' "$structure" | grep -q '^MISSING '; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    bad "rule is missing required fields: ${line#MISSING }"
  done < <(printf '%s\n' "$structure" | grep '^MISSING ')
else
  ok "every rule has expr, for, severity, summary and a runbook link"
fi

duplicates="$(printf '%s\n' "$structure" | grep '^NAME ' | sort | uniq -d || true)"
if [ -z "$duplicates" ]; then
  ok "alert names are unique"
else
  bad "duplicate alert names: $(printf '%s' "$duplicates" | tr '\n' ' ')"
fi

#: Multi-line scalars would make the expression extraction below silently
#: incomplete, so require one `expr:` per expression.
expr_declared="$(grep -cE '^[[:space:]]*expr:' "$ALERTS" || true)"
expr_expressions="$(grep -E '^[[:space:]]*expr:' "$ALERTS" \
  | sed 's/^[[:space:]]*expr:[[:space:]]*//')"
expr_lines="$(printf '%s\n' "$expr_expressions" | grep -c . || true)"
if [ "${expr_declared:-0}" = "${expr_lines:-0}" ]; then
  ok "every expression is a single-line scalar ($expr_lines)"
else
  bad "multi-line expr scalars ($expr_declared declared, $expr_lines parsed)"
fi

# ---------------------------------------------------------------------------
# 1b. the scheduled backup path really runs (and is why its series exist)
#
# Several series are *absent* until the feature has run — deliberately, because
# zero would mean "a backup finished in 1970" rather than "there has never been
# one". So the reference check below can only be satisfied by exercising the
# schedule, which is the stronger proof anyway: the deployment mode is the
# long-running service, not a one-shot command.
# ---------------------------------------------------------------------------
echo
echo "-- 1b. scheduled backup path"

psql_t() { docker compose exec -T postgres psql -U "${DATABASE_USER:-argus}" \
  -d "${DATABASE_NAME:-argus_db}" -tAc "$1" 2>/dev/null | tr -d '[:space:]' || true; }

before="$(psql_t "select count(*) from backup_runs where status='SUCCEEDED'" || true)"
case "$before" in ''|*[!0-9]*) before=0 ;; esac

if docker compose --profile backup up -d backup >/dev/null 2>&1; then
  ok "the backup scheduler service starts and keeps a loop alive"
else
  bad "the backup scheduler service could not be started"
fi

#: One shot *through the service definition* — same image, same environment,
#: same entrypoint — because waiting for the long-running loop would mean waiting
#: a day for the next run. Both paths execute `backup.sh run`, so this is the
#: scheduled code taking the scheduled decision (including "is a drill due?").
if docker compose --profile backup run --rm --no-deps backup --once >/dev/null 2>&1; then
  ok "a scheduled run completes end to end"
else
  bad "the scheduled run did not complete (see: docker compose logs backup)"
fi

succeeded="$(psql_t "select count(*) from backup_runs where status='SUCCEEDED'" || true)"
case "$succeeded" in ''|*[!0-9]*) succeeded="$before" ;; esac
if [ "$succeeded" -gt "$before" ]; then
  ok "the run recorded a successful backup in backup_runs"
else
  last="$(psql_t "select coalesce(error,'no row') from backup_runs order by started_at desc limit 1" || true)"
  bad "no success was recorded (newest row: ${last:-none})"
fi

#: A status alone is not evidence. The row must carry the proof the archive was
#: fully read and its real size, or the metric would report freshness for a file
#: nobody has checked.
proof="$(psql_t "select size_bytes from backup_runs where status='SUCCEEDED' and verified order by started_at desc limit 1" || true)"
case "$proof" in ''|*[!0-9]*) proof=0 ;; esac
if [ "$proof" -gt 0 ]; then
  ok "the recorded backup is verified and $((proof / 1048576)) MiB on disk"
else
  bad "no verified backup with a recorded size exists"
fi

echo
echo "-- 2. metric references (against the live scrape)"

live_families="$(curl -sf "$API/metrics" \
  | grep -oE '^# TYPE argus_[a-z0-9_]+' | awk '{print $3}' | sort -u)"
live_count="$(printf '%s\n' "$live_families" | grep -c . || true)"
ok "the API exposes $live_count metric families"

referenced="$(
  {
    grep -oE 'argus_[a-z0-9_]+' "$ALERTS"
    grep -oE 'argus_[a-z0-9_]+' "$DASHBOARD"
  } | sort -u
)"

# A referenced family must be one of two things:
#
#   * scraped from the live API — the strong case, and the only one that proves
#     the whole path (rule → /metrics → Prometheus) works; or
#   * absent, but a name the application source really emits, quoted as a
#     string literal. A family can be correctly named and still missing from a
#     fresh deployment: a population statistic (MTTA/MTTR) has no value until
#     something has been resolved.
#
# The second case is deliberately narrow, and its narrowness is enforced by
# tests rather than by this script: `tests/test_hardening_metrics.py` asserts
# that every enum-labelled counter in the exporter is emitted for every enum
# member (so `_by_status`/`_by_result` families cannot be absent) and that the
# only families allowed to depend on data are the population statistics.
#
# Source text is what makes the weak case honest: a typo, a rename applied on
# one side only, or a rule written against a metric that never existed all fail
# it. Anything taking the weak path is printed, so the data-dependent series are
# visible in the gate output rather than assumed.
declared_in_source="$(grep -rhoE "'argus_[a-z0-9_]+'|\\\"argus_[a-z0-9_]+\\\"" \
  apps/api/app 2>/dev/null | tr -d "'\\\"" | sort -u || true)"

unexplained=""
deferred=""
scraped=0
while IFS= read -r name; do
  [ -z "$name" ] && continue
  if printf '%s\n' "$live_families" | grep -qx "$name"; then
    scraped=$((scraped + 1))
    continue
  fi
  if printf '%s\n' "$declared_in_source" | grep -qx "$name"; then
    deferred="$deferred $name"
  else
    unexplained="$unexplained $name"
  fi
done < <(printf '%s\n' "$referenced")

if [ -z "$unexplained" ]; then
  ok "$scraped referenced series are really scraped from the running API"
else
  bad "referenced but neither scraped nor emitted by any app source file:$unexplained"
fi

#: Both branches assert, so the gate's assertion count is the same on every
#: run. It used to `ok` only in the deferred case, which made the count depend
#: on whether data existed yet — and a number that moves is a number no
#: documented total can be checked against.
if [ -n "$deferred" ]; then
  ok "$(printf '%s' "$deferred" | wc -w | tr -d ' ') are declared in code but absent until data exists:$deferred"
else
  ok "every referenced series is present in the live scrape (none deferred)"
fi

# ---------------------------------------------------------------------------
# 3. coverage — every emitted series is a decision
# ---------------------------------------------------------------------------
echo
echo "-- 3. coverage of the metric surface"

unmonitored=""
while IFS= read -r name; do
  [ -z "$name" ] && continue
  printf '%s\n' "$referenced" | grep -qx "$name" && continue
  allowed=0
  for listed in ${UNMONITORED[@]+"${UNMONITORED[@]}"}; do
    [ "$listed" = "$name" ] && allowed=1
  done
  [ "$allowed" -eq 0 ] && unmonitored="$unmonitored $name"
done < <(printf '%s\n' "$live_families")

if [ -z "$unmonitored" ]; then
  ok "every emitted series is alerted on, on the dashboard, or listed with a reason"
else
  bad "emitted but unmonitored:$unmonitored"
fi

# ---------------------------------------------------------------------------
# 4. dashboard and datasource structure
# ---------------------------------------------------------------------------
echo
echo "-- 4. dashboard structure"

dash_check="$(python3 - "$DASHBOARD" <<'PY'
import json, sys

path = sys.argv[1]
try:
    with open(path) as handle:
        dashboard = json.load(handle)
except Exception as exc:  # noqa: BLE001
    print(f"PARSE {exc}")
    raise SystemExit(0)

problems = []
panels = dashboard.get("panels") or []
if not panels:
    problems.append("no panels")

for panel in panels:
    title = panel.get("title", "?")
    targets = panel.get("targets") or []
    if panel.get("type") in {"row", "text", "dashlist"}:
        continue
    if not targets:
        problems.append(f"{title}: no targets")
    for target in targets:
        if not (target.get("expr") or "").strip():
            problems.append(f"{title}: target without an expr")
    datasource = panel.get("datasource") or {}
    if datasource.get("uid") != "argus-prometheus":
        problems.append(f"{title}: datasource uid is not argus-prometheus")

for variable in (dashboard.get("templating") or {}).get("list") or []:
    query = variable.get("query") or {}
    text = query.get("query") if isinstance(query, dict) else str(query)
    if not (text or "").strip():
        problems.append(f"variable {variable.get('name')}: no query")

print(f"PANELS {len(panels)}")
for problem in problems:
    print(f"PROBLEM {problem}")
PY
)"
if printf '%s' "$dash_check" | grep -q '^PARSE '; then
  bad "dashboard JSON does not parse: ${dash_check#PARSE }"
else
  ok "dashboard JSON parses (${dash_check%%$'\n'*})"
  if printf '%s' "$dash_check" | grep -q '^PROBLEM '; then
    while IFS= read -r line; do
      bad "dashboard: ${line#PROBLEM }"
    done < <(printf '%s' "$dash_check" | grep '^PROBLEM ')
  else
    ok "every panel has an expr and the provisioned datasource"
  fi
fi

if grep -q 'uid: argus-prometheus' "$DATASOURCE" \
  && grep -q 'url: http://prometheus:9090' "$DATASOURCE"; then
  ok "the provisioned datasource targets the prometheus service"
else
  bad "datasource provisioning does not point at the prometheus service"
fi

if grep -q 'job_name: argus-api' "$PROM_CONFIG" \
  && grep -q 'api:8000' "$PROM_CONFIG" \
  && grep -q 'alerts.yml' "$PROM_CONFIG"; then
  ok "prometheus scrapes the api service and loads the rule file"
else
  bad "prometheus.yml does not scrape the api service with the rules loaded"
fi

# ---------------------------------------------------------------------------
# 5/6. live Prometheus: the rules load, and every expression evaluates
# ---------------------------------------------------------------------------
echo
echo "-- 5. live rule evaluation"

if ! docker compose --profile observability up -d prometheus >/dev/null 2>&1; then
  bad "prometheus could not be started (docker compose --profile observability)"
else
  ready=0
  for _ in $(seq 1 30); do
    if curl -sf "$PROM/-/ready" >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then
    bad "prometheus did not become ready at $PROM"
  else
    ok "prometheus is up at $PROM"

    loaded="$(curl -sf "$PROM/api/v1/rules" \
      | python3 -c '
import json, sys
data = json.load(sys.stdin)
names = []
for group in (data.get("data") or {}).get("groups") or []:
    for rule in group.get("rules") or []:
        names.append(rule.get("name") or "")
print("\n".join(sorted(n for n in names if n)))
' 2>/dev/null || true)"

    unexpectedly_missing=""
    while IFS= read -r name; do
      [ -z "$name" ] && continue
      printf '%s\n' "$loaded" | grep -qx "$name" || unexpectedly_missing="$unexpectedly_missing $name"
    done < <(printf '%s\n' "$structure" | grep '^NAME ' | sed 's/^NAME //')

    if [ -z "$unexpectedly_missing" ]; then
      ok "all $rule_count rules are loaded by prometheus"
    else
      bad "rules not loaded by prometheus:$unexpectedly_missing"
    fi

    # Every expression is evaluated for real. This is the check YAML validation
    # cannot do: a misremembered function or label is only visible to PromQL.
    broken=""
    checked=0
    while IFS= read -r expr; do
      [ -z "$expr" ] && continue
      checked=$((checked + 1))
      status="$(curl -sG "$PROM/api/v1/query" --data-urlencode "query=$expr" \
        | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("status",""))
except Exception:
    print("")' 2>/dev/null)"
      [ "$status" = "success" ] || broken="$broken [${expr:0:40}]"
    done < <(printf '%s\n' "$expr_expressions" | grep .)
    if [ -z "$broken" ]; then
      ok "all $checked expressions evaluate against live data"
    else
      bad "expressions Prometheus refused:$broken"
    fi

    # 6. the whole scrape path, not just the config.
    #
    # Polled, because the target series only exists *after* Prometheus's first
    # scrape — up to one `scrape_interval` (30s) after it becomes ready. Reading
    # the series once here measured the gate's own impatience, and reported a
    # healthy target as "missing".
    target=""
    for _ in $(seq 1 40); do
      target="$(curl -sG "$PROM/api/v1/query" \
        --data-urlencode 'query=up{job="argus-api"}' \
        | python3 -c 'import json,sys
try:
    result = json.load(sys.stdin)["data"]["result"]
    print(result[0]["value"][1] if result else "missing")
except Exception:
    print("missing")' 2>/dev/null)"
      [ "$target" = "1" ] && break
      sleep 3
    done
    if [ "$target" = "1" ]; then
      ok "prometheus reports the argus-api target up"
    else
      bad "prometheus reports the argus-api target as: $target"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 7. Grafana serves the provisioned dashboard
# ---------------------------------------------------------------------------
echo
echo "-- 7. grafana"

if ! docker compose --profile observability up -d grafana >/dev/null 2>&1; then
  bad "grafana could not be started"
else
  ready=0
  for _ in $(seq 1 45); do
    if curl -sf "$GRAFANA/api/health" >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then
    bad "grafana did not become ready at $GRAFANA"
  else
    ok "grafana is up at $GRAFANA"
    served="$(curl -sf -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
      "$GRAFANA/api/dashboards/uid/argus-overview" 2>/dev/null || true)"
    if printf '%s' "$served" | grep -q 'argus-overview'; then
      ok "the dashboard is provisioned and served by grafana"
    else
      bad "grafana does not serve the provisioned dashboard"
    fi
  fi
fi

echo
echo "== observability gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
