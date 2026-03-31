#!/usr/bin/env bash
# =============================================================================
# Bio AI Platform — Boltz-2 ACA Deployment
# Deploys API (Container App) + Worker (Event-triggered Job) to Azure
# =============================================================================
set -euo pipefail

required_vars=(
  RESOURCE_GROUP
  CONTAINERAPPS_ENV
  ACR_LOGIN_SERVER
  ACR_USERNAME
  ACR_PASSWORD
  DATABASE_URL
  SUPABASE_URL
  SUPABASE_ANON_KEY
  SUPABASE_JWT_SECRET
  AZURE_STORAGE_ACCOUNT_URL
  AZURE_STORAGE_ACCOUNT_NAME
  AZURE_STORAGE_ACCOUNT_KEY
  SERVICE_BUS_NAMESPACE
  SERVICE_BUS_QUEUE_NAME
  SERVICE_BUS_CONNECTION_STRING
)

for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "ERROR: ${var_name} is required." >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Configurable defaults
# ---------------------------------------------------------------------------

API_APP_NAME="${API_APP_NAME:-boltz2-api}"
WORKER_JOB_NAME="${WORKER_JOB_NAME:-boltz2-worker}"
API_IMAGE_TAG="${API_IMAGE_TAG:-latest}"
WORKER_IMAGE_TAG="${WORKER_IMAGE_TAG:-latest}"

API_IMAGE="${ACR_LOGIN_SERVER}/boltz2-api:${API_IMAGE_TAG}"
WORKER_IMAGE="${ACR_LOGIN_SERVER}/boltz2-worker:${WORKER_IMAGE_TAG}"

API_CPU="${API_CPU:-1.0}"
API_MEMORY="${API_MEMORY:-2.0Gi}"
API_MIN_REPLICAS="${API_MIN_REPLICAS:-1}"
API_MAX_REPLICAS="${API_MAX_REPLICAS:-3}"

WORKER_CPU="${WORKER_CPU:-8.0}"
WORKER_MEMORY="${WORKER_MEMORY:-32Gi}"
WORKER_REPLICA_TIMEOUT="${WORKER_REPLICA_TIMEOUT:-86400}"
WORKER_REPLICA_RETRY_LIMIT="${WORKER_REPLICA_RETRY_LIMIT:-0}"
WORKER_POLLING_INTERVAL="${WORKER_POLLING_INTERVAL:-15}"
WORKER_MAX_EXECUTIONS="${WORKER_MAX_EXECUTIONS:-10}"
WORKER_MIN_EXECUTIONS="${WORKER_MIN_EXECUTIONS:-0}"
WORKER_WORKLOAD_PROFILE="${WORKER_WORKLOAD_PROFILE:-ConsumptionA100}"

# ---------------------------------------------------------------------------
# Environment variables for containers
# ---------------------------------------------------------------------------

api_env_vars=(
  "APP_ENV=production"
  "BLOB_BACKEND=azure"
  "QUEUE_BACKEND=azure"
  "AZURE_INPUT_CONTAINER=boltz2-inputs"
  "AZURE_RESULTS_CONTAINER=boltz2-results"
  "SERVICE_BUS_QUEUE_NAME=${SERVICE_BUS_QUEUE_NAME}"
  "ACA_SUBSCRIPTION_ID=${ACA_SUBSCRIPTION_ID:-}"
  "ACA_RESOURCE_GROUP=${RESOURCE_GROUP}"
  "ACA_WORKER_JOB_NAME=${WORKER_JOB_NAME}"
  "MSA_SERVER_URL=${MSA_SERVER_URL:-https://api.colabfold.com}"
  "SUPABASE_URL=secretref:supurl"
  "SUPABASE_ANON_KEY=secretref:supanon"
  "SUPABASE_JWT_SECRET=secretref:supjwt"
  "DATABASE_URL=secretref:dburl"
  "AZURE_STORAGE_ACCOUNT_URL=secretref:sturl"
  "AZURE_STORAGE_ACCOUNT_NAME=secretref:stname"
  "AZURE_STORAGE_ACCOUNT_KEY=secretref:stkey"
  "SERVICE_BUS_CONNECTION_STRING=secretref:sbconn"
)

