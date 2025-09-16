#!/bin/bash
# Exit on error, unset vars, and fail pipelines if any part fails
set -euo pipefail

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

# Google Cloud Project ID
PROJECT_ID="ai-student-assistant-472319"

# Google Cloud Region
REGION="us-central1"

# Name for the Artifact Registry repository
REPOSITORY="student-ai-assistant-repo"

# Name for the Docker image
IMAGE_NAME="student-ai-assistant"

# Name for the Cloud Run service
SERVICE_NAME="student-ai-assistant-service"

# Cloud Run service port (your app's listening port)
SERVICE_PORT=8501

# ------------------------------------------------------------------------------
# DO NOT EDIT BELOW THIS LINE
# ------------------------------------------------------------------------------

# Construct the full image tag
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:latest"

# --- Preflight checks ----------------------------------------------------------

# 0) gcloud is authenticated and on the right project
CURRENT_ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
CURRENT_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
echo "Using gcloud account: ${CURRENT_ACCOUNT:-<none>}"
echo "gcloud default project: ${CURRENT_PROJECT:-<none>}"

if [[ -z "${CURRENT_ACCOUNT}" ]]; then
  echo "❌ You are not logged in to gcloud. Run: gcloud auth login --update-adc"
  exit 1
fi

if [[ "${CURRENT_PROJECT}" != "${PROJECT_ID}" ]]; then
  echo "ℹ️  Setting gcloud project to ${PROJECT_ID}"
  gcloud config set project "${PROJECT_ID}" >/dev/null
fi

# 1) Billing must be enabled on the project
#echo "Checking billing on project ${PROJECT_ID}..."
#if ! gcloud beta billing projects describe "${PROJECT_ID}" --format='value(billingEnabled)' | grep -qx true; then
#  echo "❌ Billing is NOT enabled for project ${PROJECT_ID}."
#  echo "   Link an OPEN billing account first, e.g.:"
#  echo "   gcloud beta billing accounts list"
#  echo "   gcloud beta billing projects link ${PROJECT_ID} --billing-account=XXXXXX-XXXXXX-XXXXXX"
#  exit 1
#fi
#echo "✅ Billing is enabled."

# 2) Enable necessary Google Cloud services
echo "Enabling Google Cloud services..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --project="${PROJECT_ID}"

# 3) Create an Artifact Registry repository (if it doesn't exist)
echo "Creating Artifact Registry repository (if needed)..."
if ! gcloud artifacts repositories describe "${REPOSITORY}" \
      --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Repository for Student AI Assistant" \
    --project="${PROJECT_ID}"
else
  echo "Repository ${REPOSITORY} already exists."
fi

# 4) Build the Docker image using Google Cloud Build
echo "Building Docker image with Cloud Build..."
gcloud builds submit --tag "${IMAGE_TAG}" --project="${PROJECT_ID}"

# 5) Deploy the image to Google Cloud Run
echo "Deploying to Cloud Run..."

# Prepare environment variables from .env file (optional)
ENV_VARS_FROM_FILE=""
if [[ -f ".env" ]]; then
  echo "Found .env file, preparing environment variables..."
  # Read .env, ignore comments/blank lines, then join lines with commas
  # Result looks like: KEY1=VAL1,KEY2=VAL2
  # NOTE: If a value contains commas/spaces, quote it in .env as KEY="value with, commas"
  ENV_VARS_FROM_FILE="$(grep -v '^[[:space:]]*#' .env | grep -v '^[[:space:]]*$' | paste -sd ',' -)"
else
  echo "Warning: .env file not found. Proceeding without extra env vars from file."
fi

# Always include these Streamlit envs
BASE_ENV_VARS="STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false,STREAMLIT_SERVER_ENABLE_CORS=false"

# Merge env vars (with comma only if both sides present)
if [[ -n "${ENV_VARS_FROM_FILE}" ]]; then
  ALL_ENV_VARS="${BASE_ENV_VARS},${ENV_VARS_FROM_FILE}"
else
  ALL_ENV_VARS="${BASE_ENV_VARS}"
fi

# Deploy
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE_TAG}" \
  --platform=managed \
  --region="${REGION}" \
  --allow-unauthenticated \
  --port="${SERVICE_PORT}" \
  --min-instances=0 \
  --set-env-vars "${ALL_ENV_VARS}" \
  --project="${PROJECT_ID}"

echo "✅ Deployment complete!"
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --platform=managed \
  --region="${REGION}" \
  --format='value(status.url)' \
  --project="${PROJECT_ID}")"
echo "Service URL: ${SERVICE_URL}"
