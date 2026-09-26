#!/usr/bin/env bash
# ARGUS — Postgres backup, verify and restore (hardening W8).
#
# Postgres is the only component whose loss is not recoverable by re-deriving
# work from stored telemetry: Redis queues, code snapshots and reproduction
# artifacts can all be rebuilt, incident history cannot. So this script exists
# to make the operation boring, and it is deliberately hostile about the two
# ways a backup strategy fails in practice:
#
#   1. **A backup that was never verified.** `backup` proves the archive it just
#      wrote is complete — it lists the table of contents *and* decompresses the
#      whole archive — and refuses to call it a success otherwise. A truncated
#      dump is worse than no dump, because it is trusted, and a listing check
#      alone passes on one.
#   2. **A restore over live data.** `restore` is a destructive operation. It
#      refuses unless the target database is empty, or `--force` is given, and
#      it asks for the dump's own metadata first so the operator sees what they
#      are about to overwrite with.
#
#   3. **A restore that was never rehearsed.** `drill` is the answer to "are we
#      actually backed up?": it restores the dump with `--exit-on-error` into a
#      *scratch* database, requires every counted table to be present, and
#      compares per-table row counts against a floor recorded before the dump.
#      Verification alone proves the archive is readable; only a drill proves a
#      restore reproduces the data.
#
# Both questions are answered for real. `verify` used to list the archive's table
# of contents, which passes on a file truncated to its first megabyte — the
# header is intact and the data is gone — so it now decompresses the entire
# archive as well. A verification that a truncated dump passes is the same
# failure as having no verification, and it is worse, because it is trusted.
#
# Usage:
#   bash infrastructure/backup.sh backup [--out DIR] [--tag NAME]
#   bash infrastructure/backup.sh run    [--out DIR] [--drill|--no-drill]
#   bash infrastructure/backup.sh verify DUMP
#   bash infrastructure/backup.sh restore DUMP [--force]
#   bash infrastructure/backup.sh drill  [DUMP]
#   bash infrastructure/backup.sh list   [--dir DIR]
#
# `run` is the *scheduled* path: it dumps, verifies, rehearses a restore when one
# is due, and records every attempt in ``backup_runs`` so the API can export
# backup freshness and alert when the recovery point goes stale. It exists as a
# command here — rather than as a separate scheduler script — precisely so the
# scheduled path and the manual path cannot drift apart.
#
# Environment: DATABASE_USER / DATABASE_NAME / POSTGRES_SERVICE override the
# defaults below. By default everything runs through `docker compose exec`, so
# the script never needs the database port exposed.
#
# ``BACKUP_LOCAL=1`` runs the clients *in this host* instead of through compose.
# That is how the scheduler container uses this same script: it is built from the
# database's own image (postgres:16-alpine), so `pg_dump` always matches the
# server's major version, and the API process — which parses untrusted input —
# never needs a database-dumping binary.
set -eu

# Defaults mirror docker-compose.yml and .env.example (POSTGRES_DB/`) exactly.
#
# They used to say `argus`, which is not a database this stack ever creates, so
# the documented `backup` command failed on a first run with
# `database "argus" does not exist` — the kind of defect a doc-and-script pair
# hides until someone actually rehearses their recovery. A `.env` in the repo
# root wins, because that is what compose itself reads.
if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null || true
fi

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
DATABASE_USER="${DATABASE_USER:-argus}"
DATABASE_NAME="${DATABASE_NAME:-argus_db}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
COMPOSE="${COMPOSE:-docker compose}"

command="${1:-}"
shift || true

die() { echo "error: $*" >&2; exit 1; }
info() { echo "  $*"; }

compose_exec() {
  # Local mode: the caller is already inside a container that has the database
  # clients (the scheduler). One branch, so no code path can exist in only one of
  # the two modes.
  if [ "${BACKUP_LOCAL:-0}" = "1" ]; then
    "$@"
    return
  fi
  # shellcheck disable=SC2086
  $COMPOSE exec -T "$POSTGRES_SERVICE" "$@"
}

