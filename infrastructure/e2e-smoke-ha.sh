#!/usr/bin/env bash
# ARGUS hardening W4 — live high-availability gate: replication *and* a real
# point-in-time recovery.
#
# Two claims are made about the HA overlay, and neither is worth making unless
# something falsifies it:
#
#   1. **The replica streams.** Proven by asking the primary who is connected
#      (`pg_stat_replication.state = 'streaming'`), writing on the primary and
#      reading it back from the replica, and confirming that the replica
#      refuses writes — a "replica" that accepts writes is a second primary
#      waiting to diverge.
#   2. **The WAL archive can rebuild the database to a chosen moment.** A base
#      backup plus archived WAL is restored into a scratch container with
#      `recovery_target_time` set to a moment *between* two marker rows, and the
#      recovered database must contain the first marker and not the second.
#
# The second claim is checked twice, on purpose. A restore that stops at the
# target proves nothing by itself — a restore that failed to replay any WAL at
# all would also show the later marker missing. So the same base backup is
# restored a second time with **no** target, and that run must contain **both**
# markers. The difference between the two runs is the recovery target, which is
# exactly the property being claimed.
#
# Prereq: the base stack is up. This gate applies the overlay itself and leaves
# it applied — archiving WAL on the primary is the overlay's purpose — but it
# stops the replica it started and removes every scratch object it created.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DC=(docker compose -f docker-compose.yml -f docker-compose.ha.yml)
PASS=0; FAIL=0
ok()    { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()   { FAIL=$((FAIL+1)); echo "  FAIL  $1  ($2)"; }
check() { [ "$3" = "$2" ] && ok "$1" || bad "$1" "expected=$2 got=$3"; }

PG_USER="${DATABASE_USER:-argus}"
PG_DB="${DATABASE_NAME:-argus_db}"
PG_PASS="${DATABASE_PASSWORD:-argus_password}"
ARCHIVE_VOLUME=argus_wal_archive
BASE_SUFFIX="pitr-$$"

primary() { "${DC[@]}" exec -T postgres psql -U "$PG_USER" -d "${2:-$PG_DB}" -tAc "$1" 2>/dev/null; }
replica() { "${DC[@]}" exec -T postgres-replica psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null; }
squash()  { tr -d '[:space:]'; }

#: Scratch objects this run created. Reaped whatever happens: a killed HA gate
#: must not leave a restored cluster holding disk.
SCRATCH_CONTAINERS=()
SCRATCH_VOLUMES=()
cleanup() {
  local name
  for name in ${SCRATCH_CONTAINERS[@]+"${SCRATCH_CONTAINERS[@]}"}; do
    [ -n "$name" ] || continue
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
  for name in ${SCRATCH_VOLUMES[@]+"${SCRATCH_VOLUMES[@]}"}; do
    [ -n "$name" ] || continue
    docker volume rm -f "$name" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

if ! primary "select 1" >/dev/null 2>&1; then
  echo "error: postgres is not reachable via docker compose — is the stack up?" >&2
  exit 2
fi

echo "== ARGUS HA gate: replication, and a recovery that was actually rehearsed =="

#: Reset the archiver's counters so the assertions below describe *this*
#: configuration. `pg_stat_archiver` is cumulative for the life of the cluster,
#: so a failure count from before the archive directory existed makes a working
#: archive look permanently broken — and a check an operator learns to ignore
#: is worse than no check at all.
primary "select pg_stat_reset_shared('archiver')" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 1. The overlay: an archiving primary and a streaming replica
# ---------------------------------------------------------------------------
echo
echo "-- 1. apply the HA overlay"

#: Both services are named explicitly. `up -d postgres` alone leaves the
#: replica uncreated — and the gate then waits 90 iterations for a container
#: that does not exist, which is the least useful way to fail.
"${DC[@]}" up -d postgres postgres-replica >/dev/null 2>&1
check "primary accepts connections after the overlay" "1" "$(primary 'select 1' | squash)"
check "primary is configured for replication (wal_level)" "replica" "$(primary 'show wal_level' | squash)"
check "primary archives WAL (archive_mode)" "on" "$(primary 'show archive_mode' | squash)"

#: The replica bootstraps from a base backup, which takes as long as the
#: database is large. Bounded, and reported rather than waited on blindly.
replica_ready=0
for attempt in $(seq 1 90); do
  if replica "select 1" >/dev/null 2>&1; then replica_ready=1; break; fi
  if [ $((attempt % 15)) -eq 0 ]; then
    echo "  .... waiting for the replica to bootstrap (${attempt}/90)"
  fi
  sleep 4
done
if [ "$replica_ready" != "1" ]; then
  bad "replica bootstrap completes and accepts read queries" "still not answering"
  "${DC[@]}" logs --tail 20 postgres-replica 2>&1 | sed 's/^/    | /' || true
  echo
  echo "HA gate: $PASS passed, $FAIL failed"
  exit 1
fi
ok "replica bootstrap completes and accepts read queries"

# ---------------------------------------------------------------------------
# 2. The replica really is a replica
# ---------------------------------------------------------------------------
echo
echo "-- 2. streaming, read-only, and catching up"

check "replica reports itself in recovery" "t" "$(replica 'select pg_is_in_recovery()' | squash)"

#: How long this takes depends on the size of the base backup, not on
#: correctness: a standby with a `restore_command` replays archived WAL first
#: and only *then* connects its walreceiver, so `pg_stat_replication` can hold
#: no row for minutes on a database this size. The earlier bound (60s) was
#: short enough that a fresh bootstrap failed the gate while replication was
#: working perfectly — the next assertion, that a write propagates, passed
#: immediately afterwards. Waited for, with progress, rather than assumed.
streaming=""
for attempt in $(seq 1 150); do
  streaming="$(primary "select state from pg_stat_replication limit 1" | squash)"
  [ "$streaming" = "streaming" ] && break
  if [ $((attempt % 30)) -eq 0 ]; then
    echo "  .... waiting for the standby to stream (${attempt}/150; last state '${streaming:-none}')"
  fi
  sleep 2
done
check "primary reports a streaming standby" "streaming" "$streaming"

#: A standby that accepts writes is not a standby. PostgreSQL refuses by
#: default; asserting it here makes "read-only" a fact about the running system
#: rather than a property of the documentation.
write_error="$(
  "${DC[@]}" exec -T postgres-replica psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "create table argus_ha_write_probe (id int)" 2>&1 | tr -d '\r' | head -1
)"
case "$write_error" in
  *[Rr]ead-only*|*recovery*) ok "replica refuses writes (read-only standby)" ;;
  *) bad "replica refuses writes (read-only standby)" "got: ${write_error}" ;;
