#!/bin/sh
set -eu
: "${SQLPILOT_READONLY_PASSWORD:=change_readonly}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v reader_password="$SQLPILOT_READONLY_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE sqlpilot_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'sqlpilot_reader')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO sqlpilot_reader', current_database())
\gexec
GRANT USAGE ON SCHEMA public TO sqlpilot_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sqlpilot_reader;
SQL
