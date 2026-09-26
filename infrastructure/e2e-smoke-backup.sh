#!/usr/bin/env bash
# ARGUS hardening W8 — live backup & restore-rehearsal gate.
#
# `backup.sh verify` answers "is this archive readable?". `backup.sh drill`
# answers the question an operator actually cares about — "if Postgres died
# right now, would the data come back?" A backup strategy that is never
# rehearsed is a belief, not a control, and a *check* that a truncated archive
# passes is worse than no check, because it is trusted.
#
# So this gate rehearses the real thing against the running stack, and it
# falsifies its own assertions rather than trusting them:
#
#   1. `backup`   → an archive plus the count manifest recorded at dump time
#   2. `verify`   → accepts the real archive
#   3. `verify`   → REFUSES a truncated copy. The archive's table of contents
#                   sits at the front, so the cheap listing check alone passes
#                   on a file whose data is gone; this assertion is what proves
#                   the full-decompression check is load-bearing
#   4. `drill`    → REFUSES the truncated copy
#   5. `drill`    → restores the real archive with --exit-on-error into a scratch
#                   database, finds every counted table present, and finds none
#                   under-restored beyond the tables' own churn
#   6. drift      → after real telemetry is written through the public API (so
#                   the live database provably no longer matches the dump), the
#                   same drill still passes. Comparing a restore against a
#                   *moving* reference reports a healthy backup as broken
#                   whenever a sweep writes or revises a row in between — and
#                   the high-volume ARGUS tables are revised constantly
#   7. leftovers  → a successful drill leaves no scratch database behind, and a
#                   scratch database left by a drill that was killed is reaped
#                   instead of accumulating
#   8. guard      → `restore` refuses a populated database without --force
#
# Prereq: the stack is up (`docker compose up -d`).
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