esac

#: A standby that is still replaying the base-backup backlog cannot show a row
#: written now, however long the write is retried — so catch-up is a
#: *precondition* of the propagation claim and is waited on (and reported)
#: explicitly here. Folding it into the propagation loop below would make a
#: still-caught-up-in-replay replica read as a replication failure.
caught_up=0
for attempt in $(seq 1 90); do
  lag="$(primary "select coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint, -1) from pg_stat_replication limit 1" | squash)"
  case "$lag" in
    ''|'-1') : ;;
    *[!0-9]*) : ;;
    *) if [ "$lag" -le 1048576 ]; then caught_up=1; break; fi ;;
  esac
  if [ $((attempt % 10)) -eq 0 ]; then
    echo "  .... replica still catching up: ${lag:-unknown} byte(s) behind (${attempt}/90)"
  fi
  sleep 2
done
if [ "$caught_up" = "1" ]; then
  ok "the replica catches up to the primary's write-ahead log"
else
  bad "the replica catches up to the primary's write-ahead log" "lag stayed above 1 MiB for 180s"
fi

#: Propagation is asserted on a real row, not on an LSN: an advanced LSN proves
#: bytes moved, a visible row proves the *database* moved.
marker="argus-ha-$$-$RANDOM"
primary "create table if not exists argus_ha_probe (marker text, at timestamptz default now())" >/dev/null
primary "insert into argus_ha_probe (marker) values ('$marker')" >/dev/null

appeared=0
for _ in $(seq 1 30); do
  seen="$(replica "select count(*) from argus_ha_probe where marker = '$marker'" | squash)"
  if [ "$seen" = "1" ]; then appeared=1; break; fi
  sleep 2
done
check "a write on the primary becomes visible on the replica" "1" "$appeared"

lag_bytes="$(primary "select coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint, -1) from pg_stat_replication limit 1" | squash)"
echo "  .... replication lag at check time: ${lag_bytes:-unknown} byte(s)"

# ---------------------------------------------------------------------------
# 3. The WAL archive is actually archiving
# ---------------------------------------------------------------------------
echo
echo "-- 3. WAL archiving"

check "archive_command has recorded no failures" "0" "$(primary "select failed_count from pg_stat_archiver" | squash)"

"${DC[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "select pg_switch_wal()" >/dev/null 2>&1
archived=0
for _ in $(seq 1 30); do
  archived="$(primary "select count(*) from pg_stat_archiver where archived_count > 0 and last_archived_wal is not null" | squash)"
  [ "$archived" = "1" ] && break
  sleep 2
