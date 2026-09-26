#!/usr/bin/env bash
# ARGUS — streaming replica bootstrap.
#
# Two jobs, in this order, and the order is the whole design:
#
#   1. If this volume has never been bootstrapped, take a base backup from the
#      primary (`pg_basebackup -R`) into it. `-R` writes standby.signal and the
#      primary_conninfo, which is what makes the resulting cluster a *standby*
#      rather than a second, divergent primary.
#   2. Hand over to the image's own entrypoint, so the replica runs the same
#      startup path as every other postgres container.
#
# Idempotent by construction: the base backup only runs when PG_VERSION is
# missing. A restart of a replica container therefore resumes streaming rather
# than re-copying the database — which matters, because a re-copy of a busy
# primary is a full table scan of everything on the primary's disk.
#
# A physical replication slot is used so the primary does not recycle WAL the
# replica has not consumed. The slot is created here (not assumed), and the
# creation is tolerant of a restart that finds it already present.
set -euo pipefail

PRIMARY_HOST="${PRIMARY_HOST:-postgres}"
PRIMARY_PORT="${PRIMARY_PORT:-5432}"
REPL_USER="${DATABASE_USER:-argus}"
REPL_DB="${DATABASE_NAME:-argus_db}"
SLOT="${REPLICATION_SLOT:-argus_replica_slot}"
DATA_DIR="${PGDATA:-/var/lib/postgresql/data}"

export PGPASSWORD="${DATABASE_PASSWORD:-argus_password}"

log() { echo "[replica] $*"; }

if [ ! -s "${DATA_DIR}/PG_VERSION" ]; then
  log "no cluster in ${DATA_DIR}; bootstrapping from ${PRIMARY_HOST}:${PRIMARY_PORT}"

  # Nothing may be left in the directory: pg_basebackup refuses a non-empty
  # target, and a half-written directory from a killed bootstrap would
  # otherwise be treated as a valid cluster.
  mkdir -p "${DATA_DIR}"
  find "${DATA_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

  # Wait for the primary to answer. `depends_on: service_healthy` covers the
  # normal path; this covers a primary that is up but still recovering.
  for attempt in $(seq 1 60); do
    if pg_isready -h "${PRIMARY_HOST}" -p "${PRIMARY_PORT}" -U "${REPL_USER}" -d "${REPL_DB}" >/dev/null 2>&1; then
      break
    fi
    log "waiting for primary (attempt ${attempt}/60)"
    sleep 2
  done

  # The slot, if it is not already there. `SELECT ... WHERE NOT EXISTS` is a
  # set-returning function over a single row, so this is one statement that is
  # safe to repeat.
  psql \
    -h "${PRIMARY_HOST}" -p "${PRIMARY_PORT}" -U "${REPL_USER}" -d "${REPL_DB}" \
    -v ON_ERROR_STOP=1 \
    -c "SELECT pg_create_physical_replication_slot('${SLOT}')
          WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '${SLOT}');" \
    >/dev/null

  pg_basebackup \
    -h "${PRIMARY_HOST}" -p "${PRIMARY_PORT}" -U "${REPL_USER}" \
    -D "${DATA_DIR}" \
    -X stream -c fast -R -S "${SLOT}"

  log "base backup complete"
else
  log "cluster already present in ${DATA_DIR}; resuming streaming"
fi

# `-c hot_standby=on` is the image default, but stating it here means a
# replica that cannot serve reads is a configuration mistake rather than a
# silent one. `primary_slot_name` is set by -R; repeating it is harmless.
exec docker-entrypoint.sh postgres \
  -c hot_standby=on \
  -c primary_slot_name="${SLOT}" \
  -c wal_level=replica
