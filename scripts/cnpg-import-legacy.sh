#!/usr/bin/env bash
# Optional, one-time: copy the application database from the legacy
# single-instance Postgres Deployment into the CloudNativePG cluster.
# Not needed for a fresh install. See docs/source/operations.rst
# ("Importing an existing database").
#
# Usage: scripts/cnpg-import-legacy.sh [--replace] [--yes]
#   --replace  drop the target's existing tables first (for example the
#              empty schema the API's migrations created)
#   --yes      do not ask for confirmation
#
# Reads .env like deploy-kubernetes.sh (or the file named by ENV_FILE). The source defaults to the legacy
# Service (GAMIFICATIONENGINE_SERVICE_NAME_POSTGRES) with the same database
# name and credentials as the target; override with LEGACY_DB_HOST,
# LEGACY_DB_PORT, LEGACY_DB_NAME, LEGACY_DB_USER and LEGACY_DB_PASSWORD.
#
# Stop writes first (scale the API to 0): the copy is a snapshot.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=${ENV_FILE:-.env}
if [ -f "$ENV_FILE" ]; then
    # Literal KEY=VALUE lines (no shell expansion, so "$" in passwords is
    # safe); surrounding quotes are stripped. Already-set variables win.
    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        key=${BASH_REMATCH[1]}
        value=${BASH_REMATCH[2]}
        value=${value%%[[:space:]]#*}
        if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
            value=${BASH_REMATCH[1]}
        fi
        [ -n "${!key+x}" ] || export "$key=$value"
    done < "$ENV_FILE"
fi

REPLACE=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --replace) REPLACE=1 ;;
        --yes) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

NS=${GAMIFICATIONENGINE_NAMESPACE:?set GAMIFICATIONENGINE_NAMESPACE}
CLUSTER=${GAMIFICATIONENGINE_CNPG_CLUSTER_NAME:?set GAMIFICATIONENGINE_CNPG_CLUSTER_NAME}
DB_NAME=${GAMIFICATIONENGINE_DB_NAME:?}
DB_USER=${GAMIFICATIONENGINE_DB_USER:?}
DB_PASSWORD=${GAMIFICATIONENGINE_DB_PASSWORD:?}
DB_PORT=${GAMIFICATIONENGINE_DB_PORT:-5432}
# The cluster's own image: its client tools match the server version.
IMAGE=$(kubectl -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.spec.imageName}')
: "${IMAGE:?could not read the image of cluster $CLUSTER in namespace $NS}"

SRC_HOST=${LEGACY_DB_HOST:-${GAMIFICATIONENGINE_SERVICE_NAME_POSTGRES:-postgres}}
SRC_PORT=${LEGACY_DB_PORT:-$DB_PORT}
SRC_DB=${LEGACY_DB_NAME:-$DB_NAME}
SRC_USER=${LEGACY_DB_USER:-$DB_USER}
SRC_PASSWORD=${LEGACY_DB_PASSWORD:-$DB_PASSWORD}

echo "Copy $SRC_DB from $SRC_HOST:$SRC_PORT into $DB_NAME on $CLUSTER-rw (namespace $NS)."
[ "$REPLACE" = "1" ] && echo "--replace: existing tables in the target will be DROPPED."
if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "Writes to the source must be stopped. Continue? [y/N] " answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 1
fi

{
    for v in SRC_HOST SRC_PORT SRC_DB SRC_USER SRC_PASSWORD REPLACE; do
        printf 'export %s=%q\n' "$v" "${!v}"
    done
    printf 'export %s=%q\n' DST_HOST "$CLUSTER-rw" DST_PORT "$DB_PORT" \
        DST_DB "$DB_NAME" DST_USER "$DB_USER" DST_PASSWORD "$DB_PASSWORD"
    cat scripts/cnpg/import-legacy-db.sh
} | kubectl -n "$NS" run "$CLUSTER-import-$(date +%s)" --rm -i --quiet \
    --restart=Never --image="$IMAGE" --command -- bash -s