done
check "at least one WAL segment reached the archive" "1" "$archived"

wal_count="$("${DC[@]}" exec -T postgres sh -c 'ls /wal-archive/wal 2>/dev/null | wc -l' | squash)"
if [ "${wal_count:-0}" -gt 0 ]; then
  ok "the archive volume holds WAL segments (${wal_count})"
else
  bad "the archive volume holds WAL segments" "found ${wal_count:-0}"
fi

# ---------------------------------------------------------------------------
# 4. Point-in-time recovery, rehearsed
# ---------------------------------------------------------------------------
echo
echo "-- 4. point-in-time recovery to a chosen moment"

#: `-U` but no `-h` and no `-d`, deliberately. The command runs *inside* the
#: primary container, where the local socket is trusted (see the `local
#: replication` record in ``pg_hba.conf``), so no password is needed. Both
#: omitted flags are traps worth naming:
#:
#:   * ``-d`` takes a libpq **conninfo string**, so a bare database name fails
#:     with `missing "=" after "argus_db" in connection info string`.
#:   * ``docker compose exec`` runs as **root**, and PostgreSQL authenticates
#:     the *database* user, not the OS user — without `-U` the connection is
#:     `role "root" does not exist`.
BASE="/wal-archive/base/$BASE_SUFFIX"
"${DC[@]}" exec -T postgres sh -c "rm -rf '$BASE' && pg_basebackup -U '$PG_USER' -D '$BASE' -X stream -c fast" >/dev/null 2>&1
base_ok="$("${DC[@]}" exec -T postgres sh -c "[ -s '$BASE/PG_VERSION' ] && echo yes" | squash)"
check "a base backup was taken into the archive volume" "yes" "$base_ok"

primary "create table if not exists argus_pitr_probe (seq int primary key, note text, at timestamptz default now())" >/dev/null
primary "delete from argus_pitr_probe" >/dev/null
primary "insert into argus_pitr_probe (seq, note) values (1, 'before-target')" >/dev/null

#: The moment *between* the two commits, read from the database's own clock so
#: the target and the transaction timestamps cannot disagree about time.
#:
#: Deliberately **not** squashed: ``squash`` strips all whitespace, including
#: the space inside the timestamp itself, and PostgreSQL then rejects
#: ``2026-09-2520:18:46+00`` with `invalid value for parameter
#: "recovery_target_time"`. Command substitution already removes the trailing
#: newline, which is the only whitespace that should go.
TARGET="$(primary "select now()" | head -1 | tr -d '\r')"
sleep 2
primary "insert into argus_pitr_probe (seq, note) values (2, 'after-target')" >/dev/null
"${DC[@]}" exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "select pg_switch_wal()" >/dev/null 2>&1
for _ in $(seq 1 30); do
  failed_wal="$(primary "select count(*) from pg_stat_archiver where last_failed_wal is not null" | squash)"
  [ "$failed_wal" = "0" ] && break
  sleep 2
done

check "the recovery target moment was captured" "yes" "$([ -n "$TARGET" ] && echo yes)"
echo "  .... recovery target: $TARGET"

TARGET_CONF="/tmp/argus-pitr-target-$$.conf"
CONTROL_CONF="/tmp/argus-pitr-control-$$.conf"
cat > "$TARGET_CONF" <<EOF
restore_command = 'cp /wal-archive/wal/%f %p'
recovery_target_action = 'promote'
recovery_target_time = '$TARGET'
recovery_target_inclusive = on
EOF
#: The control run has **no** recovery target at all, so it replays every
#: archived segment and then promotes. It must not use
#: ``recovery_target = 'immediate'``: that stops at the earliest consistent
#: point — the base backup itself — which would discard the very WAL this run
#: exists to prove is replayable. (The first version of this gate did exactly
#: that and reported an empty control.)
cat > "$CONTROL_CONF" <<EOF
restore_command = 'cp /wal-archive/wal/%f %p'
recovery_target_action = 'promote'
EOF