require_stack() {
  if [ "${BACKUP_LOCAL:-0}" = "1" ]; then
    pg_isready -U "$DATABASE_USER" -q 2>/dev/null ||
      die "postgres is not reachable at ${PGHOST:-localhost}:${PGPORT:-5432} — is the database up?
       (in local mode the client connects over the network; libpq reads PGHOST/PGUSER/PGPASSWORD)"
    return
  fi
  compose_exec pg_isready -U "$DATABASE_USER" -q 2>/dev/null ||
    die "postgres is not reachable via '$COMPOSE exec $POSTGRES_SERVICE' — is the stack up?"
}

#: Tables whose counts the drill compares. Chosen to cover every phase and to
#: be cheap to count; the comparison is exact, so a silently-skipped table shows
#: up as a mismatch rather than as an absence of evidence.
DRILL_TABLES="projects environments system_components observability_events \
anomalies incidents causal_analyses reproductions fixes knowledge_entries \
reliability_forecasts remediation_actions reliability_experiences platform_cases"

#: A drill that is killed (Ctrl-C, a dropped terminal, `docker compose down`
#: mid-restore) cannot run its EXIT trap, so its scratch database survives. Left
#: alone these accumulate silently into tens of megabytes each. Anything this old
#: cannot be a concurrent drill, because a drill that started an hour ago is not
#: still running.
STALE_SCRATCH_SECONDS="${STALE_SCRATCH_SECONDS:-3600}"

#: Fraction of a table's dump-time count the drill tolerates as churn (see the
#: comparison in `drill`). Small tables get a one-row slack instead, so for them
#: the check is effectively exact.
DRIFT_TOLERANCE="${DRIFT_TOLERANCE:-0.02}"

row_counts() {  # <database> <out-file> -> prints the total on stdout
  local db="$1" out="$2" table total=0 count
  : > "$out"
  for table in $DRILL_TABLES; do
    count=$(compose_exec psql -U "$DATABASE_USER" -d "$db" -tAc \
      "select count(*) from $table" 2>/dev/null | tr -d '[:space:]' || true)
    # A table that does not exist yet (an older dump) counts as zero rather than
    # crashing the drill: the comparison below is what reports the difference.
    case "$count" in ''|*[!0-9]*) count=0 ;; esac
    printf '%s %s\n' "$table" "$count" >> "$out"
    total=$((total + count))
  done
  echo "$total"
}

verify_dump() {
  local dump="$1"
  [ -f "$dump" ] || die "no such dump: $dump"
  [ -s "$dump" ] || die "dump is empty: $dump"
  #: A custom-format archive carries a table of contents; listing it is the
  #: cheapest proof that the file is complete rather than a partial write.
  local entries
  entries=$(compose_exec pg_restore --list < "$dump" 2>/dev/null | grep -c "TABLE DATA" || true)
  [ "${entries:-0}" -gt 0 ] || die "dump contains no table data: $dump"

  #: The table of contents sits at the front of the archive, so the check above
  #: passes on a file truncated after it. Decompress the entire archive to
  #: /dev/null: that reads every data block (a few seconds for a real dump) and
  #: fails on any truncation, without touching a database.
  compose_exec pg_restore -f /dev/null < "$dump" >/dev/null 2>&1 ||
    die "dump is not fully readable (truncated or corrupt): $dump"

  info "verified $dump ($entries table-data entries, $(du -h "$dump" | cut -f1), full decompression ok)"
}