worker_env_vars=(
  "APP_ENV=production"
  "BLOB_BACKEND=azure"
  "QUEUE_BACKEND=azure"
  "AZURE_INPUT_CONTAINER=boltz2-inputs"
  "AZURE_RESULTS_CONTAINER=boltz2-results"
  "SERVICE_BUS_QUEUE_NAME=${SERVICE_BUS_QUEUE_NAME}"
  "BOLTZ2_BIN=boltz"
  "BOLTZ2_CACHE_DIR=/cache"
  "BOLTZ2_RUN_TIMEOUT_SECONDS=${BOLTZ2_RUN_TIMEOUT_SECONDS:-14400}"
  "BOLTZ2_DEVICES=${BOLTZ2_DEVICES:-1}"
  "MSA_SERVER_URL=${MSA_SERVER_URL:-https://api.colabfold.com}"
  "SUPABASE_URL=secretref:supurl"
  "SUPABASE_ANON_KEY=secretref:supanon"
  "SUPABASE_JWT_SECRET=secretref:supjwt"
  "DATABASE_URL=secretref:dburl"
  "AZURE_STORAGE_ACCOUNT_URL=secretref:sturl"
  "AZURE_STORAGE_ACCOUNT_NAME=secretref:stname"
  "AZURE_STORAGE_ACCOUNT_KEY=secretref:stkey"
  "SERVICE_BUS_CONNECTION_STRING=secretref:sbconn"
)

secrets_args=(
  "supurl=${SUPABASE_URL}"
  "supanon=${SUPABASE_ANON_KEY}"
  "supjwt=${SUPABASE_JWT_SECRET}"
  "dburl=${DATABASE_URL}"
  "sturl=${AZURE_STORAGE_ACCOUNT_URL}"
  "stname=${AZURE_STORAGE_ACCOUNT_NAME}"
  "stkey=${AZURE_STORAGE_ACCOUNT_KEY}"
  "sbconn=${SERVICE_BUS_CONNECTION_STRING}"
)

echo "============================================================"
echo "Boltz-2 ACA Deploy"
echo "  Resource Group:    ${RESOURCE_GROUP}"
echo "  Environment:       ${CONTAINERAPPS_ENV}"
echo "  API App:           ${API_APP_NAME} (${API_IMAGE})"
echo "  Worker Job:        ${WORKER_JOB_NAME} (${WORKER_IMAGE})"
echo "  Queue:             ${SERVICE_BUS_QUEUE_NAME}"
echo "============================================================"

# ---------------------------------------------------------------------------
# 1. Ensure Service Bus queue exists
# ---------------------------------------------------------------------------

if ! az servicebus queue show \
  -g "${RESOURCE_GROUP}" \
  --namespace-name "${SERVICE_BUS_NAMESPACE}" \
  -n "${SERVICE_BUS_QUEUE_NAME}" >/dev/null 2>&1; then
  echo "Creating Service Bus queue: ${SERVICE_BUS_QUEUE_NAME}..."
  az servicebus queue create \
    -g "${RESOURCE_GROUP}" \
    --namespace-name "${SERVICE_BUS_NAMESPACE}" \
    -n "${SERVICE_BUS_QUEUE_NAME}" >/dev/null
fi

# ---------------------------------------------------------------------------
# 2. Deploy API Container App
# ---------------------------------------------------------------------------

echo "Deploying API app..."
if az containerapp show -g "${RESOURCE_GROUP}" -n "${API_APP_NAME}" >/dev/null 2>&1; then
  az containerapp secret set \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --secrets "${secrets_args[@]}" >/dev/null

  az containerapp update \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --image "${API_IMAGE}" \
    --cpu "${API_CPU}" \
    --memory "${API_MEMORY}" \
    --min-replicas "${API_MIN_REPLICAS}" \
    --max-replicas "${API_MAX_REPLICAS}" \
    --replace-env-vars "${api_env_vars[@]}" >/dev/null
else
  az containerapp create \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --environment "${CONTAINERAPPS_ENV}" \
    --image "${API_IMAGE}" \
    --registry-server "${ACR_LOGIN_SERVER}" \
    --registry-username "${ACR_USERNAME}" \
    --registry-password "${ACR_PASSWORD}" \
    --cpu "${API_CPU}" \
    --memory "${API_MEMORY}" \
    --min-replicas "${API_MIN_REPLICAS}" \
    --max-replicas "${API_MAX_REPLICAS}" \
    --ingress external \
    --target-port 8001 \
    --transport auto \
    --revisions-mode single \
    --secrets "${secrets_args[@]}" \
    --env-vars "${api_env_vars[@]}" >/dev/null
fi

# ---------------------------------------------------------------------------
# 3. Deploy Worker Job (Event-triggered, GPU)
# ---------------------------------------------------------------------------

