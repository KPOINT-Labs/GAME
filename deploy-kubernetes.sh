#!/bin/bash


cd "$(dirname "$0")"


if [ -f ".env" ]; then
    export $(cat .env | sed 's/#.*//g' | xargs)
fi

NAMESPACE=${GAMIFICATIONENGINE_NAMESPACE}
# Where the application database credentials Secret comes from: "env" (from
# .env, applied by --cnpg/--postgres) or "aws" (AWS Secrets Manager through
# External Secrets, applied by --aws).
DB_SECRETS_SOURCE=${GAMIFICATIONENGINE_DB_SECRETS_SOURCE:-env}
DB_SECRET=${GAMIFICATIONENGINE_DB_CREDENTIALS_SECRET}

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

show_help() {
    echo "Usage: $0 [OPTION]"
    echo "Apply Kubernetes YAML files for specific components."
    echo ""
    echo "Options:"
    echo "  --postgres  Apply the single-instance PostgreSQL deployment (no failover; non-production)."
    echo "  --cnpg      Apply the highly available PostgreSQL cluster (CloudNativePG;"
    echo "              the operator must be installed first)."
    echo "  --aws       Apply the AWS pieces: encrypted gp3 StorageClass and the"
    echo "              database credentials from AWS Secrets Manager (External"
    echo "              Secrets Operator must be installed; see infra/aws/db)."
    echo "  --cnpg-monitoring"
    echo "              Apply the PodMonitor that scrapes the database instances"
    echo "              (needs the Prometheus Operator)."
    echo "  --api       Apply only the API deployment and its dependencies."
    echo "  --verbose   Display verbose output."
    echo "  --help      Display this help message."
    echo ""
    echo "Examples:"
    echo "  $0 --postgres        # Apply only the PostgreSQL deployment."
    echo "  $0 --api             # Apply only the API deployment."
    echo "  $0 --api --verbose   # Apply only the API deployment with verbose output."
}

apply_yaml() {
    FILE=$1
    VERBOSE=$2
    TEMP_FILE="temp-$(basename $FILE)"

    # Substitute only GAMIFICATIONENGINE_* variables: manifests also contain
    # shell variables meant for the container (e.g. $POSTGRES_USER in probes).
    envsubst "$(env | grep -o '^GAMIFICATIONENGINE_[A-Za-z0-9_]*' | sed 's/^/$/' | tr '\n' ' ')" < $FILE > $TEMP_FILE

    if [ "$VERBOSE" = "--verbose" ]; then
        echo -e "${YELLOW}Executing command: kubectl --namespace $NAMESPACE apply -f $TEMP_FILE${NC}"
    fi
    kubectl --namespace $NAMESPACE apply -f $TEMP_FILE
    local status=$?

    if [ $status -eq 0 ]; then
        echo -e "${GREEN}[+] Success: The resource $TEMP_FILE has been successfully applied to the namespace $NAMESPACE.${NC}"
    else
        echo -e "${RED}[-] Error: Failed to apply the resource $TEMP_FILE to the namespace $NAMESPACE.${NC}"
    fi

    rm $TEMP_FILE
    return $status
}

for arg in "$@"; do
    if [ "$arg" = "--help" ]; then
        show_help
        exit 0
    fi
done

if [ -z "$DB_SECRET" ] && [[ "$1" =~ ^--(cnpg|postgres|api|aws)$ ]]; then
    echo -e "${RED}[-] Error: set GAMIFICATIONENGINE_DB_CREDENTIALS_SECRET in .env (see .env.sample).${NC}"
    exit 1
fi

DB_SECRET_FILES=()
if [ "$DB_SECRETS_SOURCE" = "env" ]; then
    DB_SECRET_FILES=("kubernetes/secrets/db-app-credentials-secret.yaml")
elif [ "$DB_SECRETS_SOURCE" != "aws" ]; then
    echo -e "${RED}[-] Error: GAMIFICATIONENGINE_DB_SECRETS_SOURCE must be env or aws.${NC}"
    exit 1
fi

