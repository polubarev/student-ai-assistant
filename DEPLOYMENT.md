# GCP Deployment Runbook (Cloud Run + Podman)

This runbook documents the deployment path that worked and the errors to avoid.

## Final Known-Good Setup

- Project: `ai-student-assistant-v2`
- Region: `us-east1`
- Registry: Artifact Registry (`${REGION}-docker.pkg.dev`)
- Runtime: Cloud Run (`min-instances=0`)
- Secrets: Google Secret Manager (`assemblyai-api-key`, `openrouter-api-key`)
- Image build/push: Podman (local)
- PDF export: Playwright + Chromium (installed in image build)

## Why This Path

- Avoids `gcloud builds submit` IAM/policy blockers.
- Avoids plaintext API keys in deploy commands.
- Keeps idle cost low (`min-instances=0`).

## One-Time Prerequisites

1. Authenticate:
```bash
gcloud auth login --update-adc
```

2. Set active project:
```bash
gcloud config set project ai-student-assistant-v2
```

3. Ensure Podman works:
```bash
podman machine start
```

4. Give Cloud Run runtime service account access to lecture files in GCS:
```bash
PROJECT_NUMBER="$(gcloud projects describe ai-student-assistant-v2 --format='value(projectNumber)')"
gcloud storage buckets add-iam-policy-binding gs://MY_LECTURES_BUCKET \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectViewer"
```

## Recommended Deploy Command

From repo root:
```bash
bash deploy.sh
```

If you want the script to grant bucket read access automatically:
```bash
GCS_SOURCE_BUCKET=MY_LECTURES_BUCKET bash deploy.sh
```

For UI-only large uploads (no manual `gs://` for users), also set:
```bash
GCS_SOURCE_BUCKET=MY_LECTURES_BUCKET \
GCS_UPLOAD_BUCKET=MY_LECTURES_BUCKET \
bash deploy.sh
```
`deploy.sh` will set `APP_BASE_URL` to the service URL automatically.

The script now does all of this:
- enables required APIs (`run`, `artifactregistry`, `secretmanager`)
- ensures repository exists
- authenticates Podman to Artifact Registry
- builds + pushes image
- installs Chromium in the image for Markdown->PDF export
- creates/updates secret versions from env values
- grants secret access to Cloud Run runtime service account
- grants `roles/iam.serviceAccountTokenCreator` on runtime service account (required for signed browser upload forms)
- configures bucket CORS for your app origin (required when upload uses signed `PUT` fallback)
- deploys Cloud Run with secret bindings and `min-instances=0`

## Cloud Run Resource Knobs

`deploy.sh` supports these optional env vars:

- `SERVICE_MEMORY` (default: `1Gi`)
- `SERVICE_CPU` (default: `1`)
- `SERVICE_TIMEOUT` in seconds (default: `600`)
- `EXECUTION_ENVIRONMENT` (`gen1` or `gen2`, default: `gen2`)

Example:
```bash
SERVICE_MEMORY=2Gi SERVICE_CPU=2 SERVICE_TIMEOUT=900 bash deploy.sh
```

This is useful if PDF export + long summarization requests need more headroom.

## API Key Handling

The script reads API keys from:
1. process environment (`ASSEMBLYAI_API_KEY`, `OPENROUTER_API_KEY`)
2. fallback `.env` file in repo root

It stores keys in Secret Manager and deploys with:
- `ASSEMBLYAI_API_KEY=assemblyai-api-key:latest`
- `OPENROUTER_API_KEY=openrouter-api-key:latest`

## Rotate/Update Secrets

Add new secret versions:
```bash
echo -n "NEW_ASSEMBLYAI_KEY" | gcloud secrets versions add assemblyai-api-key --data-file=-
echo -n "NEW_OPENROUTER_KEY" | gcloud secrets versions add openrouter-api-key --data-file=-
```

Roll Cloud Run to latest secret versions:
```bash
gcloud run services update student-ai-assistant-service \
  --region us-east1 \
  --update-secrets ASSEMBLYAI_API_KEY=assemblyai-api-key:latest,OPENROUTER_API_KEY=openrouter-api-key:latest
```

## Verify Deployment

Service URL:
```bash
gcloud run services describe student-ai-assistant-service \
  --region us-east1 \
  --format='value(status.url)'
```

Scale-to-zero:
```bash
gcloud run services describe student-ai-assistant-service \
  --region us-east1 \
  --format='value(spec.template.metadata.annotations.autoscaling\.knative\.dev/minScale)'
```

Expected minScale: `0`.

## Large File Workflow (Recommended)

### UI-only flow (recommended for end users)

1. In app, choose **Large file upload** source.
2. Click **Prepare secure browser upload**.
3. Choose file and click **Upload to Cloud Storage**.
4. Browser redirects back; file loads automatically.

## Errors We Hit And How To Avoid Them

1. `gcloud builds submit ... PERMISSION_DENIED`
- Cause: Cloud Build permissions/org policy.
- Fix: do not use Cloud Build in this repo path. Use Podman local build/push.

2. `podman push ... Requesting bearer token ... 403 Forbidden`
- Cause: project policy/registry constraints (seen in old recovered project).
- Fix:
  - use clean/new project (`ai-student-assistant-v2`)
  - authenticate Podman with `gcloud auth print-access-token`
  - push with safer options (already in `deploy.sh`)

3. Cloud Run deploy returns `The service has encountered an internal error`
- Cause in our case was project/environment instability on restored project.
- Fix: deploy in new project/region.

4. `gcloud logging read ... textPayload` empty
- Cause: errors may be in `protoPayload` or condition events, not `textPayload`.
- Fix: inspect service/revision conditions and full log payload fields.

5. `Blob object has no attribute generate_signed_post_policy_v4`
- Cause: older `google-cloud-storage` runtime API surface.
- Fix: app now falls back to signed `PUT` upload automatically. Redeploy latest code.

6. PDF download fails with browser-not-found / launch errors
- Cause: runtime can’t find Chromium binaries used by Playwright.
- Fix:
  - Use latest Dockerfile (sets shared `PLAYWRIGHT_BROWSERS_PATH` and installs Chromium in build).
  - Rebuild and redeploy with `bash deploy.sh`.

## Security Notes

- Do not pass API keys directly in `gcloud run deploy --set-env-vars ...`.
- Do not paste API keys in terminal logs/screenshots.
- If any key appears in output/history, rotate it immediately.
