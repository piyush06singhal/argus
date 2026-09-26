#!/usr/bin/env sh
# ARGUS API container entrypoint.
#
# Runs the schema migration and (optionally) the idempotent demo seed before
# starting the server, so the container is useful on first boot: tables exist
# via Alembic, and a development stack has the ARGUS Demo Commerce dataset for
# the web UI.
#
# The seed itself decides whether to run, from ``SEED_DEMO``:
#   * unset  — seeded outside production, skipped in production;
#   * false  — never seeded (set this on any deployment you care about);
#   * true   — seeded everywhere.
# Keeping the decision inside ``seed_data.py`` means a bare
# ``python seed_data.py`` run behaves exactly like a container boot.
set -e

echo "argus-api: running alembic migrations..."
alembic upgrade head

echo "argus-api: demo seed check (SEED_DEMO)..."
python seed_data.py

echo "argus-api: starting uvicorn..."
exec uvicorn app.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"
