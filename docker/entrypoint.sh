#!/bin/sh
set -e

DATA_DIR="/app/data"
LOG_DIR="/app/logs"

mkdir -p "$DATA_DIR" "$LOG_DIR"

# Ensure DB files live on persistent storage.
for name in memory.db alerts.db lldp_cache.db; do
  if [ ! -e "/app/$name" ]; then
    ln -s "$DATA_DIR/$name" "/app/$name"
  fi
  if [ ! -e "$DATA_DIR/$name" ]; then
    touch "$DATA_DIR/$name"
  fi
done

exec uvicorn app:app --host 0.0.0.0 --port 8001
