-- consumer_skiplocked.sql
-- Reproduces job-queue churn: claim queued rows with FOR UPDATE SKIP LOCKED.
-- Concurrency (pgbench -c) is what creates the lock contention this models.
-- EDIT: table/column names to match your queue (jobs / id / status / locked_at).
\set batch 10
BEGIN;
UPDATE jobs
   SET status = 'running', locked_at = now()
 WHERE id IN (
     SELECT id FROM jobs
      WHERE status = 'queued'
      ORDER BY id
        FOR UPDATE SKIP LOCKED
      LIMIT :batch
 )
RETURNING id;
END;
