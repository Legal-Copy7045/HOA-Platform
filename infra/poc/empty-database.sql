-- Empty the portal's database, keeping the database and its roles (D53, D98).
-- Run as the owner role with psql, ON_ERROR_STOP=1, autocommit (no -1 /
-- --single-transaction) and the runtime role's name as :app:
--
--   psql ... -v ON_ERROR_STOP=1 -v app=<app role> < infra/poc/empty-database.sql
--
-- Used by reseed-demo.sh and restore-portal.sh. One `DROP SCHEMA public
-- CASCADE` takes a lock on every table, index, sequence, row type, constraint
-- and trigger in a single transaction: about 6,700 locks at 0.44.0, more than
-- the VM's Postgres lock table holds (max_locks_per_transaction 64 x about 60
-- slots with max_connections=40), so it failed with "out of shared memory"
-- and psql exit 3 (the 0.44.0 reseed failure). Here every table is dropped in
-- its own statement, so its own transaction (a handful of locks each: the
-- table, its indexes, triggers and the foreign keys on it), and only then the
-- schema with what is left (functions, types, sequences, the btree_gist
-- extension). Views and materialised views go first for the same reason.
SET client_min_messages = warning;
SET lock_timeout = '60s';

SELECT format('DROP VIEW IF EXISTS %I.%I CASCADE', schemaname, viewname)
  FROM pg_views WHERE schemaname = 'public'
\gexec
SELECT format('DROP MATERIALIZED VIEW IF EXISTS %I.%I CASCADE', schemaname, matviewname)
  FROM pg_matviews WHERE schemaname = 'public'
\gexec
SELECT format('DROP TABLE IF EXISTS %I.%I CASCADE', schemaname, tablename)
  FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
\gexec

DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app";
