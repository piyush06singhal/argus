#!/usr/bin/env sh
# ARGUS API container entrypoint.
#
# Runs the schema migration and idempotent seed before starting the server so the
# container is useful on first boot (tables exist via Alembic, and the ARGUS
# Demo Commerce dataset is present for the web UI).
set -e

echo "argus-api: running alembic migrations..."
alembic upgrade head

echo "argus-api: seeding demo data (idempotent)..."
python seed_data.py

echo "argus-api: starting uvicorn..."
exec uvicorn app.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"