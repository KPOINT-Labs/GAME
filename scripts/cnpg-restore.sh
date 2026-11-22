#!/usr/bin/env bash
# Restore the database from its backups into a NEW CloudNativePG cluster:
# the latest state, or a point in time (PITR). The main cluster and its
# backups are not touched. See docs/source/operations.rst
# ("Backups and restore").
#
# Usage: scripts/cnpg-restore.sh [--name NAME] [--target-time TIME]
#                                [--instances N] [--storage-class CLASS]
#                                [--timeout DURATION] [--yes]
#   --name           new cluster name (default
#                    GAMIFICATIONENGINE_CNPG_RESTORE_CLUSTER_NAME). Its IAM
#                    role must trust it (Terraform cnpg_restore_cluster_names).
#   --target-time    RFC 3339 with a zone, e.g. 2026-11-22T08:00:00Z;
#                    restores to the last transaction before it. Default:
#                    the latest state in the archive.
#   --instances      instances of the new cluster (default
#                    GAMIFICATIONENGINE_CNPG_RESTORE_INSTANCES)
#   --storage-class  default GAMIFICATIONENGINE_CNPG_STORAGE_CLASS (volumes
#                    kept); use <class>-delete for throwaway clusters
#   --timeout        how long to wait for the cluster (default 60m)
#   --yes            do not ask for confirmation
#
# Reads .env (or ENV_FILE) like deploy-kubernetes.sh. Needs kubectl, envsubst.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/cnpg/lib.sh
source scripts/cnpg/lib.sh
load_env_file

NAME=${GAMIFICATIONENGINE_CNPG_RESTORE_CLUSTER_NAME:-}
TARGET_TIME=""
INSTANCES=${GAMIFICATIONENGINE_CNPG_RESTORE_INSTANCES:-1}
STORAGE_CLASS=${GAMIFICATIONENGINE_CNPG_STORAGE_CLASS:-}
TIMEOUT=60m
ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --name) NAME=$2; shift 2 ;;
        --target-time) TARGET_TIME=$2; shift 2 ;;
        --instances) INSTANCES=$2; shift 2 ;;
        --storage-class) STORAGE_CLASS=$2; shift 2 ;;
        --timeout) TIMEOUT=$2; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

NS=${GAMIFICATIONENGINE_NAMESPACE:?set GAMIFICATIONENGINE_NAMESPACE}
MAIN=${GAMIFICATIONENGINE_CNPG_CLUSTER_NAME:?set GAMIFICATIONENGINE_CNPG_CLUSTER_NAME}
: "${NAME:?set --name or GAMIFICATIONENGINE_CNPG_RESTORE_CLUSTER_NAME}"
: "${STORAGE_CLASS:?set --storage-class or GAMIFICATIONENGINE_CNPG_STORAGE_CLASS}"
for v in GAMIFICATIONENGINE_CNPG_BACKUP_OBJECTSTORE GAMIFICATIONENGINE_CNPG_BACKUP_SERVER_NAME \
         GAMIFICATIONENGINE_AWS_DB_BACKUP_ROLE_ARN GAMIFICATIONENGINE_DB_CREDENTIALS_SECRET; do
    [ -n "${!v:-}" ] || { echo "Error: set $v (see .env.sample)." >&2; exit 1; }
done

if [ "$NAME" = "$MAIN" ]; then
    echo "Error: restore into a NEW cluster, not $MAIN." >&2
    exit 1
fi
if [[ ! "$INSTANCES" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: --instances must be a positive integer." >&2
    exit 1
fi
if [ -n "$TARGET_TIME" ] && \
   [[ ! "$TARGET_TIME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}[T\ ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})$ ]]; then
    echo "Error: --target-time must be RFC 3339 with a zone, e.g. 2026-11-22T08:00:00Z." >&2
    exit 1
fi
for tool in kubectl envsubst; do
    command -v "$tool" >/dev/null || { echo "Error: $tool is required." >&2; exit 1; }
done
if ! kubectl get crd clusters.postgresql.cnpg.io >/dev/null 2>&1; then
    echo "Error: the CloudNativePG operator is not installed." >&2
    exit 1
fi
if kubectl -n "$NS" get cluster "$NAME" >/dev/null 2>&1; then
    echo "Error: cluster $NAME already exists in $NS. Delete it or choose another --name." >&2
    exit 1
fi

echo "Restore backups of $GAMIFICATIONENGINE_CNPG_BACKUP_SERVER_NAME (object store $GAMIFICATIONENGINE_CNPG_BACKUP_OBJECTSTORE)"
echo "  into NEW cluster $NAME in $NS: $INSTANCES instance(s), storage class $STORAGE_CLASS"
echo "  target: ${TARGET_TIME:-latest state in the archive}"
if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "Continue? [y/N] " answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 1
fi

export GAMIFICATIONENGINE_CNPG_RESTORE_CLUSTER_NAME=$NAME
export GAMIFICATIONENGINE_CNPG_RESTORE_INSTANCES=$INSTANCES
export GAMIFICATIONENGINE_CNPG_RESTORE_STORAGE_CLASS=$STORAGE_CLASS
manifest=$(render_manifest kubernetes/cnpg/postgres-cluster-restore.yaml)
if [ -n "$TARGET_TIME" ]; then
    target="      recoveryTarget:\n        targetTime: \"$TARGET_TIME\""
else
    target=""
fi
manifest=$(printf '%s\n' "$manifest" | awk -v t="$target" '
    /# RECOVERY_TARGET/ { if (t != "") { gsub(/\\n/, "\n", t); print t }; next }
    { print }')

printf '%s\n' "$manifest" | kubectl -n "$NS" apply -f -
echo "Waiting up to $TIMEOUT for $NAME to become ready (restoring)..."
if ! kubectl -n "$NS" wait --for=condition=Ready "cluster/$NAME" --timeout="$TIMEOUT"; then
    echo "Error: $NAME did not become ready. Check: kubectl -n $NS describe cluster $NAME;" \
         "kubectl -n $NS logs -l cnpg.io/cluster=$NAME --all-containers" >&2
    exit 1
fi
echo "Restored: cluster $NAME is ready (service $NAME-rw)."
