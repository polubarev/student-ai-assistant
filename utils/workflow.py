"""Private, per-user recovery checkpoints for the current application instance."""

import hashlib
import json
import os
from pathlib import Path
import threading
import time

from utils.workspace import SESSION_FILES


WORKFLOW_KEYS = (
    "_session_id", "_workspace_id", "step", "processing_started", "summary_started",
    "audio_path", "video_path", "cloud_media", "file_sig", "input_source_mode",
    "input_source", "input_name", "input_size_bytes", "authorized_upload",
    "transcription_job_id", "transcription_error", "extraction_error", "transcript", "summary",
    "language", "show_transcription_before_summary",
)
_LOCK = threading.RLock()


def _checkpoint_path(state):
    owner, fingerprint = state.get("authenticated_user"), state.get("auth_record")
    if not state.get("password_correct") or not owner or not fingerprint:
        return None
    key = hashlib.sha256(json.dumps([owner, fingerprint]).encode()).hexdigest()
    directory = SESSION_FILES.root / "recovery"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / f"{key}.json"


def save_workflow(state):
    with _LOCK:
        path = _checkpoint_path(state)
        if path is None or not state.get("processing_started"):
            return
        data = {key: state[key] for key in WORKFLOW_KEYS if key in state}
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump({"saved_at": time.time(), "workflow": data}, output, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        temporary.replace(path)


def restore_workflow(state):
    with _LOCK:
        path = _checkpoint_path(state)
        if path is None or state.get("processing_started") or not path.exists():
            return False
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - document["saved_at"] > SESSION_FILES.idle_seconds:
                path.unlink(missing_ok=True)
                return False
            data = document["workflow"]
            workspace = SESSION_FILES._directory(data.get("_workspace_id")) if data.get("_workspace_id") else None
            for key in ("audio_path", "video_path"):
                if data.get(key):
                    artifact = Path(data[key]).resolve()
                    if workspace is None or artifact.parent != workspace:
                        raise ValueError("Invalid checkpoint artifact")
                    if not artifact.is_file():
                        data[key] = None
            if not any(data.get(key) for key in ("audio_path", "video_path", "cloud_media", "transcript", "transcription_job_id")):
                path.unlink(missing_ok=True)
                return False
            state.update({key: value for key, value in data.items() if key in WORKFLOW_KEYS})
            if workspace and workspace.exists():
                SESSION_FILES.ensure(state)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False


def discard_workflow(state):
    with _LOCK:
        path = _checkpoint_path(state)
        if path:
            path.unlink(missing_ok=True)