source "$(dirname "${BASH_SOURCE[0]}")/lib/gate-auth.sh"
API="${API:-http://localhost:8000}"
PASS=0; FAIL=0
ok()    { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()   { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
check() { [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"; }

PG_USER="${DATABASE_USER:-argus}"
PG_DB="${DATABASE_NAME:-argus_db}"
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

psql_() {  # psql_ <sql> [database]
  docker compose exec -T postgres psql -U "$PG_USER" -d "${2:-$PG_DB}" -tAc "$1" 2>/dev/null | tr -d '[:space:]'
}
scratch_dbs() {  # one scratch database name per line
  #: No `tr -d '[:space:]'` here: it also deletes the newlines that separate the
  #: rows, so two leftovers arrived as a single ``argus_drill_1argus_drill_2``
  #: token — which the cleanup loop could not match against $PREEXISTING and
  #: the reaping assertion could not parse. One name per line is the shape
  #: every caller reads.
  docker compose exec -T postgres psql -U "$PG_USER" -d postgres -tAc \
    "select datname from pg_database where datname like 'argus_drill_%' order by datname" 2>/dev/null
}

WORK="$(mktemp -d)"
#: Scratch databases that already existed before this gate ran. A drill is
#: supposed to reap its own; the ones we saw up front belong to nobody and are
#: only removed by the explicit reaping assertion below.
PREEXISTING="$(scratch_dbs || true)"

#: Membership test against the pre-existing set. `grep -Fxq` over a
#: newline-separated list, because the previous `case " $PREEXISTING " in
#: *" $db "*)` glob stopped matching as soon as a second leftover made the
#: separators newlines rather than spaces.
is_preexisting() {
  printf '%s\n' "$PREEXISTING" | grep -Fxq -- "$1"
}

cleanup() {
  rm -rf "$WORK"
  # Whatever happened, do not leave a scratch database behind — including the
  # one this gate creates on purpose to prove stale ones get reaped.
  local db
  while IFS= read -r db; do
    [ -n "$db" ] || continue
    is_preexisting "$db" && continue
    docker compose exec -T postgres psql -U "$PG_USER" -d postgres -q -c \
      "drop database if exists \"$db\"" >/dev/null 2>&1 || true
  done < <(scratch_dbs || true)
}
trap cleanup EXIT

echo "== ARGUS backup gate: a restore that was actually rehearsed =="

if ! psql_ "select 1" >/dev/null 2>&1; then
  echo "error: postgres is not reachable via docker compose — is the stack up?" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 1. A real backup
# ---------------------------------------------------------------------------
echo
echo "-- 1. backup"

out="$(bash infrastructure/backup.sh backup --out "$WORK" 2>&1)" || {
  bad "backup completes" "$(printf '%s' "$out" | tail -2)"
  echo
  echo "== backup gate: $PASS passed, $FAIL failed"
  exit 1
}
dump="$(ls -1 "$WORK"/*.dump 2>/dev/null | head -1 || true)"
[ -n "$dump" ] && [ -s "$dump" ] && ok "archive written ($(du -h "$dump" | cut -f1))" \
  || bad "archive written" "no non-empty .dump in $WORK"
[ -s "${dump}.counts" ] && ok "count floor recorded ($(wc -l < "${dump}.counts" | tr -d ' ') tables)" \
  || bad "count floor recorded" "no ${dump}.counts"

DUMP_TOTAL="$(awk '{sum += $2} END {print sum + 0}' "${dump}.counts")"
ok "dump-time floor: $DUMP_TOTAL+ rows across the counted tables"

# ---------------------------------------------------------------------------
# 2. verify accepts the real archive
# ---------------------------------------------------------------------------
echo
echo "-- 2. verify"

if bash infrastructure/backup.sh verify "$dump" >/dev/null 2>&1; then
  ok "verify accepts the real archive"
else
  bad "verify accepts the real archive" "exit=$?"
fi

# ---------------------------------------------------------------------------
# 3-4. falsification: a truncated archive must be refused
# ---------------------------------------------------------------------------
echo
echo "-- 3. falsification: a truncated archive is not trusted"

size="$(wc -c < "$dump" | tr -d ' ')"
half=$((size / 2))
truncated="$WORK/truncated.dump"
if [ "$half" -ge 4096 ]; then
  head -c "$half" "$dump" > "$truncated"
  cp "${dump}.counts" "${truncated}.counts"

  # `set -e` would kill the gate on the expected non-zero exit, so the status is
  # captured explicitly rather than read afterwards.
  rc=0
  bash infrastructure/backup.sh verify "$truncated" >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] && ok "verify REFUSES a truncated archive (exit=$rc)" \
    || bad "verify REFUSES a truncated archive" "exit=0 — a dump with no data was reported as verified"

  rc=0
  bash infrastructure/backup.sh drill "$truncated" >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] && ok "drill REFUSES a truncated archive (exit=$rc)" \
    || bad "drill REFUSES a truncated archive" "exit=0 — the drill trusted an unrestorable dump"
else
  bad "falsification ran" "dump too small ($size bytes) to truncate meaningfully"
fi

# ---------------------------------------------------------------------------
# 5. the drill: a real restore into a scratch database
# ---------------------------------------------------------------------------
echo
echo "-- 5. drill (restore into a scratch database)"

out="$(bash infrastructure/backup.sh drill "$dump" 2>&1)" || true
if printf '%s' "$out" | grep -q "drill ok"; then
  ok "drill restores the archive ($(printf '%s' "$out" | grep -o 'archive holds [0-9]*+ rows (floor recorded at dump time), restored [0-9]* rows' | tail -1))"
else
  bad "drill restores the archive" "$(printf '%s' "$out" | tail -2)"
fi

# ---------------------------------------------------------------------------
# 6. drift: the live database moving must not break the drill
# ---------------------------------------------------------------------------
echo
echo "-- 6. drift: live database changes after the dump"

TS="$(date +%s)"
SLUG="backup-drift-$TS"
PROJ="$(curl -fsS -X POST "$API/api/v1/projects" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Backup Drift $TS\",\"slug\":\"$SLUG\",\"description\":\"W8 backup gate drift\"}")"
PID="$(printf '%s' "$PROJ" | jget "d['id']")"
ENV="$(curl -fsS -X POST "$API/api/v1/projects/$PID/environments" -H 'Content-Type: application/json' \
  -d '{"name":"production","environment_type":"PRODUCTION"}')"
EID="$(printf '%s' "$ENV" | jget "d['id']")"
SRC="$(curl -fsS -X POST "$API/api/v1/ingestion/sources" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PID\",\"environment_id\":\"$EID\",\"name\":\"drift-collector\",\"source_type\":\"OTEL\"}")"
SID="$(printf '%s' "$SRC" | jget "d['id']")"
INGEST="$(curl -fsS -X POST "$API/api/v1/ingestion/sources/$SID/rotate-token" | jget "d['ingest_token']")"

before="$(psql_ "select count(*) from observability_events")"
DRIFT_MSG="backup-gate-drift-$TS"
BODY="$WORK/drift.json"
cat > "$BODY" <<JSON
{"project_id":"$PID","resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"checkout"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"$((TS * 1000000000))","severityText":"ERROR","body":{"stringValue":"$DRIFT_MSG"}}]}]}]}
JSON
status="$(command curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/otlp/v1/logs" \
  -H "Authorization: Bearer $INGEST" -H 'Content-Type: application/json' -d "@$BODY")"
