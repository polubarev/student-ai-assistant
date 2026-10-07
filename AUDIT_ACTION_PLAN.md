# Reliability and Security Audit: Action Plan

**Deployment update (2026-10-05):** The hardened revision is live at 100%
traffic. Private temporary files, bounded uploads/processing, Argon2id credentials,
CORS/XSRF, restricted runtime permissions, session affinity, and cached static PDF
export are deployed and verified. The Linux test suite, live text/summary/PDF
workflow, cross-user checks, and a synthetic speech-transcription check passed.
Legacy IAM grant removal was deferred by the user after an automatic-review rejection.
See the current evidence and remaining work in [SECURITY_REVIEW.md](SECURITY_REVIEW.md).

## Scope and verification status

This review covers the Streamlit application, its Cloud Run deployment script, the
upload and download paths, authentication, and PDF export. Findings below are based
on the repository as of 2026-09-29. They describe risks supported by the code;
production frequency and impact have not been measured. A read-only Cloud Run
inspection was attempted, but `gcloud` could not refresh its authentication token
because TLS certificate verification failed. No production configuration was changed.

The reported Chrome download failure prompted a local change: summary PDF/TXT and
transcript downloads now use browser-side `data:` links instead of Streamlit's
instance-local media endpoint (`app.py`, `utils/downloads.py`). The regression check
`py -m unittest tests.test_downloads -v` passes. A deployed Chrome download has not
yet been verified. The change also means the file bytes are sent in the page's
WebSocket payload, so unusually large exports should be measured against
Streamlit's message-size limit.

## Findings and actions

### 1. Shared temporary-file directory across user sessions — Critical

**Evidence.** `app.py:626-629` names the directory using the process ID and
`id(st.session_state)`. Streamlit exposes `st.session_state` through a module-level
proxy, so that Python object ID identifies the proxy, not the individual user
session. Local uploads use the original basename (`app.py:1017-1019`), and extracted
audio always uses `audio.wav` (`app.py:1137-1140`). The GCS upload prefix also uses
`id(st.session_state)` (`app.py:1044`), although its final object key includes a UUID.

**Impact.** Two users on one instance can overwrite each other's local files.
Transcription may read another user's audio. `reset_workflow()` clears session state
but does not remove temporary files, so disk memory and private data remain until
the instance stops.

**Action.** Generate a cryptographically random directory ID once per session and
store it as a value *inside* session state. Use that value for local paths and GCS
prefixes. Give each input and derived file a unique filename. Remove only the
current session's directory on explicit reset; add a bounded age-based cleanup for
abandoned sessions. Never delete a shared parent directory during a user request.

**Acceptance.** Two concurrent sessions uploading files with the same name have
different paths and retain their own bytes. Reset removes only the caller's files.
An abandoned-session cleanup test leaves active files untouched.

### 2. Password hashes are committed and use plain SHA-256 — Critical

**Evidence.** `.streamlit/secrets.toml` is tracked by Git and contains two password
hash entries; the values are intentionally omitted here. `utils/auth.py:9-11`
compares unsalted SHA-256 digests. `.dockerignore` does not exclude the secrets file,
so the build copies it into the container image. The app also has no visible
password-attempt throttling.

**Impact.** Anyone who obtains the repository history or image can run offline
password guesses against the hashes. Removing the current file from Git alone does
not remove its earlier copies from history.

**Action.** Arrange a password reset for the affected accounts. Move the credential
store to a managed secret or identity provider and stop copying it into images.
Replace SHA-256 with Argon2id (or another password-specific, salted algorithm) and
use its verifier. Add rate limiting at the authentication boundary. Add
`.streamlit/secrets.toml` to `.gitignore` and `.dockerignore`, then remove it from
the Git index. Assess repository and image access before deciding whether history
rewriting is necessary; coordinate any rewrite with all collaborators.

**Acceptance.** No active password hash is present in the tracked tree or new
image. The new authentication flow accepts valid credentials, rejects invalid
ones, and limits repeated attempts. Old passwords no longer work.

### 3. Large-upload allowance exceeds Cloud Run memory — High

**Evidence.** `services/storage_service.py:136` allows signed uploads up to 10 GiB.
`services/storage_service.py:46` then downloads the object to a local file, while
`deploy.sh:14` defaults to a 1 GiB Cloud Run instance. Cloud Run's writable
container filesystem consumes instance memory.

**Impact.** A successfully uploaded file can terminate the instance during the
download or FFmpeg processing. One request can disrupt other users on that
instance. Old temporary files increase the chance of failure.

