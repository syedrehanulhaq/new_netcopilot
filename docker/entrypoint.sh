#!/bin/sh
set -e

# All SQLite DBs (settings, memory, alerts, playbooks) are created in
# NETCOPILOT_DATA_DIR — mount a persistent volume there (/app/data by default).
mkdir -p "${NETCOPILOT_DATA_DIR:-/app/data}" /app/logs

exec uvicorn app:app --host 0.0.0.0 --port 8001
