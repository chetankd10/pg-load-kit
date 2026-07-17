#!/usr/bin/env bash
# run_load.sh — synthetic production-write-traffic generator for a Postgres fork/clone.
#
# Generates three write patterns concurrently:
#   1. SKIP LOCKED job-queue churn   (consumer + producer)
#   2. Integration-sync UPDATE volume on a large table
#   3. Materialized-view refresh (runs alongside)
#
# It DRIVES the .sql scripts in scripts/ via pgbench. Weights (@N) should come from
# scripts/derive_weights.sql run against PRODUCTION first.
#
# ------------------------------------------------------------------------------
# SAFETY: point this at a FORK/CLONE, never production. The guard below refuses
# to run unless you set I_UNDERSTAND_THIS_IS_NOT_PROD=yes.
# ------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

# ---- Config (override via env) ----
URL="${TARGET_DATABASE_URL:?Set TARGET_DATABASE_URL to your FORK/CLONE connection string}"
CLIENTS="${CLIENTS:-60}"          # pgbench -c : concurrent clients (drives contention)
THREADS="${THREADS:-12}"          # pgbench -j : worker threads
DURATION="${DURATION:-900}"       # seconds (long enough for autovacuum to engage)
WARMUP="${WARMUP:-120}"           # seconds of warmup to DISCARD before measuring

# Weights — set these from derive_weights.sql pct_calls ratios
W_CONSUMER="${W_CONSUMER:-6}"
W_PRODUCER="${W_PRODUCER:-2}"
W_UPDATE="${W_UPDATE:-5}"

RUN_MATVIEW="${RUN_MATVIEW:-1}"   # 1 = also run the refresh loop

# Script paths — overridable so the control panel can point at GENERATED scripts
# (built from your real table/column names). Default to the placeholder scripts.
CONSUMER_SQL="${CONSUMER_SQL:-scripts/consumer_skiplocked.sql}"
PRODUCER_SQL="${PRODUCER_SQL:-scripts/producer_enqueue.sql}"
UPDATE_SQL="${UPDATE_SQL:-scripts/integration_update.sql}"

# ---- Safety guard ----
if [[ "${I_UNDERSTAND_THIS_IS_NOT_PROD:-no}" != "yes" ]]; then
  cat <<'EOF'
REFUSING TO RUN.
This generates heavy write load. Point it at a FORK/CLONE, never production.
Re-run with:  I_UNDERSTAND_THIS_IS_NOT_PROD=yes ./run_load.sh
EOF
  exit 1
fi

command -v pgbench >/dev/null || { echo "pgbench not found (install postgres client)."; exit 1; }

echo "=== pg-load-kit ==="
echo "target      : ${URL%%\?*}"
echo "clients=$CLIENTS threads=$THREADS duration=${DURATION}s warmup=${WARMUP}s"
echo "weights     : consumer@$W_CONSUMER producer@$W_PRODUCER update@$W_UPDATE"
echo

# ---- Reset stats so the measurement window is clean ----
echo "[setup] ANALYZE + reset stats..."
psql "$URL" -v ON_ERROR_STOP=1 -c "ANALYZE;" >/dev/null 2>&1 || echo "[setup] ANALYZE skipped"
psql "$URL" -c "SELECT pg_stat_statements_reset();" >/dev/null 2>&1 || echo "[setup] pg_stat_statements_reset skipped (extension not enabled?)"
psql "$URL" -c "SELECT pg_stat_reset();" >/dev/null 2>&1 || true

# ---- Start matview refresh loop alongside (optional) ----
MV_PID=""
if [[ "$RUN_MATVIEW" == "1" ]]; then
  ./scripts/refresh_matview.sh "$URL" &
  MV_PID=$!
  trap '[[ -n "$MV_PID" ]] && kill "$MV_PID" 2>/dev/null || true' EXIT
fi

# ---- Warmup (discarded) ----
if (( WARMUP > 0 )); then
  echo "[warmup] ${WARMUP}s (results discarded)..."
  pgbench "$URL" -n -T "$WARMUP" -c "$CLIENTS" -j "$THREADS" \
    -f "$CONSUMER_SQL"@"$W_CONSUMER" \
    -f "$PRODUCER_SQL"@"$W_PRODUCER" \
    -f "$UPDATE_SQL"@"$W_UPDATE" >/dev/null 2>&1 || true
  psql "$URL" -c "SELECT pg_stat_statements_reset();" >/dev/null 2>&1 || true
fi

# ---- Measured run ----
echo "[run] measured load for ${DURATION}s..."
pgbench "$URL" -n -T "$DURATION" -c "$CLIENTS" -j "$THREADS" -P 30 \
  -f "$CONSUMER_SQL"@"$W_CONSUMER" \
  -f "$PRODUCER_SQL"@"$W_PRODUCER" \
  -f "$UPDATE_SQL"@"$W_UPDATE"

echo
echo "[done] Load complete. Inspect with Heroku pg-extras, e.g.:"
echo "  heroku pg:outliers  -a <app>   # top queries by total time"
echo "  heroku pg:calls     -a <app>   # by frequency"
echo "  heroku pg:locks / pg:blocking  # contention"
echo "  heroku pg:vacuum-stats         # dead rows / autovacuum"
echo "  heroku pg:cache-hit            # cache efficiency"
