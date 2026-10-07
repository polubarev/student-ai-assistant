"""Stage a verified image, then promote it after authenticated live checks."""

import argparse
import json
import secrets
from pathlib import Path
from urllib.parse import urlparse

from utils.credentials import PASSWORD_HASHER, load_users
from scripts.build_cloud import ROOT, PROJECT, REGION, run


SERVICE = "student-ai-assistant-service"
RUNTIME = f"student-ai-runtime@{PROJECT}.iam.gserviceaccount.com"
STATE_PATH = ROOT / ".cache/rollout-state.json"


def save(state):
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def secret_from_file(name, path):
    existing = run("secrets", "describe", name, f"--project={PROJECT}", "--format=json", optional=True)
    if existing is None:
        run("secrets", "create", name, f"--project={PROJECT}", f"--data-file={path}", "--replication-policy=automatic")
    else:
        run("secrets", "versions", "add", name, f"--project={PROJECT}", f"--data-file={path}")
    return enabled_version(name)


def enabled_version(name):
    value = run("secrets", "versions", "list", name, f"--project={PROJECT}", "--filter=state=ENABLED",
                "--sort-by=~createTime", "--limit=1", "--format=value(name)")
    return str(int(value.rsplit("/", 1)[-1]))


def grant_validation_access(state):
    policy = json.loads(run("secrets", "get-iam-policy", "app-users-validation", f"--project={PROJECT}", "--format=json"))
    member = f"serviceAccount:{RUNTIME}"
    existing = any(binding.get("role") == "roles/secretmanager.secretAccessor"
                   and member in binding.get("members", []) and not binding.get("condition")
                   for binding in policy.get("bindings", []))
    if not existing:
        run("secrets", "add-iam-policy-binding", "app-users-validation", f"--project={PROJECT}",
            f"--member={member}", "--role=roles/secretmanager.secretAccessor")
        state["validation_access_created"] = True