#: Remove scratch databases left behind by drills that could not run their EXIT
#: trap (Ctrl-C, a dropped terminal, a container stop mid-restore).
reap_stale_scratch() {
  local now stale db epoch
  now=$(date +%s)
  #: One name per token, and NOT piped through `tr -d '[:space:]'`: that deletes
  #: the newline *separators* as well, gluing two leftovers into a single
  #: ``argus_drill_Aargus_drill_B`` token whose ``epoch`` is not a number — so
  #: every one took the ``continue`` branch and nothing was ever reaped once a
  #: second drill had been killed. Word-splitting on the raw output is what
  #: keeps the names apart. (`order by` only makes the log deterministic.)
  stale=$(compose_exec psql -U "$DATABASE_USER" -d postgres -tAc \
    "select datname from pg_database where datname like 'argus_drill_%' order by datname" 2>/dev/null || true)
  [ -n "$stale" ] || return 0
  for db in $stale; do
    epoch="${db##argus_drill_}"
    case "$epoch" in ''|*[!0-9]*) continue ;; esac
    [ $((now - epoch)) -gt "$STALE_SCRATCH_SECONDS" ] || continue
    info "dropping stale scratch database $db (a previous drill did not exit cleanly)"
    compose_exec psql -U "$DATABASE_USER" -d postgres -q -c \
      "drop database if exists \"$db\"" >/dev/null 2>&1 || true
  done
}

# ---------------------------------------------------------------------------
# Run recording (see app/models/backup.py).
#
# This is deliberate raw SQL from the backup host, and the trade-off is worth
# stating: the alternative — calling a Python client in the scheduler — would
# mean shipping the application image (with a mismatched `pg_dump`) into the
# backup container, which is worse. The coupling is contained to these two
# statements and their columns, and `infrastructure/e2e-smoke-observability.sh`
# runs this exact path and asserts the row lands and the freshness metric moves,
# so a schema change cannot silently break recording.
# ---------------------------------------------------------------------------
record_start() {  # <kind> -> prints the new row id (empty when it cannot)
  #: `head -n 1` is load-bearing: `psql -tAc` prints the returned row *and* the
  #: command tag (``INSERT 0 1``) on the next line, so stripping whitespace
  #: without taking the first line glues them together into
  #: ``<uuid>INSERT01`` — an id that matches nothing. That is exactly what
  #: happened, and because the later update had its errors suppressed, every run
  #: recorded a row that stayed RUNNING forever.
  compose_exec psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -tAc \
    "insert into backup_runs
       (id, kind, status, trigger, started_at, verified, run_by, created_at, updated_at)
     values (gen_random_uuid(), '$1', 'RUNNING', '${2:-SCHEDULED}', now(), false,
             '$(hostname)', now(), now())
     returning id" 2>/dev/null | head -n 1 | tr -d '[:space:]' || true
}

