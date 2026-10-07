"""Validate the deployed transcription client using disposable synthetic audio."""

import json
from pathlib import Path
from uuid import uuid4

from scripts.build_cloud import ROOT, PROJECT, REGION, run
from scripts.deploy_cloud import RUNTIME


def main():
    state_path = ROOT / ".cache/rollout-state.json"
    state = json.loads(state_path.read_text())
    identifier = uuid4().hex[:10]
    key = f"uploads/rollout-validation/speech-{identifier}.wav"
    uri = f"gs://{state['upload_bucket']}/{key}"
    job = f"security-speech-{identifier}"
    image = state["image"].rsplit(":", 1)[0] + "@" + state["release_image_digest"]
    code = (
        "import os; from pathlib import Path; "
        "from services.storage_service import GCSStorageService; "
        "from services.transcription_service import AssemblyAIProvider; "
        "p=Path('/tmp/rollout-speech.wav'); "
        f"GCSStorageService().download_to_path({uri!r},p,expected_bucket={state['upload_bucket']!r},"
        f"expected_object={key!r},max_size_bytes=1048576,max_text_bytes=1048576); "
        "text=AssemblyAIProvider(os.environ['ASSEMBLYAI_API_KEY']).transcribe(str(p),{'language_code':'en'}); "
        "assert len(text)>20; print('Synthetic speech transcription passed')"
    )
    created = False
    try:
        run("storage", "cp", str(ROOT / ".cache/rollout-speech.wav"), uri)
        run("run", "jobs", "create", job, f"--project={PROJECT}", f"--region={REGION}", f"--image={image}",
            f"--service-account={RUNTIME}", "--cpu=1", "--memory=1Gi", "--max-retries=0", "--task-timeout=300",
            "--command=python", "--args=^~^-c~" + code,
            f"--set-secrets=ASSEMBLYAI_API_KEY=assemblyai-api-key:{state['assemblyai_version']}")
        created = True
        run("run", "jobs", "execute", job, f"--project={PROJECT}", f"--region={REGION}", "--wait")
        state["speech_validation_passed"] = True
        state_path.write_text(json.dumps(state, indent=2))
        print("Synthetic speech transcription passed on the deployed image and runtime identity.")
    finally:
        if created:
            run("run", "jobs", "delete", job, f"--project={PROJECT}", f"--region={REGION}")
        run("storage", "rm", uri)


if __name__ == "__main__":
    main()
