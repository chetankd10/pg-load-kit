-- demo_schema.sql — throwaway schema so the kit's scripts run as-is for a smoke test.
-- Creates jobs (queue) and big_table (large UPDATE target) matching the placeholders
-- in consumer_skiplocked.sql / producer_enqueue.sql / integration_update.sql, plus a
-- materialized view (with a UNIQUE index so REFRESH ... CONCURRENTLY works).
--
-- Everything lives under the pgloadkit_demo schema OR carries a demo marker so the
-- teardown can remove it cleanly. Seed size is small by default; override with psql
-- variables, e.g.:  psql "$URL" -v big_rows=200000 -f scripts/demo_schema.sql

\set ON_ERROR_STOP on
\if :{?big_rows} \else \set big_rows 50000 \endif
\if :{?job_rows} \else \set job_rows 5000 \endif

-- jobs queue (unqualified name 'jobs' matches the load scripts) --------------
DROP TABLE IF EXISTS jobs CASCADE;
CREATE TABLE jobs (
    id         bigserial PRIMARY KEY,
    payload    text        NOT NULL,
    status     text        NOT NULL DEFAULT 'queued',
    locked_at  timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_status_idx ON jobs (status, id);
INSERT INTO jobs (payload, status)
SELECT repeat('x', 200), 'queued'
FROM generate_series(1, :job_rows);

-- big_table: large UPDATE target -------------------------------------------
DROP TABLE IF EXISTS big_table CASCADE;
CREATE TABLE big_table (
    pk         bigint PRIMARY KEY,
    payload    text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 90);   -- leaves room for HOT updates, like a real sync target
INSERT INTO big_table (pk, payload)
SELECT g, md5(g::text)
FROM generate_series(1, :big_rows) g;

-- materialized view (my_mv) + UNIQUE index for CONCURRENTLY refresh ---------
DROP MATERIALIZED VIEW IF EXISTS my_mv;
CREATE MATERIALIZED VIEW my_mv AS
SELECT status, count(*) AS n, max(created_at) AS newest
FROM jobs GROUP BY status;
CREATE UNIQUE INDEX my_mv_status_uidx ON my_mv (status);

ANALYZE jobs;
ANALYZE big_table;

SELECT 'jobs' AS table, count(*) FROM jobs
UNION ALL SELECT 'big_table', count(*) FROM big_table;
