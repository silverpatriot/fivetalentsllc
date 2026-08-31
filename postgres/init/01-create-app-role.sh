#!/bin/bash
# Runs once, automatically, when the postgres container initializes an
# EMPTY data directory (the official postgres image executes every
# /docker-entrypoint-initdb.d/* script on first boot only — see the note
# in README.md if you already have a populated volume from before this
# existed).
#
# Why this has to exist at all: the official postgres image makes
# POSTGRES_USER a superuser. Superusers bypass row-level security
# unconditionally — ENABLE/FORCE ROW LEVEL SECURITY doesn't change that.
# Migrations need that superuser (CREATE EXTENSION, CREATE POLICY, etc.),
# but the application must NOT run as it, or RLS silently does nothing.
# This role is what backend/celery-worker connect as instead.
set -euo pipefail

: "${APP_DB_USER:?APP_DB_USER must be set}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
            CREATE ROLE "${APP_DB_USER}" LOGIN PASSWORD '${APP_DB_PASSWORD}'
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
        END IF;
    END
    \$\$;
EOSQL
