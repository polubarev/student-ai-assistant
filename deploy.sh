#!/usr/bin/env bash
# Deploys to Cloud Run using Podman + Artifact Registry + Secret Manager.
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

ASSEMBLYAI_SECRET_NAME="${ASSEMBLYAI_SECRET_NAME:-assemblyai-api-key}"
OPENROUTER_SECRET_NAME="${OPENROUTER_SECRET_NAME:-openrouter-api-key}"
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
require_cmd podman

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
if [[ "$(uname -s)" == "Darwin" ]]; then
  podman machine start >/dev/null 2>&1 || true
fi

echo "Authenticating Podman to Artifact Registry..."
TOKEN="$(gcloud auth print-access-token)"
printf '%s' "${TOKEN}" | podman login "${REGION}-docker.pkg.dev" \
  -u oauth2accesstoken \
  --password-stdin >/dev/null

echo "Building container image with Podman..."
podman build --platform linux/amd64 -t "${IMAGE_TAG}" .

echo "Pushing container image..."
# Use explicit options to avoid blob reuse/signature issues observed with Podman.
podman push \
  --format docker \
  --compression-format gzip \
  --force-compression \
  --remove-signatures \
  "${IMAGE_TAG}"

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

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
if [[ -z "${RUNTIME_SERVICE_ACCOUNT}" ]]; then
  RUNTIME_SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
if [[ -z "${GCS_SIGNER_SERVICE_ACCOUNT_EMAIL}" ]]; then
  GCS_SIGNER_SERVICE_ACCOUNT_EMAIL="${RUNTIME_SERVICE_ACCOUNT}"
fi
echo "Using runtime service account: ${RUNTIME_SERVICE_ACCOUNT}"
echo "Granting Secret Manager access to Cloud Run runtime service account..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null

# Needed for generating signed GCS upload policies from Cloud Run without static key files.
echo "Granting runtime service account token-creator on itself (for signed upload URLs)..."
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SERVICE_ACCOUNT}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="${PROJECT_ID}" \
  --quiet >/dev/null

if [[ -n "${GCS_SOURCE_BUCKET}" ]]; then
  if [[ "${GCS_SOURCE_BUCKET}" == gs://* ]]; then
    GCS_BUCKET_URI="${GCS_SOURCE_BUCKET}"
  else
    GCS_BUCKET_URI="gs://${GCS_SOURCE_BUCKET}"
  fi
  if [[ -z "${GCS_UPLOAD_BUCKET}" ]]; then
    GCS_UPLOAD_BUCKET="${GCS_BUCKET_URI#gs://}"
  fi
  echo "Granting Cloud Run runtime service account GCS read access on ${GCS_BUCKET_URI}..."
  gcloud storage buckets add-iam-policy-binding "${GCS_BUCKET_URI}" \
    --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role="roles/storage.objectViewer" >/dev/null
fi

echo "Deploying Cloud Run service..."
SECRET_BINDINGS=()
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
  --service-account "${RUNTIME_SERVICE_ACCOUNT}"
  --project "${PROJECT_ID}"
)

ENV_BINDINGS=("STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false" "STREAMLIT_SERVER_ENABLE_CORS=false")
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
    "method": ["PUT", "POST", "GET", "HEAD", "OPTIONS"],
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
