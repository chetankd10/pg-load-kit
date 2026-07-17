-- producer_enqueue.sql
-- Feeds the queue so consumers have work + generates INSERT/dead-tuple churn.
-- EDIT: table/column names and payload shape to match your queue.
INSERT INTO jobs (payload, status, created_at)
VALUES (repeat('x', 200), 'queued', now());