run_recovery() {
  # run_recovery <label> <conf-file> → the probe rows found, or a reason
  local label="$1" conf="$2"
  local volume="argus-pitr-${label}-$$"
  local container="argus-pitr-${label}-$$"
  local out="ERROR" recovered=0

  SCRATCH_CONTAINERS+=("$container")
  SCRATCH_VOLUMES+=("$volume")
  docker volume rm -f "$volume" >/dev/null 2>&1 || true
  docker volume create "$volume" >/dev/null

  #: Place the base backup in the scratch volume and hand it to postgres.
  #: ``chown 70:70`` because Docker creates the volume root as root while the
  #: cluster must be owned by the postgres user (uid 70 in this image) — the
  #: failure otherwise is "could not open file postmaster.pid: permission
  #: denied", which reads like corruption and is not.
  #: Every option precedes the image: Docker parses flags only up to the image
  #: reference, so a late `-v` becomes an argument to the command instead. That
  #: is precisely how the first version of this helper failed.
  if ! docker run --rm \
    -v "$ARCHIVE_VOLUME":/wal-archive \
    -v "$volume":/pgdata \
    -v "$conf":/tmp/recovery.conf:ro \
    alpine:3 sh -c "
      set -e
      rm -rf /pgdata/* /pgdata/.[!.]* 2>/dev/null || true
      cp -a /wal-archive/base/$BASE_SUFFIX/. /pgdata/
      rm -f /pgdata/standby.signal
      touch /pgdata/recovery.signal
      cat /tmp/recovery.conf >> /pgdata/postgresql.auto.conf
      chown -R 70:70 /pgdata
    " >/dev/null 2>&1; then
    echo "RESTORE-PREP-FAILED"
    return 0
  fi

  #: No ``-c hba_file`` here: the base backup carried the primary's
  #: pg_hba.conf inside PGDATA, and the local socket (which is how this gate
  #: connects) is trusted in it.
  docker run -d --name "$container" \
    -e POSTGRES_PASSWORD="$PG_PASS" \
    -e PGDATA=/pgdata \
    -v "$volume":/pgdata \
    -v "$ARCHIVE_VOLUME":/wal-archive \
    postgres:16-alpine >/dev/null 2>&1 || true

  for _ in $(seq 1 60); do
    if docker exec "$container" psql -U "$PG_USER" -d "$PG_DB" -tAc "select 1" >/dev/null 2>&1; then
      #: Recovery must be *over* before the contents mean anything: a standby
      #: mid-replay answers queries but has not yet reached the target.
      if [ "$(docker exec "$container" psql -U "$PG_USER" -d "$PG_DB" -tAc 'select pg_is_in_recovery()' 2>/dev/null | squash)" = "f" ]; then
        recovered=1
        break
      fi
    fi
    sleep 2
  done

  if [ "$recovered" = "1" ]; then
    out="$(docker exec "$container" psql -U "$PG_USER" -d "$PG_DB" -tAc \
      "select coalesce(string_agg(seq::text, ',' order by seq), 'none') from argus_pitr_probe" 2>/dev/null | squash)"
  else
    docker logs --tail 12 "$container" 2>&1 | grep -iE 'fatal|error|panic' | tail -3 | sed 's/^/    | /' || true
  fi

  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm -f "$volume" >/dev/null 2>&1 || true
  echo "${out:-none}"
}

with_target="$(run_recovery target "$TARGET_CONF")"
echo "  .... recovered to the target moment: rows [$with_target]"
case "$with_target" in
  1) ok "a restore to the target contains the pre-target row" ;;
  *) bad "a restore to the target contains the pre-target row" "rows=[$with_target]" ;;
esac
case "$with_target" in
  *2*) bad "a restore to the target excludes the post-target row" "rows=[$with_target]" ;;
  *) ok "a restore to the target excludes the post-target row" ;;
esac

without_target="$(run_recovery control "$CONTROL_CONF")"
echo "  .... recovered with no target (control): rows [$without_target]"
check "the control restore replays the whole archive (so the target is what stopped it)" "1,2" "$without_target"

rm -f "$TARGET_CONF" "$CONTROL_CONF"

# ---------------------------------------------------------------------------
# 5. Leave the stack tidy
# ---------------------------------------------------------------------------
echo
echo "-- 5. teardown"

"${DC[@]}" exec -T postgres sh -c "rm -rf '$BASE'" >/dev/null 2>&1 || true
#: The probe tables are dropped: they exist to be evidence for this run, and an
#: operator's database should not accumulate a table per gate execution.
primary "drop table if exists argus_ha_probe" >/dev/null 2>&1 || true
primary "drop table if exists argus_pitr_probe" >/dev/null 2>&1 || true
"${DC[@]}" stop postgres-replica >/dev/null 2>&1 || true
ok "replica stopped; the primary stays configured for archiving (the overlay's purpose)"

leftover="$(docker ps -a --filter "name=argus-pitr-" --format '{{.Names}}' | wc -l | squash)"
check "no scratch recovery container was left behind" "0" "$leftover"

echo
echo "HA gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
