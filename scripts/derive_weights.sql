-- derive_weights.sql  (READ-ONLY — run ONCE against PRODUCTION, before testing)
-- Extracts the real write mix from pg_stat_statements so pgbench weights (@N)
-- reflect production reality instead of a guess.
--
-- Usage:
--   psql "$PROD_DATABASE_URL" -f scripts/derive_weights.sql
--
-- Map the resulting pct_calls to the @weights in run_load.sh:
--   SKIP LOCKED / UPDATE-on-queue  -> consumer_skiplocked.sql@N
--   INSERT into queue              -> producer_enqueue.sql@N
--   UPDATE on large table          -> integration_update.sql@N
-- (Round to small integers whose ratio matches pct_calls.)

SELECT
    queryid,
    calls,
    rows,
    round(100.0 * calls / NULLIF(sum(calls) OVER (), 0), 1) AS pct_calls,
    round((total_exec_time / NULLIF(sum(total_exec_time) OVER (), 0) * 100)::numeric, 1) AS pct_time,
    left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS query_snippet
FROM pg_stat_statements
WHERE query ILIKE 'UPDATE%'
   OR query ILIKE 'INSERT%'
   OR query ILIKE 'DELETE%'
   OR query ILIKE '%FOR UPDATE SKIP LOCKED%'
   OR query ILIKE '%REFRESH MATERIALIZED%'
ORDER BY calls DESC
LIMIT 30;
