#!/usr/bin/env bash
# Runs INSIDE a one-off pod (or container) with the PostgreSQL 18 client
# tools; started by scripts/cnpg-import-legacy.sh, which streams this file
# and the connection settings on stdin, so no password lands in a pod spec.
#
# Copies one database from the legacy Postgres (SRC_*) into the CloudNativePG
# cluster (DST_*): pg_dump | pg_restore in a single transaction, objects owned
# by DST_USER, then ANALYZE and a row-count comparison.
#
# Required env: SRC_HOST SRC_PORT SRC_DB SRC_USER SRC_PASSWORD
#               DST_HOST DST_PORT DST_DB DST_USER DST_PASSWORD
# Optional env: REPLACE=1 drops the target's public schema first (it must
#               otherwise be empty, e.g. before the API ran its migrations).
set -euo pipefail

src() { PGPASSWORD="$SRC_PASSWORD" "$@" -h "$SRC_HOST" -p "$SRC_PORT" -U "$SRC_USER" -d "$SRC_DB"; }
dst() { PGPASSWORD="$DST_PASSWORD" "$@" -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" -d "$DST_DB"; }
q() { "$@" -X -q -v ON_ERROR_STOP=1 -At; }

echo "Source: $SRC_USER@$SRC_HOST:$SRC_PORT/$SRC_DB ($(src q psql -c 'SHOW server_version'))"
echo "Target: $DST_USER@$DST_HOST:$DST_PORT/$DST_DB ($(dst q psql -c 'SHOW server_version'))"

src_major=$(src q psql -c 'SHOW server_version_num')
if [ "$((src_major / 10000))" -gt "$(pg_dump --version | sed -E 's/.* ([0-9]+)\..*/\1/')" ]; then
    echo "Error: the source is newer than this pg_dump; use a newer client image." >&2
    exit 1
fi

existing=$(dst q psql -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [ "$existing" != "0" ]; then
    if [ "${REPLACE:-0}" = "1" ]; then
        echo "Dropping the target's public schema ($existing tables)..."
        dst q psql -c 'SET client_min_messages = warning' \
            -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public'
    else
        echo "Error: the target already has $existing tables in public. Re-run with --replace to drop them." >&2
        exit 1
    fi
fi

echo "Copying (pg_dump | pg_restore, one transaction)..."
src pg_dump -Fc --no-owner --no-acl | dst pg_restore --no-owner --no-acl --single-transaction --exit-on-error
PGPASSWORD="$DST_PASSWORD" vacuumdb --analyze-only --quiet --schema=public \
    -h "$DST_HOST" -p "$DST_PORT" -U "$DST_USER" -d "$DST_DB"

echo "Comparing row counts (table | source | target):"
tables=$(src q psql -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
mismatch=0
for t in $tables; do
    a=$(src q psql -c "SELECT count(*) FROM public.\"$t\"")
    b=$(dst q psql -c "SELECT count(*) FROM public.\"$t\"")
    flag=""
    if [ "$a" != "$b" ]; then flag="  <-- MISMATCH"; mismatch=1; fi
    echo "  $t | $a | $b$flag"
done
if [ "$mismatch" = "1" ]; then
    echo "Error: row counts differ. Were writes still reaching the source?" >&2
    exit 1
fi
echo "Import complete."