**Action.** Set an explicit application limit derived from measured peak memory for
download, FFmpeg, Chromium, and concurrent requests. Read GCS object size metadata
and reject oversized objects *before* downloading. Apply the same effective limit
to signed POST policies and signed PUT uploads; a PUT URL needs server-side size
validation before processing. If the product truly needs multi-GiB inputs, move
processing to a separate job with appropriately sized storage and memory instead
of raising the UI limit alone.

**Acceptance.** An oversized object is rejected before any local copy is created.
A file at the supported limit completes processing under the configured memory
limit with expected concurrency. Repeated runs do not accumulate old files.

### 4. Streamlit CORS and XSRF protections are disabled — High

**Evidence.** `Dockerfile:57-58` and `deploy.sh:284` set both protections to
`false`; `deploy.sh:273` permits unauthenticated network access to Cloud Run and
relies on the application's own password screen.

**Impact.** Streamlit's origin and request-forgery protections are absent. The
exact exploitability depends on browser behavior and the deployed hostname, which
have not been tested here.

**Action.** Configure the public server address and allowed origin(s), then
re-enable CORS and XSRF protection. Set one strong `server.cookieSecret` across
replicas through Secret Manager. Test login, local upload, direct GCS upload,
WebSocket reconnection, and downloads on the real hostname before rollout.

**Acceptance.** Both protections are enabled in the deployed revision. Approved
same-origin flows work; cross-origin WebSocket and forged upload requests fail.

### 5. Streamlit requests can reach different Cloud Run instances — High

**Evidence.** `deploy.sh` has no `--session-affinity` setting and does not constrain
the service to one instance. Streamlit keeps session and uploaded-media state on
the serving process. The browser-side download change bypasses this dependency
for result files, but `st.file_uploader` still uses Streamlit's upload path.

**Impact.** With more than one instance, an upload or reconnect may reach an
instance that lacks the user's state. Session affinity reduces this failure mode
but Cloud Run only provides best-effort affinity, including across instance loss.

**Action.** Enable Cloud Run session affinity as a near-term mitigation and test
with at least two instances. Keep generated results in external storage if they
must survive instance replacement. Document the expected behavior when a session
is interrupted; avoid treating affinity as durable storage.

**Acceptance.** Multi-instance upload and download checks pass repeatedly under
normal scaling. An instance-replacement exercise either restores results from
external storage or gives the user a clear recovery path.

### 6. PDF is regenerated on every Streamlit rerun — Medium / performance

**Evidence.** `app.py:1275-1278` calls `build_summary_pdf_bytes()` whenever a
summary is present. That function launches Chromium (`app.py:171-196`). Streamlit
reruns the script after ordinary widget interactions, including sidebar changes.

**Impact.** Repeated Chromium launches add latency, CPU and memory use, and Cloud
Run cost. They can make an otherwise ready download temporarily unavailable if a
later launch fails.

**Action.** Cache PDF bytes per session under a hash of the summary text, or
generate on explicit user demand. Invalidate the cache when the summary changes or
the workflow resets. Track generation duration and failure count to verify the
benefit.

**Acceptance.** Repeated reruns with unchanged summary launch Chromium at most
once. A changed summary produces a fresh PDF, and a failed generation can be
retried without discarding the summary.

## Recommended sequence

1. **Protect user data first:** isolate temporary files; add cleanup and a
   two-session collision test. This is independent of cloud access.
2. **Remediate credentials:** coordinate password rotation, deploy the new
   verifier/secret source, remove hashes from future Git trees and images, then
   assess history exposure. Avoid a deployment window where users are locked out.
3. **Set a realistic file limit:** reject oversized GCS objects before download,
   align signed-upload limits, and run a memory-bound processing test.
4. **Harden Cloud Run networking:** configure hostname, shared cookie secret,
   CORS, XSRF, and session affinity in one staged revision; exercise login and
   every upload mode on that revision.
5. **Optimize PDF export:** cache or defer Chromium work and confirm Chrome saves
   `summary.pdf` and `summary.txt` from the deployed app.

Keep changes 1-3 in separately reviewable commits. For the cloud configuration
change, record the previous revision and settings so it can be rolled back if
login or uploads fail. None of the repository-level checks above substitutes for
the staged browser and concurrency checks.

## Primary references

- [Streamlit client/server architecture and multiple replicas](https://docs.streamlit.io/develop/concepts/architecture/architecture)
- [Streamlit remote-deployment troubleshooting](https://docs.streamlit.io/knowledge-base/deploy/remote-start)
- [Streamlit configuration options](https://docs.streamlit.io/develop/api-reference/configuration/config.toml)
- [Cloud Run session affinity](https://docs.cloud.google.com/run/docs/configuring/session-affinity)
- [Cloud Run container filesystem and memory](https://docs.cloud.google.com/run/docs/container-contract)
- [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
