# Shared helpers for the CloudNativePG scripts (sourced, not run).

# Load KEY=VALUE lines from $ENV_FILE (default .env) literally: no shell
# expansion, so "$" in passwords is safe; surrounding quotes are stripped;
# variables already set in the environment win.
load_env_file() {
    local file=${ENV_FILE:-.env} line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        key=${BASH_REMATCH[1]}
        value=${BASH_REMATCH[2]}
        value=${value%%[[:space:]]#*}
        if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
            value=${BASH_REMATCH[1]}
        fi
        [ -n "${!key+x}" ] || export "$key=$value"
    done < "$file"
}

# Render a manifest, substituting only GAMIFICATIONENGINE_* variables
# (manifests can contain shell variables meant for containers).
render_manifest() {
    envsubst "$(env | grep -o '^GAMIFICATIONENGINE_[A-Za-z0-9_]*' | sed 's/^/$/' | tr '\n' ' ')" < "$1"
}

# psql on a cluster's current primary, as the postgres superuser over the
# local socket: cluster_psql <namespace> <cluster> <database> <sql>
cluster_psql() {
    local ns=$1 cluster=$2 db=$3 sql=$4 primary
    primary=$(kubectl -n "$ns" get cluster "$cluster" -o jsonpath='{.status.currentPrimary}')
    [ -n "$primary" ] || { echo "No primary for cluster $cluster" >&2; return 1; }
    kubectl -n "$ns" exec "$primary" -c postgres -- \
        psql -X -q -At -v ON_ERROR_STOP=1 -d "$db" -c "$sql"
}