record_finish() {  # <id> <status> <error> <dump> <size> <verified> <tables> <rows> <duration>
  local id="$1" status="$2" error="$3" dump="$4" size="$5" verified="$6"
  local tables="$7" rows="$8" duration="$9"
  [ -n "$id" ] || return 0
  local err_sql="null"
  if [ -n "$error" ]; then
    #: Verbatim, with quotes escaped: the difference between "no space left" and
    #: "authentication failed" is the whole value of the row.
    err_sql="'${error//\'/\'\'}'"
  fi
  #: ``returning id`` + a warning, rather than a silent ``|| true``: a record
  #: write that does nothing is worse than one that fails loudly, because the
  #: metric then reads as "no backup has succeeded" while the backups are fine.
  local updated
  updated=$(compose_exec psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -tAc \
    "update backup_runs set status='$status', finished_at=now(),
       duration_seconds=${duration:-0}, dump_path='$dump', size_bytes=${size:-0},
       verified=$verified, table_count=${tables:-0}, row_count=${rows:-0},
       error=$err_sql, updated_at=now() where id='$id' returning id" \
    2>&1 | head -n 1 | tr -d '[:space:]' || true)
  if [ "$updated" != "$id" ]; then
    info "WARNING: could not record the outcome in backup_runs (id=$id): ${updated:-no response}"
  fi
}

#: Whether a restore rehearsal is due. Weekly by default: a dump is verified on
#: every run, but only a restore proves the archive reproduces the data, and a
#: rehearsal nobody runs is a belief.
#:
#: A missing table (before the first migration) counts as due, so a fresh
#: deployment rehearses immediately rather than reporting success on day one.
drill_due() {
  local interval="${BACKUP_DRILL_INTERVAL_HOURS:-168}" age
  age=$(compose_exec psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -tAc \
    "select coalesce(extract(epoch from (now() - max(started_at)))::bigint, -1)
       from backup_runs where kind = 'DRILL' and status = 'SUCCEEDED' and verified" \
    2>/dev/null | tr -d '[:space:]' || true)
  case "$age" in ''|*[!0-9-]*) echo 1; return ;; esac
  [ "$age" -lt 0 ] && { echo 1; return; }
  [ "$age" -gt $((interval * 3600)) ] && echo 1 || echo 0
}

# Rehearse the restore. This is the only check that answers "if we lost Postgres
# now, would we get the data back?" — `verify` proves the archive is readable,
# and that is a weaker question.
#
# A function rather than a case branch because the scheduled path needs it too:
# the first version called `drill` as if it were a function, so every scheduled
# rehearsal died with "drill: command not found" while the manual command kept
# working — precisely the drift that sharing one implementation prevents.
perform_drill() {  # [DUMP] — defaults to the newest dump in $BACKUP_DIR
  local dump="${1:-}" scratch manifest scratch_counts scratch_total floor_total

  if [ -z "$dump" ]; then
    # shellcheck disable=SC2012
    dump=$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | head -1 || true)
    [ -n "$dump" ] || die "no dump given and none found in $BACKUP_DIR"
    info "using the newest dump: $dump"
  fi
  require_stack
  verify_dump "$dump"
  reap_stale_scratch

  scratch="argus_drill_$(date +%s)"
  info "creating scratch database $scratch"
  compose_exec psql -U "$DATABASE_USER" -d postgres -q -c \
    "create database \"$scratch\"" >/dev/null

  # The scratch database is the drill's only side effect, and it is removed on
  # every path — a leaked scratch database would be found months later.
  cleanup() {
    compose_exec psql -U "$DATABASE_USER" -d postgres -q -c \
      "drop database if exists \"$scratch\"" >/dev/null 2>&1 || true
  }
  trap cleanup EXIT

  info "restoring into $scratch (no --clean: the database is empty by construction)"
  # --exit-on-error: a single unloadable object must fail the drill instead of
  # being skipped, because "restored, with warnings" is not a restore.
  compose_exec pg_restore -U "$DATABASE_USER" -d "$scratch" --no-owner --exit-on-error < "$dump" \
    || die "pg_restore reported an error against a fresh database — the dump is not restorable"

  manifest="$dump.counts"
  if [ ! -s "$manifest" ]; then
    die "no count manifest next to $dump — re-run 'backup' to produce one. The drill needs a reference recorded before the dump; comparing against the live database would report a healthy backup as broken whenever a sweep wrote or revised a row in between."
  fi

  scratch_counts=$(mktemp)
  scratch_total=$(row_counts "$scratch" "$scratch_counts")
  floor_total=$(awk '{sum += $2} END {print sum + 0}' "$manifest")

  info "archive holds $floor_total+ rows (floor recorded at dump time), restored $scratch_total rows"

  # The floor is compared with a tolerance, and the tolerance is the point.
  #
  # Counting rows in a system that is being written while you count cannot be
  # exact in either direction: the tables that carry history (forecasts,
  # experiences) are *revised* — rows are deleted and re-inserted — so a live
  # count wanders by a fraction of a percent around the archive's content.
  # Demanding equality would fail on a healthy backup at random, and an
  # assertion that fires at random trains people to ignore it. The load-bearing
  # checks are the full decompression above and `--exit-on-error`; the counts
  # are the gross-loss detector, so they allow the tables their own churn
  # (DRIFT_TOLERANCE) and nothing more.
  local mismatches
  mismatches=$(awk -v tol="$DRIFT_TOLERANCE" '
    NR == FNR { floor[$1] = $2; next }
    { restored[$1] = $2 }
    END {
      for (table in floor) {
        if (!(table in restored)) { printf "missing from restore: %s\n", table; continue }
        slack = floor[table] * tol
        if (slack < 1) slack = 1
        if (restored[table] + slack < floor[table])
          printf "under-restored: %s floor=%d restored=%d (gap %d > slack %d)\n",
            table, floor[table], restored[table], floor[table] - restored[table], slack
      }
    }' "$manifest" "$scratch_counts")
  if [ -n "$mismatches" ]; then
    echo "row-count differences against the dump-time floor:" >&2
    echo "$mismatches" >&2
    rm -f "$scratch_counts"
    die "restore drill FAILED: the restore did not reproduce the dump — this backup would lose data"
  fi
  rm -f "$scratch_counts"

  #: Explicitly, then clear the trap: a later `exit` must not re-run a cleanup
  #: for a database that is already gone.
  cleanup
  trap - EXIT
  echo "drill ok: $dump restores completely (every counted table present, none under-restored beyond churn)"
}

case "$command" in
  #: The scheduled path. Same dump and same rehearsal as the manual commands —
  #: it composes them, so the two can never drift apart.
  run)
    out_dir="$BACKUP_DIR"
    tag="-scheduled"
    drill_mode="${BACKUP_DRILL:-auto}"
    while [ $# -gt 0 ]; do
      case "$1" in
        --out) out_dir="$2"; shift 2 ;;
        --tag) tag="-$2"; shift 2 ;;
        --drill) drill_mode="yes"; shift ;;
        --no-drill) drill_mode="no"; shift ;;
        *) die "unknown option: $1" ;;
      esac
    done
    require_stack
    mkdir -p "$out_dir"

    run_id=$(record_start FULL)
    if [ -z "$run_id" ]; then
      info "WARNING: could not open a backup_runs row (is the database migrated?)"
      info "         the dump still runs, but no freshness metric will move"
    fi

    started=$(date +%s)
    dump="$out_dir/$DATABASE_NAME-$(date +%Y%m%d-%H%M%S)${tag}.dump"
    manifest="$dump.counts"
    err_file=$(mktemp)

    failure=""
    verified="false"
    size=0
    tables=0
    rows=0

    if ! row_counts "$DATABASE_NAME" "$manifest" >/dev/null 2>"$err_file"; then
      failure="could not record the pre-dump count floor: $(tail -1 "$err_file")"
    elif ! compose_exec pg_dump -U "$DATABASE_USER" -Fc "$DATABASE_NAME" \
      > "$dump" 2>"$err_file"; then
      failure="pg_dump failed: $(tail -1 "$err_file")"
    elif ! ( verify_dump "$dump" ) 2>"$err_file"; then
      #: A subshell, because `die` exits the shell it runs in: running the check
      #: in a subshell turns a fatal check into a catchable one, which is what
      #: lets the failure be *recorded* instead of only printed.
      failure="verification failed: $(tail -1 "$err_file")"
    else
      verified="true"
      size=$(wc -c < "$dump" | tr -d ' ')
      tables=$(wc -l < "$manifest" | tr -d ' ')
      rows=$(awk '{sum += $2} END {print sum + 0}' "$manifest")
    fi

    duration=$(($(date +%s) - started))
    if [ -n "$failure" ]; then
      record_finish "$run_id" FAILED "$failure" "$dump" "$size" "$verified" \
        "$tables" "$rows" "$duration"
      rm -f "$err_file"
      die "backup FAILED: $failure (recorded in backup_runs)"
    fi
    record_finish "$run_id" SUCCEEDED "" "$dump" "$size" "true" "$tables" "$rows" \
      "$duration"
    echo "backup ok: $dump (recorded, ${duration}s)"

    case "$drill_mode" in
      yes) due=1 ;;
      no) due=0 ;;
      *) due=$(drill_due) ;;
    esac
    if [ "$due" -eq 1 ]; then
      drill_id=$(record_start DRILL)
      drill_started=$(date +%s)
      #: `perform_drill` is a function because `drill` is a *case branch* here:
      #: the first version called `drill` as if it were a function and every
      #: scheduled rehearsal died with "drill: command not found".
      if ( perform_drill "$dump" ) 2>"$err_file"; then
        record_finish "$drill_id" SUCCEEDED "" "$dump" "$size" "true" "$tables" \
          "$rows" "$(($(date +%s) - drill_started))"
        echo "drill ok (recorded)"
      else
        reason="restore rehearsal failed: $(tail -1 "$err_file")"
        record_finish "$drill_id" FAILED "$reason" "$dump" "$size" "false" \
          "$tables" "$rows" "$(($(date +%s) - drill_started))"
        rm -f "$err_file"
        die "$reason (recorded in backup_runs)"
      fi
    else
      info "restore rehearsal not due yet (BACKUP_DRILL_INTERVAL_HOURS=${BACKUP_DRILL_INTERVAL_HOURS:-168})"
    fi
    rm -f "$err_file"
    ;;

  backup)
    out_dir="$BACKUP_DIR"
    tag=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --out) out_dir="$2"; shift 2 ;;
        --tag) tag="-$2"; shift 2 ;;
        *) die "unknown option: $1" ;;
      esac
    done
    require_stack
    mkdir -p "$out_dir"
    dump="$out_dir/$DATABASE_NAME-$(date +%Y%m%d-%H%M%S)${tag}.dump"
    manifest="$dump.counts"

    # Record the row counts **before** the dump starts, so they are a floor the
    # archive must contain rather than a claim about what it holds.
    #
    # Reading the counts *after* the dump looks more accurate and is not: ARGUS
    # writes continuously, and `reliability_forecasts` alone revises hundreds of
    # thousands of rows per pass (delete + re-insert), so a post-dump read landed
    # ~1,400 rows *below* the archive's own content and reported a healthy backup
    # as broken. A false alarm on a healthy backup is how a real one gets
    # ignored, so the reference is a floor taken before the snapshot, and the
    # drill compares against it with a tolerance for the tables' own churn
    # (see `drill`).
    row_counts "$DATABASE_NAME" "$manifest" >/dev/null
    info "recorded at-least counts for $(wc -l < "$manifest" | tr -d ' ') tables in ${manifest##*/}"

    info "dumping $DATABASE_NAME (custom format, compressed, consistent snapshot)"
    compose_exec pg_dump -U "$DATABASE_USER" -Fc "$DATABASE_NAME" > "$dump"
    verify_dump "$dump"
    echo "backup ok: $dump"
    ;;

  verify)
    dump="${1:-}"
    [ -n "$dump" ] || die "usage: backup.sh verify DUMP"
    require_stack
    verify_dump "$dump"
    echo "verify ok: $dump"
    ;;

  restore)
    dump="${1:-}"
    force=""
    [ "${2:-}" = "--force" ] && force="yes"
    [ -n "$dump" ] || die "usage: backup.sh restore DUMP [--force]"
    require_stack
    verify_dump "$dump"

    #: Refuse to silently destroy a populated database.
    existing=$(compose_exec psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -tAc \
      "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo 0)
    info "target database currently has ${existing:-0} tables in schema 'public'"
    if [ "${existing:-0}" -gt 0 ] && [ -z "$force" ]; then
      die "target database is not empty — re-run with --force to restore over it"
    fi

    compose_exec pg_restore -U "$DATABASE_USER" -d "$DATABASE_NAME" \
      --clean --if-exists --no-owner < "$dump"
    tables=$(compose_exec psql -U "$DATABASE_USER" -d "$DATABASE_NAME" -tAc \
      "select count(*) from information_schema.tables where table_schema='public'")
    info "restored ${tables:-0} tables"
    echo "restore ok: $dump"
    echo "note: run 'python -m app.cli list-tokens' if you need to confirm which credentials survived"
    ;;

  drill)
    perform_drill "${1:-}"
    ;;

  list)
    dir="$BACKUP_DIR"
    [ "${1:-}" = "--dir" ] && dir="$2"
    [ -d "$dir" ] || die "no backup directory: $dir"
    # shellcheck disable=SC2012
    ls -lh "$dir"/*.dump 2>/dev/null | awk '{print $5"\t"$9}' || echo "(no dumps)"
    ;;

  ""|-h|--help|help)
    sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;

  *)
    die "unknown command: $command (expected run | backup | verify | restore | drill | list)"
    ;;
esac
