#!/usr/bin/env bash
# ARGUS — scheduled backup loop (hardening W10).
#
# The gap this closes: `backup.sh` could produce a verified, rehearsed dump, but
# only when a human ran it. A recovery strategy that depends on someone
# remembering is not a strategy, and its failure mode is silence — nobody notices
# until the day the data is needed.
#
# This runs as its own container, built from the *database's own image*, for two
# reasons that are worth more than the convenience of reusing the application
# image:
#
#   1. `pg_dump` and `pg_restore` always match the server's major version. A
#      client older than the server refuses to dump at all, and that failure only
#      appears in production;
#   2. the API process — which parses untrusted input from the network — does not
#      need a database-dumping binary. Privilege is not added where it is not
#      required.
#
# It delegates the actual work to `infrastructure/backup.sh run` (mounted
# read-only). That is deliberate: the scheduled path and the manual path are the
# same code, so a fix to one cannot miss the other.
#
# Environment:
#   BACKUP_INTERVAL_HOURS        how often to run (default 24)
#   BACKUP_DRILL_INTERVAL_HOURS  how often to rehearse a restore (default 168)
#   BACKUP_RETENTION_DAYS        how long dumps are kept locally (default 14)
#   BACKUP_DIR                   where dumps are written (default /backups)
set -uo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
SCRIPT="${BACKUP_SCRIPT:-/infrastructure/backup.sh}"

export BACKUP_LOCAL=1
export BACKUP_DIR

ONCE=0
[ "${1:-}" = "--once" ] && ONCE=1

log() { printf '%s backup-scheduler: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

#: Dumps left on the local volume are not a backup strategy on their own — a
#: host loss takes the dumps with it — but an unbounded local directory is its own
#: outage. Retention is documented, not implicit: ship them off-host as well.
prune() {
  local days="$1" pruned=0
  [ -d "$BACKUP_DIR" ] || return 0
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    rm -f "$old" && pruned=$((pruned + 1))
  done < <(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump*' -type f -mtime "+$days" 2>/dev/null)
  [ "$pruned" -gt 0 ] && log "pruned $pruned file(s) older than ${days}d"
  return 0
}

log "scheduler started (every ${INTERVAL_HOURS}h, drill every ${BACKUP_DRILL_INTERVAL_HOURS:-168}h, keep ${RETENTION_DAYS}d)"
log "dumps are written to $BACKUP_DIR inside this container; ship them off-host too"

while true; do
  #: A failed run is recorded in `backup_runs` and alerted on; it must not stop
  #: the schedule, or one bad night would silently end all future backups.
  if bash "$SCRIPT" run --out "$BACKUP_DIR"; then
    log "run completed"
  else
    log "run FAILED (recorded in backup_runs; see the FAILED row for the reason)"
  fi

  prune "$RETENTION_DAYS"

  if [ "$ONCE" -eq 1 ]; then
    log "--once given; exiting"
    exit 0
  fi

  #: `sleep` in the background + `wait`, so a container stop interrupts the sleep
  #: instead of leaving the scheduler to be SIGKILLed mid-dump.
  sleep "$((INTERVAL_HOURS * 3600))" &
  wait $! || true
done
