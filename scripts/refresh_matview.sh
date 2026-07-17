#!/usr/bin/env bash
# refresh_matview.sh — runs the materialized-view refresh on an interval,
# ALONGSIDE the pgbench load (not inside it) so its locking interacts with live writes.
#
# Usage:  ./scripts/refresh_matview.sh "$TARGET_DATABASE_URL"
# Stop:   Ctrl-C, or the driver kills it automatically when the run ends.
set -euo pipefail

URL="${1:?Usage: refresh_matview.sh <DATABASE_URL>}"
MVIEW="${MVIEW:-my_mv}"                 # EDIT or export MVIEW=...
INTERVAL="${REFRESH_INTERVAL:-60}"      # seconds between refreshes (match prod cadence)
# Use CONCURRENTLY only if the mview has a UNIQUE index (matches prod behavior).
CONCURRENTLY="${REFRESH_CONCURRENTLY:-CONCURRENTLY}"

echo "[matview] refreshing $MVIEW every ${INTERVAL}s (mode: ${CONCURRENTLY:-plain})"
while true; do
  start=$(date +%s)
  psql "$URL" -v ON_ERROR_STOP=1 -c "REFRESH MATERIALIZED VIEW ${CONCURRENTLY} ${MVIEW};" \
    && echo "[matview] refreshed in $(( $(date +%s) - start ))s" \
    || echo "[matview] refresh FAILED (check MVIEW name / unique index for CONCURRENTLY)"
  sleep "$INTERVAL"
done