POSTGRES_FILES=(
    "${DB_SECRET_FILES[@]}"
    "kubernetes/volumen/postgres-data-persistentvolumeclaim.yaml"
    "kubernetes/services/postgres-service.yaml"
    "kubernetes/deployments/postgres-deployment.yaml"
)

CNPG_FILES=(
    "${DB_SECRET_FILES[@]}"
    "kubernetes/cnpg/postgres-cluster.yaml"
)

CNPG_MONITORING_FILES=(
    "kubernetes/cnpg/postgres-podmonitor.yaml"
)

AWS_FILES=(
    "kubernetes/aws/storageclass-gp3-encrypted.yaml"
    "kubernetes/aws/secrets-reader-serviceaccount.yaml"
    "kubernetes/aws/secretstore-aws.yaml"
    "kubernetes/aws/externalsecret-db-app.yaml"
)

API_FILES=(
    "${DB_SECRET_FILES[@]}"
    "kubernetes/configmaps/env-prod-configmap.yaml"
    "kubernetes/services/gamificationengine-service.yaml"
    "kubernetes/deployments/gamificationengine-deployment.yaml"
)

INGRESS=(
    "kubernetes/ingresses/ingress.yaml"

)

if [ "$1" = "--postgres" ]; then
    FILES=("${POSTGRES_FILES[@]}")
elif [ "$1" = "--cnpg" ]; then
    if ! kubectl get crd clusters.postgresql.cnpg.io >/dev/null 2>&1; then
        echo -e "${RED}[-] Error: the CloudNativePG operator is not installed (no clusters.postgresql.cnpg.io CRD). See docs/source/operations.rst.${NC}"
        exit 1
    fi
    FILES=("${CNPG_FILES[@]}")
elif [ "$1" = "--cnpg-monitoring" ]; then
    if ! kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1; then
        echo -e "${RED}[-] Error: the Prometheus Operator is not installed (no podmonitors.monitoring.coreos.com CRD). Use the scrape config in docs/source/observability.rst instead.${NC}"
        exit 1
    fi
    FILES=("${CNPG_MONITORING_FILES[@]}")
elif [ "$1" = "--aws" ]; then
    if [ "$DB_SECRETS_SOURCE" != "aws" ]; then
        echo -e "${RED}[-] Error: --aws needs GAMIFICATIONENGINE_DB_SECRETS_SOURCE=aws.${NC}"
        exit 1
    fi
    if ! kubectl get crd externalsecrets.external-secrets.io >/dev/null 2>&1; then
        echo -e "${RED}[-] Error: the External Secrets Operator is not installed (no externalsecrets.external-secrets.io CRD). See infra/aws/db/README.md.${NC}"
        exit 1
    fi
    FILES=("${AWS_FILES[@]}")
elif [ "$1" = "--api" ]; then
    FILES=("${API_FILES[@]}")
elif [ "$1" = "--ingress" ]; then
    FILES=("${INGRESS[@]}")
else
    echo "Invalid option or no option provided. Use --help for usage information."
    exit 1
fi

# With credentials from AWS, the Secret must have been synced (--aws) before
# the database or the API can start.
if [ "$DB_SECRETS_SOURCE" = "aws" ] && [[ "$1" =~ ^--(cnpg|postgres|api)$ ]]; then
    if ! kubectl --namespace "$NAMESPACE" get secret "$DB_SECRET" >/dev/null 2>&1; then
        echo -e "${RED}[-] Error: Secret $DB_SECRET does not exist yet. Run $0 --aws and check: kubectl -n $NAMESPACE get externalsecret $DB_SECRET${NC}"
        exit 1
    fi
fi

FAILED=0
for FILE in "${FILES[@]}"; do
    apply_yaml $FILE $2 || FAILED=1 # $2 can be --verbose
done

if [ "$FAILED" -ne 0 ]; then
    echo -e "${RED}[-] Error: one or more resources failed to apply.${NC}"
    exit 1
fi
echo -e "${BLUE}[*] Info: Process completed.${NC}"