case "$status" in
  200|202) ok "real telemetry written after the dump ($status)" ;;
  *) bad "real telemetry written after the dump" "got $status" ;;
esac

# The drift has to be observable, or this step proves nothing: poll the
# source-of-truth count rather than trusting the HTTP status.
after="$before"
for _ in $(seq 1 20); do
  after="$(psql_ "select count(*) from observability_events")"
  [ "${after:-0}" -gt "${before:-0}" ] && break
  sleep 1
done
[ "${after:-0}" -gt "${before:-0}" ] \
  && ok "live database diverged from the dump ($before → $after observability rows)" \
  || bad "live database diverged from the dump" "count stayed at $before — the drift step proved nothing"

# Corroborate the drift against the manifest's own count for that table: the
# live database is now ahead of what the dump recorded, so a drill that compared
# against live rows would report a healthy backup as broken.
dumped_events="$(awk '$1 == "observability_events" {print $2}' "${dump}.counts")"
if [ "${after:-0}" -gt "${dumped_events:-0}" ]; then
  ok "the dump is now stale (${dumped_events:-0} rows at dump time, ${after:-0} live)"
else
  bad "the dump is now stale" "manifest=${dumped_events:-0} live=${after:-0}"
fi

out="$(bash infrastructure/backup.sh drill "$dump" 2>&1)" || true
if printf '%s' "$out" | grep -q "drill ok"; then
  ok "drill still passes after live drift (compares against the dump-time manifest)"
else
  bad "drill after live drift" "$(printf '%s' "$out" | tail -2)"
fi

# ---------------------------------------------------------------------------
# 7. no leftovers, and a killed drill's scratch is reaped
# ---------------------------------------------------------------------------
echo
echo "-- 7. scratch databases"

#: Only the drills *this gate* ran must clean up after themselves. A leftover
#: from a previously killed drill is the next assertion's job, so it must not be
#: reported here as this run's leak — doing so made the gate fail on a stack
#: where an earlier interrupted drill had left a scratch database behind.
left=""
while IFS= read -r db; do
  [ -n "$db" ] || continue
  is_preexisting "$db" && continue
  left="$left $db"
done < <(scratch_dbs || true)
[ -z "$left" ] && ok "a completed drill leaves no scratch database" \
  || bad "a completed drill leaves no scratch database" "found:${left}"

#: Two stale scratch databases on purpose. The reaper used to pipe the name
#: list through `tr -d '[:space:]'`, which deleted the newline separators too and
#: glued the two names into one unparseable token — so *neither* was dropped.
#: A single leftover does not exercise that, which is why this creates two.
stale="argus_drill_$(( $(date +%s) - 7200 ))"
stale2="argus_drill_$(( $(date +%s) - 7100 ))"
for db in "$stale" "$stale2"; do
  docker compose exec -T postgres psql -U "$PG_USER" -d postgres -q -c \
    "create database \"$db\"" >/dev/null 2>&1 || true
done
if [ -n "$(psql_ "select 1 from pg_database where datname = '$stale'" postgres)" ] \
   && [ -n "$(psql_ "select 1 from pg_database where datname = '$stale2'" postgres)" ]; then
  out="$(bash infrastructure/backup.sh drill "$dump" 2>&1)" || true
  if printf '%s' "$out" | grep -q "dropping stale scratch database $stale" \
     && printf '%s' "$out" | grep -q "dropping stale scratch database $stale2"; then
    ok "scratch databases left by killed drills are both reaped"
  else
    bad "scratch databases left by killed drills are both reaped" "$(printf '%s' "$out" | head -2)"
  fi
  gone=1
  for db in "$stale" "$stale2"; do
    [ -z "$(psql_ "select 1 from pg_database where datname = '$db'" postgres)" ] || gone=0
  done
  [ "$gone" -eq 1 ] && ok "the stale scratch databases are both gone" \
    || bad "the stale scratch databases are both gone" "$stale / $stale2 still exist"
else
  bad "stale-scratch setup" "could not create $stale and $stale2"
fi

# ---------------------------------------------------------------------------
# 8. the destructive path stays guarded
# ---------------------------------------------------------------------------
echo
echo "-- 8. restore guard"

tables="$(psql_ "select count(*) from information_schema.tables where table_schema='public'")"
if [ "${tables:-0}" -gt 0 ]; then
  rc=0
  bash infrastructure/backup.sh restore "$dump" >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] && ok "restore refuses a populated database without --force (exit=$rc)" \
    || bad "restore refuses a populated database" "exit=0 — it would have overwritten live data"
else
  bad "restore guard" "the live database has no tables; the guard cannot be exercised"
fi

echo
echo "== backup gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
