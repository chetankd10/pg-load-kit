-- integration_update.sql
-- Reproduces integration-sync UPDATE volume on a large table.
-- random_zipfian concentrates writes on a hot row set (s=1.1), like real sync traffic,
-- instead of a uniform spread. Raise s for hotter skew, lower toward 1.0 for flatter.
-- EDIT: max pk range (2nd arg), table/column names (big_table / pk / payload / updated_at).
\set id random_zipfian(1, 100000000, 1.1)
UPDATE big_table
   SET payload = payload,
       updated_at = now()
 WHERE pk = :id;
