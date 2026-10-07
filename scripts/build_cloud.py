"""Build and test a credential-free source bundle with a dedicated Cloud Build identity."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "ai-student-assistant-v2"
REGION = "us-east1"
REPOSITORY = "student-ai-assistant-repo"
BUILDER = f"student-ai-builder@{PROJECT}.iam.gserviceaccount.com"
GCloud = shutil.which("gcloud.cmd") if os.name == "nt" else shutil.which("gcloud")


def run(*args, optional=False):
    result = subprocess.run([GCloud, *args, "--quiet"], capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        if optional and ("NOT_FOUND" in result.stderr or "does not exist" in result.stderr
                         or "not found: 404" in result.stderr):
            return None
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def main(*, reuse_existing=False):
    if not GCloud:
        raise RuntimeError("Google Cloud CLI is required")
    identity = run("iam", "service-accounts", "describe", BUILDER, f"--project={PROJECT}", "--format=json", optional=True)
    if identity is None and reuse_existing:
        raise ValueError("Existing build identity is required")
    if identity is None:
        run("iam", "service-accounts", "create", "student-ai-builder", f"--project={PROJECT}", "--display-name=Student AI image builder")
    if not reuse_existing:
        run("artifacts", "repositories", "add-iam-policy-binding", REPOSITORY, f"--location={REGION}",
            f"--project={PROJECT}", f"--member=serviceAccount:{BUILDER}", "--role=roles/artifactregistry.writer")
        run("projects", "add-iam-policy-binding", PROJECT, f"--member=serviceAccount:{BUILDER}", "--role=roles/logging.logWriter")
    project_number = run("projects", "describe", PROJECT, "--format=value(projectNumber)")
    source_bucket = f"student-ai-build-source-{project_number}"
    bucket = run("storage", "buckets", "describe", f"gs://{source_bucket}", "--format=json", optional=True)
    if bucket is None:
        if reuse_existing:
            raise ValueError("Existing source bucket is required")
        run("storage", "buckets", "create", f"gs://{source_bucket}", f"--project={PROJECT}",
            f"--location={REGION}", "--uniform-bucket-level-access", "--public-access-prevention")
    if not reuse_existing:
        run("storage", "buckets", "add-iam-policy-binding", f"gs://{source_bucket}",
            f"--member=serviceAccount:{BUILDER}", "--role=roles/storage.objectViewer")

    identifier = uuid4().hex[:12]
    source = ROOT / ".cache" / f"cloud-build-{identifier}"
    source.mkdir(parents=True)
    files = [ROOT / name for name in ("app.py", "config.py", "Dockerfile", "requirements.lock",
                                      "requirements-dev.lock", "pytest.ini", ".streamlit/config.toml")]
    for folder in ("services", "utils", "tests", "scripts"):
        files.extend((ROOT / folder).rglob("*.py"))
    files.append(ROOT / "data/system_prompt.md")
    for path in files:
        relative = path.resolve().relative_to(ROOT)
        if path.is_symlink():
            raise ValueError("Source symlinks are not allowed")
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    (source / ".gcloudignore").write_text(".git\n__pycache__\n*.pyc\n", encoding="utf-8")
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}/student-ai-assistant:security-{identifier}"
    checks = (
        "set -eu; python -m venv --system-site-packages /tmp/security-tests; "
        "/tmp/security-tests/bin/python -m pip install --require-hashes -r /checks/requirements-dev.lock; "
        "PYTHONPATH=/app:/checks /tmp/security-tests/bin/python -m pytest -q -p no:cacheprovider "
        "-c /checks/pytest.ini /checks/tests"
    )
    pdf_check = "from app import build_summary_pdf_bytes; data=build_summary_pdf_bytes('# Security export check\\n\\nSample lecture.'); assert data.startswith(b'%PDF'); print('PDF export passed')"
    config = {
        "steps": [
            {"name": "gcr.io/cloud-builders/docker", "args": ["build", "--platform", "linux/amd64", "-t", image, "."]},
            {"name": "gcr.io/cloud-builders/docker", "args": ["run", "--rm", "--entrypoint", "/bin/sh", "--volume", "/workspace:/checks:ro", image, "-c", checks]},
            {"name": "gcr.io/cloud-builders/docker", "args": ["run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c", pdf_check]},
        ],
        "images": [image],
        "timeout": "1800s",
        "options": {"logging": "CLOUD_LOGGING_ONLY"},
    }
    config_path = ROOT / ".cache" / f"cloud-build-{identifier}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = json.loads(run("builds", "submit", str(source), f"--config={config_path}", f"--project={PROJECT}",
                            f"--region={REGION}", f"--service-account=projects/{PROJECT}/serviceAccounts/{BUILDER}",
                            f"--gcs-source-staging-dir=gs://{source_bucket}/sources", "--async", "--format=json"))
    state_path = ROOT / ".cache/rollout-state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        (ROOT / ".cache" / f"rollout-before-{identifier}.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        state = {}
    service = json.loads(run("run", "services", "describe", "student-ai-assistant-service",
                             f"--project={PROJECT}", f"--region={REGION}", "--format=json"))
    values = {item["name"]: item.get("value")
              for item in service["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    active = next(item for item in service["status"]["traffic"] if item.get("percent") == 100)
    state.update(service_url=service["status"]["url"], previous_revision=active["revisionName"],
                 previous_identity=service["spec"]["template"]["spec"]["serviceAccountName"],
                 upload_bucket=values["GCS_UPLOAD_BUCKET"], previous_traffic=service["status"]["traffic"])
    state.update(build_id=result["id"], image=image, build_region=REGION, build_source=str(source),
                 live_checks_passed=False, validated_revision=None, validated_image_digest=None,
                 release_revision=None, release_image_digest=None, prepared=False, promoted=False,
                 production_checks_passed=False, speech_validation_passed=False, media_checks_passed=False)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps({"build_id": result["id"], "image": image, "status": result.get("status")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-existing", action="store_true", help="Use existing build permissions and source bucket")
    main(reuse_existing=parser.parse_args().reuse_existing)
