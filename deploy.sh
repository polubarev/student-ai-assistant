#!/usr/bin/env bash
# Deploys to Cloud Run using Docker or Podman + Artifact Registry + Secret Manager.
set -euo pipefail

# ------------------------------------------------------------------------------
# Configuration (override via environment variables if needed)
# ------------------------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-ai-student-assistant-v2}"
REGION="${REGION:-us-east1}"
REPOSITORY="${REPOSITORY:-student-ai-assistant-repo}"
IMAGE_NAME="${IMAGE_NAME:-student-ai-assistant}"
SERVICE_NAME="${SERVICE_NAME:-student-ai-assistant-service}"
SERVICE_PORT="${SERVICE_PORT:-8501}"
SERVICE_MEMORY="${SERVICE_MEMORY:-1Gi}"
SERVICE_CPU="${SERVICE_CPU:-1}"
SERVICE_TIMEOUT="${SERVICE_TIMEOUT:-600}"
EXECUTION_ENVIRONMENT="${EXECUTION_ENVIRONMENT:-gen2}"
CONTAINER_ENGINE="${CONTAINER_ENGINE:-}"

ASSEMBLYAI_SECRET_NAME="${ASSEMBLYAI_SECRET_NAME:-assemblyai-api-key}"
OPENROUTER_SECRET_NAME="${OPENROUTER_SECRET_NAME:-openrouter-api-key}"
APP_USERS_SECRET_NAME="${APP_USERS_SECRET_NAME:-app-users}"
COOKIE_SECRET_NAME="${COOKIE_SECRET_NAME:-streamlit-cookie-secret}"
APP_USERS_PATH="${APP_USERS_PATH:-secrets/users.json}"
ENV_FILE="${ENV_FILE:-.env}"
GCS_SOURCE_BUCKET="${GCS_SOURCE_BUCKET:-}"
GCS_UPLOAD_BUCKET="${GCS_UPLOAD_BUCKET:-}"
GCS_UPLOAD_RETENTION_DAYS="${GCS_UPLOAD_RETENTION_DAYS:-7}"
APP_BASE_URL="${APP_BASE_URL:-}"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-}"
GCS_SIGNER_SERVICE_ACCOUNT_EMAIL="${GCS_SIGNER_SERVICE_ACCOUNT_EMAIL:-}"

IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:latest"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "❌ Required command not found: ${cmd}"
    exit 1
  fi
}

