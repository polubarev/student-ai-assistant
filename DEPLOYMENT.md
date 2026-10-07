# GCP Deployment Runbook (Cloud Run + Docker or Podman)

This runbook documents the deployment path that worked and the errors to avoid.

## Deployment configuration (production validation required)

- Project: `ai-student-assistant-v2`
- Region: `us-east1`
- Registry: Artifact Registry (`${REGION}-docker.pkg.dev`)
- Runtime: Cloud Run (`min-instances=0`, `max-instances=1`, concurrency 4, session affinity)
- Secrets: Google Secret Manager (`assemblyai-api-key`, `openrouter-api-key`, `app-users`, `streamlit-cookie-secret`)
- Runtime identity: dedicated `student-ai-runtime` service account with resource-scoped grants
- Image build/push: Docker or Podman (local; automatically detected)
- PDF export: static WeasyPrint worker with Pango, no external resources, and CPU/memory/time limits

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

3. Start the installed container engine. For Docker Desktop:
```bash
docker desktop start
```
Alternatively, use Podman. `CONTAINER_ENGINE=podman` selects it explicitly.

4. Reset all login passwords locally before the first hardened deployment:
```bash
python -m scripts.reset_passwords
```
The ignored `secrets/users.json` is uploaded to the `app-users` Secret Manager
secret and mounted at `/secrets/app-users/users.json`. The app reads it through
`APP_USERS_FILE`. Subsequent deployments can reuse the managed secret if the
local file is absent. Never use the committed SHA-256 credentials with this version.

5. Use Python 3.11 or newer (`python3`, or `py -3` on Windows). The script uses
Python to generate random secret material and does not require OpenSSL. A
shared, randomly generated Streamlit cookie secret is created once in Secret
Manager and bound as `STREAMLIT_SERVER_COOKIE_SECRET`.

On this Windows workspace, use Git Bash from `C:/Program Files/Git/bin/bash.exe`.
The script supports the Windows Python launcher and preserves the Cloud Run
secret mount path when invoked through Git Bash. If Google Cloud TLS verification
fails despite a configured account, use the machine's trusted CA bundle through
`CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE`; keep certificate verification enabled.

## Recommended Deploy Command

From repo root:
```bash
bash deploy.sh
```

For direct browser uploads, set the dedicated upload bucket:
```bash
GCS_UPLOAD_BUCKET=MY_UPLOAD_BUCKET bash deploy.sh
```
`deploy.sh` sets `APP_BASE_URL` to the service URL automatically unless a custom
HTTPS URL is supplied. It grants the runtime identity object creation and viewing
only for the bucket's `uploads/` prefix. The app additionally permits reads only
of the exact object issued to the current authenticated session. General lecture
bucket browsing and arbitrary `gs://` imports are no longer supported.

The script builds from the hashed `requirements.lock`, creates the dedicated
runtime service account, grants access on the four individual secrets, mounts the
user credential file, enables Streamlit CORS/XSRF, and enables session affinity.
GCS CORS permits POST from the application origin; signed uploads have a 10 GiB
policy limit and no unrestricted PUT fallback. Prior secret versions are retained
for investigation and controlled rollback.

The multipart form sends an explicit `Content-Type` field before the file, as
required to preserve the media type in [GCS HTML uploads](https://docs.cloud.google.com/storage/docs/xml-api/post-object-forms).
Media type is used for routing; FFprobe validates media duration and format before
extraction or transcription. The app signs a generation-pinned GCS GET URL after
checking the session's exact object grant. FFmpeg streams cloud media into mono
32 kbps MP3 (up to four hours, bounded to 128 MiB), keeping the original large
file out of Cloud Run's RAM-backed filesystem. Signed URLs are never logged.

The script does not remove grants from the old Compute Engine identity, which may
be shared by other services. After switching revisions, an operator must review
and remove unnecessary old grants with that impact in mind. An explicit default
Compute Engine `RUNTIME_SERVICE_ACCOUNT` is rejected; remove it from `.env` or
select a dedicated identity.

Only one instance is configured because authentication/job limits and Streamlit
sessions are stored in process memory. Affinity remains best effort, and quotas
reset after replacement. Before increasing replicas, move quotas and durable
ownership records to shared storage. Reconnects retain the Streamlit session for
30 minutes; after a page refresh, authenticated per-user local checkpoints allow
recovery on the same instance for six hours. Logout and start-over remove the
checkpoint. A session lost during replacement needs a
new upload; knowledge of an old object path does not grant access to it.

PDF export uses a static, bounded worker. The Chromium sandbox check failed in
the container; the release therefore avoids launching a browser. External resource
loading is denied, Markdown HTML is escaped, and worker CPU/memory/time limits are
applied. WeasyPrint is pinned to an audited release.

## Cloud Build rollout used for this release

The active revision is `student-ai-assistant-service-00050-cej` on 100% traffic.
Docker Desktop could not start locally. A dedicated `student-ai-builder` identity
built the credential-free allowlist bundle in Cloud Build and ran all 36 Linux
tests plus a real, network-disabled PDF check.

`scripts/build_cloud.py` submits the remote build. `scripts/deploy_cloud.py`
prepares scoped permissions and managed secrets, stages a no-traffic revision,
creates a production-credential revision from the validated image digest, and
promotes only after live checks attest to that same digest. Local rollout state
and fixtures are stored in ignored directories. On this workspace, the pipeline
is run with the `.venv` Python and trusted CA bundle documented above.

`scripts/verify_live.py` uses disposable accounts and synthetic lecture content.
It verifies the signed upload policy, GCS upload/read, cross-user/session behavior,
summarization, PDF downloads, and foreign-origin WebSocket rejection. Temporary
credential versions are disabled, their runtime grant removed, and old revision
tags cleared after promotion. `scripts/verify_speech.py` additionally checked the
release transcription client with synthetic audio and deleted its temporary job
and audio object.

The retired default account's broad grants were not removed: automatic approval
review blocked revocation, and the user chose to retain them for a separate
workload review.
The production service uses the restricted dedicated identity. See
[SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the recorded remaining work.

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
- Fix: rebuild from the locked dependencies and use the storage client's signed POST policy API. Signing failures stop the upload; there is no PUT fallback.

6. PDF export fails
- Check the static worker's Pango dependencies and resource limits.
- Rebuild from the locked dependencies; do not enable external URL fetching.
- TXT downloads remain available if PDF rendering fails.

## Security Notes

- Do not pass API keys directly in `gcloud run deploy --set-env-vars ...`.
- Do not paste API keys in terminal logs/screenshots.
- If any key appears in output/history, rotate it immediately.
