#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

# Google Cloud Project ID
PROJECT_ID="your-gcp-project-id"

# Google Cloud Region
REGION="us-central1"

# Name for the Artifact Registry repository
REPOSITORY="student-ai-assistant-repo"

# Name for the Docker image
IMAGE_NAME="student-ai-assistant"

# Name for the Cloud Run service
SERVICE_NAME="student-ai-assistant-service"

# ------------------------------------------------------------------------------
# DO NOT EDIT BELOW THIS LINE
# ------------------------------------------------------------------------------

# Construct the full image tag
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:latest"

# 1. Enable necessary Google Cloud services
echo "Enabling Google Cloud services..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --project=${PROJECT_ID}

# 2. Create an Artifact Registry repository (if it doesn't exist)
echo "Creating Artifact Registry repository..."
gcloud artifacts repositories create ${REPOSITORY} \
  --repository-format=docker \
  --location=${REGION} \
  --description="Repository for Student AI Assistant" \
  --project=${PROJECT_ID} || echo "Repository ${REPOSITORY} already exists."

# 3. Build the Docker image using Google Cloud Build
echo "Building Docker image with Cloud Build..."
gcloud builds submit --tag ${IMAGE_TAG} --project=${PROJECT_ID}

# 4. Deploy the image to Google Cloud Run
echo "Deploying to Cloud Run..."

# Prepare environment variables from .env file
ENV_VARS_STRING=""
if [ -f ".env" ]; then
  echo "Found .env file, preparing environment variables..."
  # Read .env file, ignore comments and empty lines, and format for gcloud
  ENV_VARS_STRING=$(grep -v '^#' .env | grep -v '^

echo "✅ Deployment complete!"
echo "Service URL: $(gcloud run services describe ${SERVICE_NAME} --platform=managed --region=${REGION} --format='value(status.url)' --project=${PROJECT_ID})"
 | sed 's/^/--set-env-vars=/' | paste -sd "," -)
else
  echo "Warning: .env file not found. Using default environment variables."
fi

# Base gcloud deploy command
GCLOUD_COMMAND="gcloud run deploy ${SERVICE_NAME} \
  --image=${IMAGE_TAG} \
  --platform=managed \
  --region=${REGION} \
  --allow-unauthenticated \
  --port=8501 \
  --min-instances=0 \
  --set-env-vars=\"STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false\" \
  --set-env-vars=\"STREAMLIT_SERVER_ENABLE_CORS=false\" \
  --project=${PROJECT_ID}"

# Add environment variables if they exist
if [ -n "$ENV_VARS_STRING" ]; then
  GCLOUD_COMMAND="${GCLOUD_COMMAND} --update-env-vars=${ENV_VARS_STRING}"
fi

# Execute the command
eval ${GCLOUD_COMMAND}

echo "✅ Deployment complete!"
echo "Service URL: $(gcloud run services describe ${SERVICE_NAME} --platform=managed --region=${REGION} --format='value(status.url)' --project=${PROJECT_ID})"