read_env_value() {
  local key="$1"
  local line
  [[ -f "${ENV_FILE}" ]] || return 1
  line="$(grep -E "^[[:space:]]*${key}=" "${ENV_FILE}" | tail -n1 || true)"
  [[ -n "${line}" ]] || return 1
  line="${line#*=}"
  line="${line%$'\r'}"
  # Trim optional single or double quotes around the full value.
  if [[ "${line}" =~ ^\".*\"$ ]]; then
    line="${line:1:${#line}-2}"
  elif [[ "${line}" =~ ^\'.*\'$ ]]; then
    line="${line:1:${#line}-2}"
  fi
  printf '%s' "${line}"
}

extract_origin() {
  local url="$1"
  if [[ "${url}" =~ ^https?://[^/]+ ]]; then
    printf '%s' "${BASH_REMATCH[0]}"
  else
    printf '%s' "${url}"
  fi
}

upsert_secret() {
  local secret_name="$1"
  local secret_value="$2"
  if [[ -z "${secret_value}" ]]; then
    echo "⚠️  Secret value missing for ${secret_name}. Skipping secret update."
    return
  fi

  if gcloud secrets describe "${secret_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    printf '%s' "${secret_value}" | gcloud secrets versions add "${secret_name}" \
      --data-file=- \
      --project="${PROJECT_ID}" >/dev/null
    echo "Updated secret version: ${secret_name}"
  else
    printf '%s' "${secret_value}" | gcloud secrets create "${secret_name}" \
      --data-file=- \
      --replication-policy=automatic \
      --project="${PROJECT_ID}" >/dev/null
    echo "Created secret: ${secret_name}"
  fi
}

echo "Starting deploy with project=${PROJECT_ID}, region=${REGION}, service=${SERVICE_NAME}"
echo "Cloud Run settings: memory=${SERVICE_MEMORY}, cpu=${SERVICE_CPU}, timeout=${SERVICE_TIMEOUT}s, env=${EXECUTION_ENVIRONMENT}"

# Fill optional deploy config from .env when not provided as shell vars.
if [[ -z "${GCS_SOURCE_BUCKET}" ]]; then
  GCS_SOURCE_BUCKET="$(read_env_value GCS_SOURCE_BUCKET || true)"
fi
if [[ -z "${GCS_UPLOAD_BUCKET}" ]]; then
  GCS_UPLOAD_BUCKET="$(read_env_value GCS_UPLOAD_BUCKET || true)"
fi
if [[ -z "${APP_BASE_URL}" ]]; then
  APP_BASE_URL="$(read_env_value APP_BASE_URL || true)"
fi
if [[ -z "${RUNTIME_SERVICE_ACCOUNT}" ]]; then
  RUNTIME_SERVICE_ACCOUNT="$(read_env_value RUNTIME_SERVICE_ACCOUNT || true)"
fi
if [[ -z "${GCS_SIGNER_SERVICE_ACCOUNT_EMAIL}" ]]; then
  GCS_SIGNER_SERVICE_ACCOUNT_EMAIL="$(read_env_value GCS_SIGNER_SERVICE_ACCOUNT_EMAIL || true)"
fi

require_cmd gcloud
if [[ -z "${CONTAINER_ENGINE}" ]]; then
  if command -v docker >/dev/null 2>&1; then
    CONTAINER_ENGINE=docker
  else
    CONTAINER_ENGINE=podman
  fi
fi
if [[ "${CONTAINER_ENGINE}" != docker && "${CONTAINER_ENGINE}" != podman ]]; then
  echo "CONTAINER_ENGINE must be docker or podman."
  exit 1
fi
require_cmd "${CONTAINER_ENGINE}"
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
  PYTHON_CMD=(python3)
elif command -v py >/dev/null 2>&1 && py -3 -c 'import sys; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
  PYTHON_CMD=(py -3)
else
  echo "Python 3.11 or newer is required."
  exit 1
fi
# Git Bash must not convert the Cloud Run secret mount into a Windows path.
export MSYS2_ARG_CONV_EXCL="${MSYS2_ARG_CONV_EXCL:+${MSYS2_ARG_CONV_EXCL};}/secrets/"

if ! "${CONTAINER_ENGINE}" info >/dev/null 2>&1; then
  echo "Start the ${CONTAINER_ENGINE} engine before deploying."
  exit 1
fi
if [[ "${RUNTIME_SERVICE_ACCOUNT}" == *-compute@developer.gserviceaccount.com ]]; then
  echo "Use a dedicated runtime service account, not the default Compute Engine identity."
  exit 1
fi

# Validate a custom public origin before changing cloud resources.
if [[ -n "${APP_BASE_URL}" ]]; then
  "${PYTHON_CMD[@]}" - "${APP_BASE_URL}" <<'PY'
from urllib.parse import urlparse
import sys
url = urlparse(sys.argv[1])
if (url.scheme != "https" or not url.hostname or url.username or url.password
        or url.query or url.fragment or url.path not in ("", "/") or url.port not in (None, 443)):
    raise SystemExit("APP_BASE_URL must be an HTTPS origin on port 443")
PY
fi

# Validate credentials before building or changing cloud resources.
if [[ -f "${APP_USERS_PATH}" ]]; then
  "${PYTHON_CMD[@]}" - "${APP_USERS_PATH}" <<'PY'
import json, sys
from pathlib import Path
users = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(users, dict) or not users or any(
    not isinstance(k, str) or not isinstance(v, str) or not v.startswith("$argon2id$")
    for k, v in users.items()
):
    raise SystemExit("Reset all login passwords with scripts.reset_passwords before deploying.")
PY
elif ! gcloud secrets describe "${APP_USERS_SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Missing login credentials. Run python -m scripts.reset_passwords first."
  exit 1
fi

CURRENT_ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
CURRENT_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
echo "Using gcloud account: ${CURRENT_ACCOUNT:-<none>}"
echo "gcloud default project: ${CURRENT_PROJECT:-<none>}"

if [[ -z "${CURRENT_ACCOUNT}" ]]; then
  echo "❌ You are not logged in to gcloud. Run: gcloud auth login --update-adc"
  exit 1
fi

if [[ "${CURRENT_PROJECT}" != "${PROJECT_ID}" ]]; then
  echo "Setting active project to ${PROJECT_ID}"
  gcloud config set project "${PROJECT_ID}" >/dev/null
fi

echo "Enabling required Google Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  --project="${PROJECT_ID}"

echo "Ensuring Artifact Registry repository exists..."
if ! gcloud artifacts repositories describe "${REPOSITORY}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Repository for Student AI Assistant" \
    --project="${PROJECT_ID}"
else
  echo "Repository ${REPOSITORY} already exists."
fi

# Podman VM is required on macOS.
if [[ "${CONTAINER_ENGINE}" == podman && "$(uname -s)" == "Darwin" ]]; then
  podman machine start >/dev/null 2>&1 || true
fi

echo "Authenticating ${CONTAINER_ENGINE} to Artifact Registry..."
TOKEN="$(gcloud auth print-access-token)"
printf '%s' "${TOKEN}" | "${CONTAINER_ENGINE}" login "${REGION}-docker.pkg.dev" \
  -u oauth2accesstoken \
  --password-stdin >/dev/null

echo "Building container image with ${CONTAINER_ENGINE}..."
"${CONTAINER_ENGINE}" build --platform linux/amd64 -t "${IMAGE_TAG}" .

echo "Pushing container image..."
# Use explicit options to avoid blob reuse/signature issues observed with Podman.
if [[ "${CONTAINER_ENGINE}" == podman ]]; then
  podman push \
  --format docker \
  --compression-format gzip \
  --force-compression \
  --remove-signatures \
    "${IMAGE_TAG}"
else
  docker push "${IMAGE_TAG}"
fi

# Load API keys from environment first, then fallback to .env.
ASSEMBLYAI_API_KEY="${ASSEMBLYAI_API_KEY:-}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
if [[ -z "${ASSEMBLYAI_API_KEY}" ]]; then
  ASSEMBLYAI_API_KEY="$(read_env_value ASSEMBLYAI_API_KEY || true)"
fi
if [[ -z "${OPENROUTER_API_KEY}" ]]; then
  OPENROUTER_API_KEY="${OPENAI_API_KEY:-}"
fi
if [[ -z "${OPENROUTER_API_KEY}" ]]; then
  OPENROUTER_API_KEY="$(read_env_value OPENROUTER_API_KEY || true)"
fi
if [[ -z "${OPENROUTER_API_KEY}" ]]; then
  OPENROUTER_API_KEY="$(read_env_value OPENAI_API_KEY || true)"
fi

echo "Upserting secrets (if values are available)..."
upsert_secret "${ASSEMBLYAI_SECRET_NAME}" "${ASSEMBLYAI_API_KEY}"
upsert_secret "${OPENROUTER_SECRET_NAME}" "${OPENROUTER_API_KEY}"

if [[ -f "${APP_USERS_PATH}" ]]; then
  if gcloud secrets describe "${APP_USERS_SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets versions add "${APP_USERS_SECRET_NAME}" --data-file="${APP_USERS_PATH}" --project="${PROJECT_ID}" >/dev/null
  else
    gcloud secrets create "${APP_USERS_SECRET_NAME}" --data-file="${APP_USERS_PATH}" --replication-policy=automatic --project="${PROJECT_ID}" >/dev/null
  fi
fi
if ! gcloud secrets describe "${COOKIE_SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  upsert_secret "${COOKIE_SECRET_NAME}" "$("${PYTHON_CMD[@]}" -c 'import secrets; print(secrets.token_hex(48))')"
fi

if [[ -z "${RUNTIME_SERVICE_ACCOUNT}" ]]; then
  RUNTIME_SERVICE_ACCOUNT="student-ai-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
fi
if [[ "${RUNTIME_SERVICE_ACCOUNT}" == *-compute@developer.gserviceaccount.com ]]; then
  echo "Use a dedicated runtime service account, not the default Compute Engine identity."
  exit 1
fi
if ! gcloud iam service-accounts describe "${RUNTIME_SERVICE_ACCOUNT}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SERVICE_ACCOUNT%%@*}" \
    --display-name="Student AI runtime" --project="${PROJECT_ID}" >/dev/null
fi
GCS_SIGNER_SERVICE_ACCOUNT_EMAIL="${RUNTIME_SERVICE_ACCOUNT}"
for secret_name in "${ASSEMBLYAI_SECRET_NAME}" "${OPENROUTER_SECRET_NAME}" "${APP_USERS_SECRET_NAME}" "${COOKIE_SECRET_NAME}"; do
  if gcloud secrets describe "${secret_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "${secret_name}" \
      --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
      --role="roles/secretmanager.secretAccessor" --project="${PROJECT_ID}" --quiet >/dev/null
  fi
done

GCS_UPLOAD_BUCKET="${GCS_UPLOAD_BUCKET:-${GCS_SOURCE_BUCKET#gs://}}"
GCS_UPLOAD_BUCKET="${GCS_UPLOAD_BUCKET#gs://}"
if [[ -n "${GCS_UPLOAD_BUCKET}" ]]; then
  gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SERVICE_ACCOUNT}" \
    --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" --role="roles/iam.serviceAccountTokenCreator" \
    --project="${PROJECT_ID}" --quiet >/dev/null
  for role in roles/storage.objectCreator roles/storage.objectViewer; do
    gcloud storage buckets add-iam-policy-binding "gs://${GCS_UPLOAD_BUCKET}" \
      --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" --role="${role}" \
      --condition="title=app_uploads,expression=resource.name.startsWith('projects/_/buckets/${GCS_UPLOAD_BUCKET}/objects/uploads/'),description=Application uploads only" >/dev/null
  done
fi

echo "Deploying Cloud Run service..."
SECRET_BINDINGS=("/secrets/app-users/users.json=${APP_USERS_SECRET_NAME}:latest" "STREAMLIT_SERVER_COOKIE_SECRET=${COOKIE_SECRET_NAME}:latest")
if gcloud secrets describe "${ASSEMBLYAI_SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  SECRET_BINDINGS+=("ASSEMBLYAI_API_KEY=${ASSEMBLYAI_SECRET_NAME}:latest")
fi
if gcloud secrets describe "${OPENROUTER_SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  SECRET_BINDINGS+=("OPENROUTER_API_KEY=${OPENROUTER_SECRET_NAME}:latest")
fi

DEPLOY_CMD=(
  gcloud run deploy "${SERVICE_NAME}"
  --image "${IMAGE_TAG}"
  --platform managed
  --region "${REGION}"
  --allow-unauthenticated
  --port "${SERVICE_PORT}"
  --min-instances 0
  --max-instances 1
  --concurrency 4
  --session-affinity
  --memory "${SERVICE_MEMORY}"
  --cpu "${SERVICE_CPU}"
  --timeout "${SERVICE_TIMEOUT}"
  --execution-environment "${EXECUTION_ENVIRONMENT}"
  --service-account "${RUNTIME_SERVICE_ACCOUNT}"
  --project "${PROJECT_ID}"
)

ENV_BINDINGS=("STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=true" "STREAMLIT_SERVER_ENABLE_CORS=true" "APP_USERS_FILE=/secrets/app-users/users.json")
if [[ -n "${GCS_UPLOAD_BUCKET}" ]]; then
  ENV_BINDINGS+=("GCS_UPLOAD_BUCKET=${GCS_UPLOAD_BUCKET}")
fi
if [[ -n "${APP_BASE_URL}" ]]; then
  ENV_BINDINGS+=("APP_BASE_URL=${APP_BASE_URL}")
fi
if [[ -n "${GCS_SIGNER_SERVICE_ACCOUNT_EMAIL}" ]]; then
  ENV_BINDINGS+=("GCS_SIGNER_SERVICE_ACCOUNT_EMAIL=${GCS_SIGNER_SERVICE_ACCOUNT_EMAIL}")
fi
DEPLOY_CMD+=(--set-env-vars "$(IFS=,; echo "${ENV_BINDINGS[*]}")")

if [[ ${#SECRET_BINDINGS[@]} -gt 0 ]]; then
  DEPLOY_CMD+=(--update-secrets "$(IFS=,; echo "${SECRET_BINDINGS[*]}")")
fi

"${DEPLOY_CMD[@]}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format='value(status.url)')"

if [[ -z "${APP_BASE_URL}" ]]; then
  echo "Setting APP_BASE_URL to deployed service URL..."
  gcloud run services update "${SERVICE_NAME}" \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --update-env-vars "APP_BASE_URL=${SERVICE_URL}" >/dev/null
  APP_BASE_URL="${SERVICE_URL}"
fi

APP_HOST="$("${PYTHON_CMD[@]}" - "${APP_BASE_URL}" <<'PY'
from urllib.parse import urlparse
import sys
url = urlparse(sys.argv[1])
if url.scheme != "https" or not url.hostname:
    raise SystemExit("APP_BASE_URL must be a valid HTTPS URL")
print(url.hostname)
PY
)"
gcloud run services update "${SERVICE_NAME}" --region="${REGION}" --project="${PROJECT_ID}" \
  --update-env-vars "STREAMLIT_BROWSER_SERVER_ADDRESS=${APP_HOST},STREAMLIT_BROWSER_SERVER_PORT=443" >/dev/null

echo "Removing legacy OPENAI_API_KEY secret binding if present..."
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --remove-secrets "OPENAI_API_KEY" >/dev/null || true

# Retain prior secret versions so an operator can investigate or roll back a revision.

if [[ -n "${GCS_UPLOAD_BUCKET}" && -n "${APP_BASE_URL}" ]]; then
  if [[ "${GCS_UPLOAD_BUCKET}" == gs://* ]]; then
    GCS_UPLOAD_BUCKET_URI="${GCS_UPLOAD_BUCKET}"
  else
    GCS_UPLOAD_BUCKET_URI="gs://${GCS_UPLOAD_BUCKET}"
  fi

  APP_ORIGIN="$(extract_origin "${APP_BASE_URL}")"
  SERVICE_ORIGIN="$(extract_origin "${SERVICE_URL}")"
  CORS_ORIGINS=("${APP_ORIGIN}")
  if [[ -n "${SERVICE_ORIGIN}" && "${SERVICE_ORIGIN}" != "${APP_ORIGIN}" ]]; then
    CORS_ORIGINS+=("${SERVICE_ORIGIN}")
  fi
  ORIGINS_JSON="$(printf '"%s",' "${CORS_ORIGINS[@]}")"
  ORIGINS_JSON="[${ORIGINS_JSON%,}]"

  echo "Configuring CORS on ${GCS_UPLOAD_BUCKET_URI} for origin(s) ${CORS_ORIGINS[*]}..."
  CORS_FILE="$(mktemp)"
  cat > "${CORS_FILE}" <<EOF
[
  {
    "origin": ${ORIGINS_JSON},
    "method": ["POST"],
    "responseHeader": ["Content-Type", "x-goog-resumable"],
    "maxAgeSeconds": 3600
  }
]
EOF
  gcloud storage buckets update "${GCS_UPLOAD_BUCKET_URI}" --cors-file="${CORS_FILE}" >/dev/null
  rm -f "${CORS_FILE}"

  if [[ "${GCS_UPLOAD_RETENTION_DAYS}" =~ ^[0-9]+$ ]] && (( GCS_UPLOAD_RETENTION_DAYS > 0 )); then
    echo "Configuring lifecycle on ${GCS_UPLOAD_BUCKET_URI}: delete uploads/ objects older than ${GCS_UPLOAD_RETENTION_DAYS} days..."
    LIFECYCLE_FILE="$(mktemp)"
    cat > "${LIFECYCLE_FILE}" <<EOF
{
  "rule": [
    {
      "action": { "type": "Delete" },
      "condition": {
        "age": ${GCS_UPLOAD_RETENTION_DAYS},
        "matchesPrefix": ["uploads/"]
      }
    }
  ]
}
EOF
    gcloud storage buckets update "${GCS_UPLOAD_BUCKET_URI}" --lifecycle-file="${LIFECYCLE_FILE}" >/dev/null
    rm -f "${LIFECYCLE_FILE}"
  else
    echo "Skipping lifecycle configuration because GCS_UPLOAD_RETENTION_DAYS=${GCS_UPLOAD_RETENTION_DAYS}."
  fi
fi

MIN_SCALE="$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format='value(spec.template.metadata.annotations."autoscaling.knative.dev/minScale")')"

echo "✅ Deployment complete."
echo "Service URL: ${SERVICE_URL}"
echo "minScale: ${MIN_SCALE:-0}"
