#!/usr/bin/env bash
# Creates the two database roles (decision Q8) and the application database.
# Runs once, as the Postgres superuser, when the data volume is first created
# (mounted into /docker-entrypoint-initdb.d by docker-compose.yml). CI runs it
# directly. Values come from the environment; nothing is hard-coded.
#
#   HOA_DB_NAME            database name                     (default: hoa)
#   HOA_OWNER_DB_USER/PASSWORD  owns the schema; migrations only
#   HOA_APP_DB_USER/PASSWORD    runtime role for web and worker
set -euo pipefail

: "${HOA_DB_NAME:=hoa}"
: "${HOA_OWNER_DB_USER:?HOA_OWNER_DB_USER is required}"
: "${HOA_OWNER_DB_PASSWORD:?HOA_OWNER_DB_PASSWORD is required}"
: "${HOA_APP_DB_USER:?HOA_APP_DB_USER is required}"
: "${HOA_APP_DB_PASSWORD:?HOA_APP_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname postgres \
  -v db="$HOA_DB_NAME" \
  -v owner="$HOA_OWNER_DB_USER" -v owner_pw="$HOA_OWNER_DB_PASSWORD" \
  -v app="$HOA_APP_DB_USER" -v app_pw="$HOA_APP_DB_PASSWORD" <<'SQL'
-- CREATEDB lets the owner role create the throwaway test database in CI.
CREATE ROLE :"owner" LOGIN CREATEDB PASSWORD :'owner_pw';
CREATE ROLE :"app" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
CREATE DATABASE :"db" OWNER :"owner";
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"app";
SQL

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname "$HOA_DB_NAME" \
  -v owner="$HOA_OWNER_DB_USER" -v app="$HOA_APP_DB_USER" <<'SQL'
ALTER SCHEMA public OWNER TO :"owner";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app";
SQL