def prepare(state):
    load_users()  # Validate the replacement records without printing them.
    identity = run("iam", "service-accounts", "describe", RUNTIME, f"--project={PROJECT}", "--format=json", optional=True)
    if identity is None:
        run("iam", "service-accounts", "create", "student-ai-runtime", f"--project={PROJECT}", "--display-name=Student AI runtime")
    state["users_version"] = secret_from_file("app-users", ROOT / "secrets/users.json")
    cookie = run("secrets", "describe", "streamlit-cookie-secret", f"--project={PROJECT}", "--format=json", optional=True)
    if cookie is None:
        path = ROOT / "secrets/rollout-cookie.tmp"
        try:
            path.write_text(secrets.token_urlsafe(64), encoding="ascii")
            secret_from_file("streamlit-cookie-secret", path)
        finally:
            path.unlink(missing_ok=True)
    state["cookie_version"] = enabled_version("streamlit-cookie-secret")
    state["assemblyai_version"] = enabled_version("assemblyai-api-key")
    state["openrouter_version"] = enabled_version("openrouter-api-key")
    accounts = {f"rollout_{letter}_{secrets.token_hex(4)}": secrets.token_urlsafe(32) for letter in ("a", "b")}
    login_path = ROOT / "secrets/validation-logins.json"
    login_path.write_text(json.dumps(accounts), encoding="utf-8")
    hashes_path = ROOT / "secrets/validation-users.json"
    hashes_path.write_text(json.dumps({name: PASSWORD_HASHER.hash(password) for name, password in accounts.items()}), encoding="utf-8")
    state["validation_version"] = secret_from_file("app-users-validation", hashes_path)
    for name in ("app-users", "app-users-validation", "streamlit-cookie-secret", "assemblyai-api-key", "openrouter-api-key"):
        run("secrets", "add-iam-policy-binding", name, f"--project={PROJECT}",
            f"--member=serviceAccount:{RUNTIME}", "--role=roles/secretmanager.secretAccessor")
    bucket = state["upload_bucket"]
    run("iam", "service-accounts", "add-iam-policy-binding", RUNTIME, f"--project={PROJECT}",
        f"--member=serviceAccount:{RUNTIME}", "--role=roles/iam.serviceAccountTokenCreator")
    for role in ("roles/storage.objectCreator", "roles/storage.objectViewer"):
        run("storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}", f"--member=serviceAccount:{RUNTIME}", f"--role={role}",
            f"--condition=title=app_uploads,expression=resource.name.startsWith('projects/_/buckets/{bucket}/objects/uploads/'),description=Application uploads only")
    run("storage", "buckets", "update", f"gs://{bucket}", "--public-access-prevention")
    state["stage_url"] = state["service_url"].replace("https://", "https://security-check---", 1)
    bucket_info = json.loads(run("storage", "buckets", "describe", f"gs://{bucket}", "--format=json"))
    rules = bucket_info.get("cors_config", bucket_info.get("cors", [])) or []
    rules.append({"origin": [state["service_url"], state["stage_url"]], "method": ["POST"],
                  "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600})
    cors_path = ROOT / ".cache/rollout-cors.json"
    cors_path.write_text(json.dumps(rules), encoding="utf-8")
    run("storage", "buckets", "update", f"gs://{bucket}", f"--cors-file={cors_path}")
    state["prepared"] = True
    save(state)
    print("Runtime identity, scoped grants, pinned secrets and validation credentials prepared.")


def prepare_existing_runtime(state):
    """Preserve the live credential versions and permissions during an application update."""
    revision = json.loads(run("run", "revisions", "describe", state["previous_revision"],
                              f"--project={PROJECT}", f"--region={REGION}", "--format=json"))
    spec = revision["spec"]
    if spec["serviceAccountName"] != RUNTIME:
        raise ValueError("The deployed runtime identity does not match this rollout")
    container = spec["containers"][0]
    variables = {item["name"]: item for item in container.get("env", [])}
    for variable, field in (("ASSEMBLYAI_API_KEY", "assemblyai_version"),
                            ("OPENROUTER_API_KEY", "openrouter_version"),
                            ("STREAMLIT_SERVER_COOKIE_SECRET", "cookie_version")):
        state[field] = variables[variable]["valueFrom"]["secretKeyRef"]["key"]
    mount = next(item["name"] for item in container["volumeMounts"] if item["mountPath"] == "/secrets/app-users")
    secret = next(item["secret"] for item in spec["volumes"] if item["name"] == mount)
    if secret["secretName"] != "app-users":
        raise ValueError("The live user credential mount does not match")
    state["users_version"] = next(item["key"] for item in secret["items"] if item["path"] == "users.json")
    accounts = {f"rollout_{letter}_{secrets.token_hex(4)}": secrets.token_urlsafe(32) for letter in ("a", "b")}
    login_path = ROOT / "secrets/validation-logins.json"
    login_path.write_text(json.dumps(accounts), encoding="utf-8")
    hashes_path = ROOT / "secrets/validation-users.json"
    hashes_path.write_text(json.dumps({name: PASSWORD_HASHER.hash(password) for name, password in accounts.items()}), encoding="utf-8")
    state["validation_version"] = secret_from_file("app-users-validation", hashes_path)
    state["stage_url"] = state["service_url"].replace("https://", "https://security-check---", 1)
    grant_validation_access(state)
    state["prepared"] = True
    save(state)
    print("Live credential versions preserved; disposable validation accounts prepared.")


def deploy(state, *, validation):
    if not state.get("prepared"):
        raise ValueError("Run the prepare phase first")
    build = json.loads(run("builds", "describe", state["build_id"], f"--region={REGION}", f"--project={PROJECT}", "--format=json"))
    if build["status"] != "SUCCESS":
        raise ValueError(f"Image build is not verified: {build['status']}")
    digest = build["results"]["images"][0]["digest"]
    image = state["image"].rsplit(":", 1)[0] + "@" + digest
    base_url = state["stage_url"] if validation else state["service_url"]
    env = {
        "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION": "true", "STREAMLIT_SERVER_ENABLE_CORS": "true",
        "STREAMLIT_BROWSER_SERVER_ADDRESS": urlparse(base_url).hostname, "STREAMLIT_BROWSER_SERVER_PORT": "443",
        "APP_USERS_FILE": "/secrets/app-users/users.json", "APP_BASE_URL": base_url,
        "GCS_UPLOAD_BUCKET": state["upload_bucket"], "GCS_SIGNER_SERVICE_ACCOUNT_EMAIL": RUNTIME,
    }
    env_path = ROOT / ".cache/rollout-env.json"
    env_path.write_text(json.dumps(env), encoding="utf-8")
    user_secret = "app-users-validation" if validation else "app-users"
    user_version = state["validation_version"] if validation else state["users_version"]
    bindings = [f"/secrets/app-users/users.json={user_secret}:{user_version}",
                f"STREAMLIT_SERVER_COOKIE_SECRET=streamlit-cookie-secret:{state['cookie_version']}",
                f"ASSEMBLYAI_API_KEY=assemblyai-api-key:{state['assemblyai_version']}",
                f"OPENROUTER_API_KEY=openrouter-api-key:{state['openrouter_version']}"]
    tag = "security-check" if validation else "security-release"
    result = json.loads(run("run", "deploy", SERVICE, f"--image={image}", f"--project={PROJECT}", f"--region={REGION}",
                            f"--service-account={RUNTIME}", "--port=8501", "--memory=1Gi", "--cpu=1", "--timeout=600",
                            "--min-instances=0", "--max-instances=1", "--concurrency=4", "--session-affinity",
                            "--execution-environment=gen2", "--allow-unauthenticated", "--no-traffic", f"--tag={tag}",
                            f"--env-vars-file={env_path}", "--update-secrets=" + ",".join(bindings),
                            "--remove-secrets=OPENAI_API_KEY", "--format=json"))
    key = "stage_revision" if validation else "release_revision"
    state[key] = result["status"]["latestReadyRevisionName"]
    state["image_digest"] = digest
    if not validation:
        state["release_image_digest"] = digest
    save(state)
    print(json.dumps({"revision": state[key], "validation": validation, "traffic": "not promoted"}))


def promote(state):
    if (not state.get("live_checks_passed") or not state.get("release_revision")
            or not state.get("media_checks_passed")
            or state.get("validated_image_digest") != state.get("image_digest")
            or state.get("validated_revision") != state.get("stage_revision")
            or state.get("release_image_digest") != state.get("validated_image_digest")):
        raise ValueError("Live checks and the production credential revision are required")
    run("run", "services", "update-traffic", SERVICE, f"--project={PROJECT}", f"--region={REGION}",
        f"--to-revisions={state['release_revision']}=100", "--clear-tags")
    state["promoted"] = True
    save(state)
    run("secrets", "versions", "disable", state["validation_version"], "--secret=app-users-validation", f"--project={PROJECT}")
    if state.get("validation_access_created"):
        run("secrets", "remove-iam-policy-binding", "app-users-validation", f"--project={PROJECT}",
            f"--member=serviceAccount:{RUNTIME}", "--role=roles/secretmanager.secretAccessor", "--condition=None")
        state["validation_access_created"] = False
        save(state)
    for name in ("validation-users.json", "validation-logins.json"):
        (ROOT / "secrets" / name).unlink(missing_ok=True)
    print("Production traffic promoted; old revision tags and validation credentials removed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "stage", "release", "promote"))
    parser.add_argument("--reuse-runtime", action="store_true", help="Keep the active runtime credentials and permissions")
    args = parser.parse_args()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    if args.phase == "prepare":
        if args.reuse_runtime:
            prepare_existing_runtime(state)
        else:
            prepare(state)
    elif args.phase in ("stage", "release"):
        deploy(state, validation=args.phase == "stage")
    else:
        promote(state)


if __name__ == "__main__":
    main()