echo "Deploying Worker job..."
if az containerapp job show -g "${RESOURCE_GROUP}" -n "${WORKER_JOB_NAME}" >/dev/null 2>&1; then
  az containerapp job secret set \
    -g "${RESOURCE_GROUP}" \
    -n "${WORKER_JOB_NAME}" \
    --secrets "${secrets_args[@]}" >/dev/null

  az containerapp job update \
    -g "${RESOURCE_GROUP}" \
    -n "${WORKER_JOB_NAME}" \
    --image "${WORKER_IMAGE}" \
    --cpu "${WORKER_CPU}" \
    --memory "${WORKER_MEMORY}" \
    --workload-profile-name "${WORKER_WORKLOAD_PROFILE}" \
    --parallelism 1 \
    --replica-completion-count 1 \
    --replica-retry-limit "${WORKER_REPLICA_RETRY_LIMIT}" \
    --replica-timeout "${WORKER_REPLICA_TIMEOUT}" \
    --polling-interval "${WORKER_POLLING_INTERVAL}" \
    --min-executions "${WORKER_MIN_EXECUTIONS}" \
    --max-executions "${WORKER_MAX_EXECUTIONS}" \
    --scale-rule-name servicebus-queue \
    --scale-rule-type azure-servicebus \
    --scale-rule-metadata \
      "queueName=${SERVICE_BUS_QUEUE_NAME}" \
      "namespace=${SERVICE_BUS_NAMESPACE}" \
      "messageCount=1" \
    --scale-rule-auth "connection=sbconn" \
    --replace-env-vars "${worker_env_vars[@]}" >/dev/null
else
  az containerapp job create \
    -g "${RESOURCE_GROUP}" \
    -n "${WORKER_JOB_NAME}" \
    --environment "${CONTAINERAPPS_ENV}" \
    --trigger-type Event \
    --image "${WORKER_IMAGE}" \
    --registry-server "${ACR_LOGIN_SERVER}" \
    --registry-username "${ACR_USERNAME}" \
    --registry-password "${ACR_PASSWORD}" \
    --cpu "${WORKER_CPU}" \
    --memory "${WORKER_MEMORY}" \
    --workload-profile-name "${WORKER_WORKLOAD_PROFILE}" \
    --parallelism 1 \
    --replica-completion-count 1 \
    --replica-retry-limit "${WORKER_REPLICA_RETRY_LIMIT}" \
    --replica-timeout "${WORKER_REPLICA_TIMEOUT}" \
    --polling-interval "${WORKER_POLLING_INTERVAL}" \
    --min-executions "${WORKER_MIN_EXECUTIONS}" \
    --max-executions "${WORKER_MAX_EXECUTIONS}" \
    --scale-rule-name servicebus-queue \
    --scale-rule-type azure-servicebus \
    --scale-rule-metadata \
      "queueName=${SERVICE_BUS_QUEUE_NAME}" \
      "namespace=${SERVICE_BUS_NAMESPACE}" \
      "messageCount=1" \
    --scale-rule-auth "connection=sbconn" \
    --secrets "${secrets_args[@]}" \
    --env-vars "${worker_env_vars[@]}" >/dev/null
fi

# ---------------------------------------------------------------------------
# 4. Managed Identity for API → Worker log access
# ---------------------------------------------------------------------------

echo "Configuring Managed Identity..."
az containerapp identity assign \
  -g "${RESOURCE_GROUP}" \
  -n "${API_APP_NAME}" \
  --system-assigned >/dev/null

PRINCIPAL_ID="$(
  az containerapp identity show \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --query principalId \
    -o tsv
)"

WORKER_JOB_ID="$(
  az containerapp job show \
    -g "${RESOURCE_GROUP}" \
    -n "${WORKER_JOB_NAME}" \
    --query id \
    -o tsv
)"

az role assignment create \
  --assignee "${PRINCIPAL_ID}" \
  --role "Contributor" \
  --scope "${WORKER_JOB_ID}" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 5. Output
# ---------------------------------------------------------------------------

API_FQDN="$(
  az containerapp show \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --query properties.configuration.ingress.fqdn \
    -o tsv
)"

echo
echo "============================================================"
echo "Deploy complete!"
echo "  API URL:    https://${API_FQDN}"
echo "  Health:     https://${API_FQDN}/healthz"
echo "  Docs:       https://${API_FQDN}/docs"
echo "  MCP:        https://${API_FQDN}/mcp"
echo "  Worker tag: ${WORKER_IMAGE_TAG}"
echo "============================================================"
