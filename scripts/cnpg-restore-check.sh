#!/usr/bin/env bash
# Prove the backups restore: restore the latest backup into a throwaway
# cluster, compare it with the live database, then delete it. A backup only
# counts once it has been restored. Run it regularly (e.g. monthly) and
# after any change to the backup configuration.
#
# Usage: scripts/cnpg-restore-check.sh [--keep] [--timeout DURATION]
#   --keep     leave the restored cluster running for inspection (delete it
#              later: kubectl -n <ns> delete cluster <name>)
#   --timeout  how long to wait for the restore (default 60m)
#
# Fails (exit 1) when the restore fails, the schema version differs, a table
# is missing, or the restored database is empty while the live one is not.
# Row counts and the newest award time are reported; the live database keeps
# changing, so they are expected to be slightly ahead of the restore.
#
# The throwaway cluster is GAMIFICATIONENGINE_CNPG_RESTORE_CLUSTER_NAME
# (default game-db-restore-test), one instance, on
# "<GAMIFICATIONENGINE_CNPG_STORAGE_CLASS>-delete" so its volumes are deleted
# with it.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/cnpg/lib.sh
source scripts/cnpg/lib.sh
load_env_file

KEEP=0
TIMEOUT=60m
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --timeout) TIMEOUT=$2; shift 2 ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

NS=${GAMIFICATIONENGINE_NAMESPACE:?set GAMIFICATIONENGINE_NAMESPACE}
LIVE=${GAMIFICATIONENGINE_CNPG_CLUSTER_NAME:?set GAMIFICATIONENGINE_CNPG_CLUSTER_NAME}
DB=${GAMIFICATIONENGINE_DB_NAME:?set GAMIFICATIONENGINE_DB_NAME}
TEST=${GAMIFICATIONENGINE_CNPG_RESTORE_CLUSTER_NAME:-game-db-restore-test}
CLASS="${GAMIFICATIONENGINE_CNPG_STORAGE_CLASS:?set GAMIFICATIONENGINE_CNPG_STORAGE_CLASS}-delete"

cleanup() {
    if [ "$KEEP" = "1" ]; then
        echo "Kept cluster $TEST (delete it with: kubectl -n $NS delete cluster $TEST)."
    elif kubectl -n "$NS" get cluster "$TEST" >/dev/null 2>&1; then
        echo "Deleting cluster $TEST..."
        kubectl -n "$NS" delete cluster "$TEST" --wait=false >/dev/null
    fi
}

started=$(date +%s)
trap cleanup EXIT
scripts/cnpg-restore.sh --name "$TEST" --instances 1 --storage-class "$CLASS" \
    --timeout "$TIMEOUT" --yes
restore_seconds=$(( $(date +%s) - started ))

live() { cluster_psql "$NS" "$LIVE" "$DB" "$1"; }
restored() { cluster_psql "$NS" "$TEST" "$DB" "$1"; }

failures=0
fail() { echo "FAIL: $*"; failures=$((failures + 1)); }

echo
echo "Restore took ${restore_seconds}s."

live_version=$(live "SELECT version_num FROM alembic_version")
restored_version=$(restored "SELECT version_num FROM alembic_version") || restored_version=""
echo "Schema version: live $live_version, restored ${restored_version:-<none>}"
[ "$live_version" = "$restored_version" ] || fail "schema version differs"

tables_sql="SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
live_tables=$(live "$tables_sql")
restored_tables=$(restored "$tables_sql")
missing=$(comm -23 <(printf '%s\n' "$live_tables") <(printf '%s\n' "$restored_tables"))
[ -z "$missing" ] || fail "tables missing from the restore: $(echo $missing)"

echo "Row counts (table | live | restored):"
live_total=0
restored_total=0
for t in $live_tables; do
    a=$(live "SELECT count(*) FROM public.\"$t\"")
    if printf '%s\n' "$restored_tables" | grep -qx "$t"; then
        b=$(restored "SELECT count(*) FROM public.\"$t\"")
    else
        b="-"
    fi
    echo "  $t | $a | $b"
    # Application data only: alembic_version always has its one row.
    [ "$t" = "alembic_version" ] && continue
    live_total=$((live_total + a))
    [ "$b" = "-" ] || restored_total=$((restored_total + b))
done
if [ "$live_total" -gt 0 ] && [ "$restored_total" -eq 0 ]; then
    fail "the restored database is empty"
fi

newest_sql="SELECT COALESCE(to_char(max(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '-') FROM userpoints"
if printf '%s\n' "$restored_tables" | grep -qx userpoints; then
    echo "Newest award: live $(live "$newest_sql"), restored $(restored "$newest_sql")"
fi

echo
if [ "$failures" -gt 0 ]; then
    echo "Restore check FAILED ($failures problem(s))."
    exit 1
fi
echo "Restore check passed."
