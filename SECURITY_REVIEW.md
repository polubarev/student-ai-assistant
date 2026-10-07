# Security Review and Action Plan

## Lecture reliability update — 2026-10-07

Revision `student-ai-assistant-service-00055-qim` serves the verified lecture fixes.
Uploads allow 10 GiB and audio allows four hours. Cloud media streams into bounded
MP3 output; transcription jobs resume by provider ID and errors remain visible.
Authenticated checkpoints restore lectures after refresh on the current instance.
Instance replacement still requires a new upload.

All 52 tests passed in the production image, along with live upload, transcription,
recovery, and export checks. The supplied 74-minute-50-second MP3 reproduced the
previous one-hour rejection and successfully produced a 58,797-character transcript
using the deployed image. The test audio and transcript are excluded from Git.

## Deployed status — 2026-10-05

The hardened platform is deployed at
[the production service](https://student-ai-assistant-service-ikpjgp6wka-ue.a.run.app).
Revision `student-ai-assistant-service-00050-cej` receives 100% of traffic.
The old public revision tags and disposable validation accounts were removed.

The release uses image digest
`sha256:c037a1a7afbbb7419009fcfb3c67090f18a61c2a8431c023ba97498e3f8cd46a`,
verified by Cloud Build `7f3a873f-bdc0-473f-9c04-d56af6d45c46` before promotion.
Docker Desktop failed locally, so Cloud Build built and tested an explicit source
bundle that excludes credentials, environment files, Git history, and caches.

| Finding | Deployed fix and verification | Remaining work |
| --- | --- | --- |
| F1: arbitrary GCS reads | The app authorizes only the exact object issued to the authenticated session. Live GCS upload/read and a second-account forged import check passed. Bucket access is uniform and public access prevention is enforced. | Keep ownership checks when adding persistent file history. |
| F2: committed hashes | Argon2id credentials are mounted from Secret Manager; the managed records match the reset local file. Attempt limits and credential-change/session expiry checks are active. Credential files are excluded from Git and images. | Review historical repository/image exposure; coordinate any Git history rewrite. |
| F3: exhaustion | Bounded upload policies and downloads, pinned object generations, text/audio limits, timeouts, per-account quotas, one heavy job per process, and one Cloud Run instance are active. | Load-test near supported limits before expanding usage; configure billing alerts. |
| F4: CORS/XSRF | Both protections are active in the production revision. The public health/login checks passed; foreign-origin WebSockets receive HTTP 403. | Retest whenever the public hostname or proxy changes. |
| F5: unsafe HTML/PDF | Summary HTML is escaped. PDF uses a bounded static worker with all external resource loading denied. Live PDF generation and browser download passed. | Retain resource/time limits when changing PDF support. |
| F6: broad runtime IAM | The live service uses `student-ai-runtime`, individual secret grants, and conditional storage access under `uploads/`. | Legacy default-account grant removal was deferred by the user for a separate review; see below. |

The shared temporary-file directory is fixed with private random session paths,
unique filenames, reset/logout cleanup, and idle cleanup that excludes active jobs.
The deployment has session affinity, concurrency 4, and a maximum of one instance.
Process-local limits reset after replacement; they are not distributed quotas or a
hard billing cap. Reloading the browser requires signing in again.

### Verification

- 36 tests passed in the Linux image, including real FFprobe validation.
- The real PDF check passed inside the image with network access disabled.
- Live checks passed for two independent logins, bounded signed GCS upload,
  authorized reading, forged cross-user import rejection, separate session content,
  LLM summarization, PDF generation, and an actual browser download.
- Production health, invalid-login rejection, CORS/XSRF configuration, the dedicated
  identity, 100% traffic assignment, and old-tag removal were verified.
- A temporary job using the release image and runtime identity successfully
  transcribed synthetic speech through AssemblyAI. The job and its audio object
  were deleted after verification.
- `pip-audit` reported no known advisories in the 76 selected locked runtime
  packages. This does not certify OS packages or every possible application flaw.

The initial Chromium PDF check failed with "No usable sandbox". Export was changed
to a bounded static renderer. The dependency audit then
flagged WeasyPrint 69.0; the release uses 70.0 and a restrictive URLFetcher.
See the [upstream advisory](https://github.com/Kozea/WeasyPrint/security/advisories/GHSA-jf6q-chmf-3h3v).

### Legacy IAM cleanup deferred by the user

The retired identity `718655707914-compute@developer.gserviceaccount.com` still has
legacy project Editor/Secret Manager access, self-signing, and lecture-bucket read
grants. Automatic approval review rejected removal because other workload usage
was not fully verified and explicit approval to revoke these potentially shared
grants was missing. The user selected "Leave the old grants for a separate review".
No legacy grant was removed. The active application does not use this identity. Its original grants were snapshotted locally for review.

The temporary validation secret was disabled and its grant on the new runtime
identity removed. Production uses only the real credential secret and required
provider/cookie secrets. Remaining maintenance includes the legacy IAM decision,
billing alerts, near-limit load testing, log retention review, and historical
repository/image exposure review. Source changes remain uncommitted locally.

## Original review

**Review date:** 2026-09-29

**Scope:** Application source, authentication, file handling, dependency declarations, container build, and Cloud Run deployment script.
**Method:** Static review of this repository. No live penetration test, deployed-image inspection, or dependency vulnerability scan was performed. The local workspace had no active `gcloud` account, so production settings and IAM grants remain unverified.

## Executive summary

The application has three high-priority risks: it accepts arbitrary GCS object paths for server-side reading, commits password hashes into Git and the container image, and permits uploads large enough to exhaust storage or processing capacity. The deployment also disables Streamlit's CORS and XSRF protections, renders AI output as HTML, and grants broad access to the Cloud Run service identity. Address the high-priority items before expanding access to the platform.

Severity below reflects the code and configuration in this repository. Actual exposure depends on repository/image access, production IAM permissions, and the deployed revision.

## Findings

### F1 — Unrestricted GCS object reads (High)

**Evidence:** `app.py:690-770` downloads an object from a supplied `gs://` URI. `app.py:951-980` constructs that URI from query parameters, and `app.py:1089-1106` accepts one through a support field. `services/storage_service.py:37-57` uses the Cloud Run service account to fetch it. There is no check that the object belongs to the current user, is in the upload bucket, or was issued in that user's session.

**Impact:** A signed-in user who knows or obtains another object's path can have the server read anything its service account can read. An object path is not an authorization token. Broad service-account storage access increases the possible exposure.

**Action:** Stop accepting arbitrary bucket/key combinations from the browser. Store the issued object key server-side with a durable user identity and authorize each read against that record. Restrict the service identity to the intended bucket and prefix where possible. Remove the manual URI input from the ordinary user flow.

**Verification:** With two accounts, an upload owned by account A must be rejected when account B supplies its URI through either the query string or support field. A URI in another readable bucket must also be rejected.

### F2 — Committed login hashes and weak password hashing (High)

**Evidence:** `.streamlit/secrets.toml` is tracked by Git, has appeared in two commits, and contains two SHA-256-shaped password hashes. `utils/auth.py:9-11` compares an unsalted SHA-256 digest. `.dockerignore` does not exclude the secrets file, while `Dockerfile:40` runs `COPY . .`, placing the file in the image. This report intentionally omits usernames and hash values.

**Impact:** Anyone with access to the repository history or image can obtain the hashes and attempt offline guessing. Fast SHA-256 is unsuitable for passwords. Whether unauthorized people already have access to the repository or image was not established.

**Action:** Rotate both login passwords and any reused passwords. Replace the hashes with Argon2id (or another suitable slow, salted password hash). Load the credential store from a managed secret outside Git and the image. Add `.streamlit/secrets.toml` to both `.gitignore` and `.dockerignore`, stop tracking it, and rebuild the image. After rotation, coordinate any Git-history cleanup with repository owners; merely deleting the file in a new commit leaves earlier versions accessible.

**Verification:** No credential material appears in the built image or current tracked files; old passwords fail; new password records use per-user salted, slow hashes; repository access and history exposure have been assessed.

### F3 — Upload and processing exhaustion (High)

**Evidence:** `services/storage_service.py:136,163-166` allows signed POST uploads up to 10 GB. The signed PUT fallback at `services/storage_service.py:246-264` has no equivalent size condition. `app.py:712` downloads an object before checking its size; `app.py:680` reads a text file fully into memory. FFmpeg subprocesses in `services/audio_service.py:49,92` have no timeout. `deploy.sh:14` defaults the Cloud Run instance to 1 GiB of memory.

**Impact:** A signed-in user can cause storage charges, exhaust container disk or memory, tie up processing capacity, or incur transcription/LLM costs. The signed URL is a temporary bearer capability; anyone possessing it can use it while valid. Google Cloud describes signed URLs this way in its [documentation](https://docs.cloud.google.com/storage/docs/access-control/signed-urls).

**Action:** Define a realistic maximum upload size and enforce it in the signed policy, before GCS download, and before text decoding or media processing. Remove the unrestricted PUT fallback or give it an equivalent enforced limit. Add FFmpeg timeouts, concurrency controls, per-user quotas, and budget alerts. Reject oversized files before allocating local disk.

**Verification:** Oversized objects are rejected from metadata before download; malformed or long-running media jobs terminate within a fixed budget; repeated uploads and jobs cannot exceed a user's quota.

### F4 — CORS and XSRF defenses disabled (Medium)

**Evidence:** `Dockerfile:57-58` and `deploy.sh:284` set both `STREAMLIT_SERVER_ENABLE_CORS=false` and `STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false`.

**Impact:** This removes browser-origin and request protections. Streamlit states that disabling CORS accepts WebSocket connections from any origin; XSRF is a separate protection. The exact exploitability depends on the deployed version, domain, and authentication/session behavior. See [Streamlit configuration](https://docs.streamlit.io/develop/api-reference/configuration/config.toml).

**Action:** Restore both protections, configure the intended production origin, and test login and uploads behind the actual proxy/domain. Avoid leaving deployment environment variables that override safer image defaults.

**Verification:** The deployed revision reports both protections enabled, the intended origin works, and an unrelated origin cannot establish an application WebSocket or submit protected requests.

### F5 — Untrusted AI output rendered as HTML (Medium)

**Evidence:** `app.py:1271-1274` passes the AI-generated summary to `st.markdown(..., unsafe_allow_html=True)`. Uploaded content can influence that summary. Streamlit documents that this option renders HTML and advises caution in its [API reference](https://docs.streamlit.io/develop/api-reference/text/st.markdown).

**Impact:** Crafted source material or model output can change the page's displayed HTML and potentially mislead users. Script execution was not demonstrated and depends on Streamlit's deployed rendering and sanitization behavior.

**Action:** Render summaries with the default HTML escaping, or sanitize against a narrow allowlist if HTML formatting is required. Keep the PDF renderer's existing `html=False` behavior.

**Verification:** A summary containing HTML tags, styles, scripts, and deceptive links is displayed safely and does not alter the surrounding interface.

### F6 — Broad Cloud Run service identity permissions (Medium)

**Evidence:** `deploy.sh:222-240` defaults to the Compute Engine service account and grants it project-wide `roles/secretmanager.secretAccessor` plus self-signing authority. `deploy.sh:253-256` grants bucket-wide object viewing when a source bucket is configured.

**Impact:** A compromised application could access more secrets or objects than it needs, depending on the service account's other production roles. Google Cloud recommends a dedicated service identity with only required permissions in its [Cloud Run security guidance](https://docs.cloud.google.com/run/docs/securing/security).

**Action:** Create a dedicated runtime service account. Grant secret access on only the required secret resources and storage access on only the required bucket(s). Review whether self-signing is needed after the upload flow is revised. Remove broader inherited or project-level grants.

**Verification:** The production revision uses the dedicated identity; an IAM review shows only necessary grants; attempts to read unrelated secrets and buckets fail.

## Additional hardening gaps

- `utils/auth.py` shows no login throttling or lockout. Add rate limiting at the authentication edge and monitor failed attempts, especially while committed hashes are being rotated.
- `requirements.txt` uses minimum versions without a lockfile. The deployed versions and current vulnerability status are unknown. Generate a reproducible lockfile, run a dependency audit in CI, and rebuild regularly with updated base images and system packages.
- Uploaded filenames and GCS URIs are written to logs in `app.py:697-702,760-765`. Review log access and retention because names or paths may reveal user information. Production logs and retention settings were not inspected.

## Ordered action plan

### Phase 0 — Contain exposure before the next deployment

1. Restrict GCS reads to server-recorded, user-owned uploads. Until that is ready, disable the arbitrary URI entry points and avoid granting the runtime account read access to unrelated buckets.
2. Rotate the two login passwords; assess whether either was reused elsewhere. Move the credential store out of Git and the image. Rebuild and deploy after rotation.
3. Lower the maximum upload size to a capacity the service can safely process, reject large objects before download, and disable the unrestricted PUT fallback until it has an enforced limit.

### Phase 1 — Repair application and cloud controls

4. Replace SHA-256 password storage with Argon2id and add login throttling. Review whether a managed identity provider would fit the platform better than custom authentication.
5. Re-enable CORS and XSRF protection and validate the real production origin, login, and upload flow.
6. Escape or sanitize AI-generated summaries before rendering them as HTML.
7. Deploy with a dedicated service account and resource-scoped secret/storage permissions. Confirm the actual Cloud Run IAM policy and revision settings.

### Phase 2 — Prevent recurrence

8. Add authorization tests for cross-user GCS reads, upload-size boundary tests, and HTML rendering tests. Include a test that rejects alternate buckets and forged redirect parameters.
9. Pin and audit Python dependencies and the container base image in CI. Record deployed versions so advisories can be mapped to the running service.
10. Set per-user job quotas, processing timeouts, storage lifecycle rules, budget alerts, and log-retention limits. Verify them in a staging deployment with non-sensitive test data.

## Completion criteria and remaining checks

The review can be closed when all high and medium findings have fixes deployed, the verification cases above pass in staging and production, and the production service account, image contents, and dependency versions have been inspected. A live security test should then exercise authentication, cross-user file access, oversized uploads, cross-origin requests, and malicious summary content without using real user data.
