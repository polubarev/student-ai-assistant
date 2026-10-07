"""Test a supplied lecture with the deployed image and save its transcript locally."""

import argparse
import base64
import json
from pathlib import Path
from uuid import uuid4

from scripts.build_cloud import ROOT, PROJECT, REGION, run
from scripts.deploy_cloud import RUNTIME


def main(source):
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("The test audio file does not exist")
    state = json.loads((ROOT / ".cache/rollout-state.json").read_text())
    identifier = uuid4().hex[:12]
    prefix = f"uploads/lecture-check/{identifier}/"
    bucket = state["upload_bucket"]
    input_key, output_key = prefix + "input.mp3", prefix + "transcript.txt"
    input_uri, output_uri = f"gs://{bucket}/{input_key}", f"gs://{bucket}/{output_key}"
    job = f"lecture-check-{identifier}"
    image = state["image"].rsplit(":", 1)[0] + "@" + state["release_image_digest"]
    destination = ROOT / "temp/lecture_transcripts" / (source.stem + ".txt")
    destination.parent.mkdir(parents=True, exist_ok=True)
    report_path = ROOT / ".cache" / f"lecture-check-{identifier}.json"
    report = {"job": job, "input_uri": input_uri, "output_uri": output_uri,
              "source": str(source), "destination": str(destination), "image": image}
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    code = f'''import os, time
from pathlib import Path
from google.cloud import storage
from config import Config
from services.storage_service import GCSStorageService
from services.audio_service import FFmpegAudioExtractor
from services.transcription_service import AssemblyAIProvider, TranscriptionPending
service = GCSStorageService()
meta = service.media_metadata({input_uri!r}, expected_bucket={bucket!r}, expected_object={input_key!r})
url = service.signed_media_url({input_uri!r}, expected_bucket={bucket!r}, expected_object={input_key!r}, generation=meta['generation'])
prepared = '/tmp/prepared-lecture.mp3'
assert FFmpegAudioExtractor().extract_cloud_audio(url, prepared), 'Audio preparation failed'
print('Actual lecture validation and cloud audio preparation passed', flush=True)
provider = AssemblyAIProvider(os.environ['ASSEMBLYAI_API_KEY'])
submitted = {{'id': None}}
def remember(identifier):
    submitted['id'] = identifier
    print('Lecture submitted to transcription provider', flush=True)
deadline = time.monotonic() + 900
while time.monotonic() < deadline:
    try:
        text = provider.transcribe(prepared, Config.get_transcription_config('ru'), transcript_id=submitted['id'], on_submitted=remember)
        break
    except TranscriptionPending:
        print('Transcription pending; resuming the same job', flush=True)
        time.sleep(5)
else:
    raise RuntimeError('Transcription did not complete within 15 minutes')
assert len(text) > 100, 'Transcription result is too short'
storage.Client().bucket({bucket!r}).blob({output_key!r}).upload_from_string(text, content_type='text/plain; charset=utf-8', if_generation_match=0, timeout=60)
print('Actual lecture transcription passed; characters=' + str(len(text)), flush=True)
'''
    created = False
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    entrypoint = f"import base64;exec(base64.b64decode('{encoded}'))"
    try:
        run("storage", "cp", str(source), input_uri)
        run("run", "jobs", "create", job, f"--project={PROJECT}", f"--region={REGION}", f"--image={image}",
            f"--service-account={RUNTIME}", "--cpu=1", "--memory=1Gi", "--max-retries=0", "--task-timeout=1200",
            "--command=python", "--args=^~^-c~" + entrypoint,
            f"--set-env-vars=GCS_UPLOAD_BUCKET={bucket},GCS_SIGNER_SERVICE_ACCOUNT_EMAIL={RUNTIME}",
            f"--set-secrets=ASSEMBLYAI_API_KEY=assemblyai-api-key:{state['assemblyai_version']}")
        created = True
        print(f"Testing the actual lecture with the deployed image. Job: {job}", flush=True)
        run("run", "jobs", "execute", job, f"--project={PROJECT}", f"--region={REGION}", "--wait")
        run("storage", "cp", output_uri, str(destination))
        text = destination.read_text(encoding="utf-8")
        report.update(completed=True, transcript_characters=len(text))
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Actual lecture transcription passed. Saved transcript: {destination}", flush=True)
    finally:
        if created:
            run("run", "jobs", "delete", job, f"--project={PROJECT}", f"--region={REGION}")
        for uri in (input_uri, output_uri):
            try:
                run("storage", "rm", uri, optional=True)
            except RuntimeError as exc:
                if "matched no objects" not in str(exc):
                    raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    main(parser.parse_args().source)
