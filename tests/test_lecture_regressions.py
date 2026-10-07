"""Lecture-size and transcription UI regressions without paid API requests."""

import json
from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest

import app
from config import Config
from services.audio_service import FFmpegAudioExtractor
from services.storage_service import GCSStorageService
from services.transcription_service import AssemblyAIProvider, TranscriptionPending
from utils.workspace import SessionWorkspace
from utils import workflow
from tests.test_security import State, GRANT, URI


def test_large_upload_capacity_matches_previous_release():
    assert Config.MAX_UPLOAD_BYTES >= 10 * 1024**3


def test_ninety_minute_lecture_is_accepted(monkeypatch, tmp_path):
    source = tmp_path / "lecture.m4a"
    source.write_bytes(b"media")
    probe = MagicMock(return_value=SimpleNamespace(stdout=json.dumps({
        "streams": [{"codec_type": "audio"}], "format": {"duration": "5400"},
    })))
    monkeypatch.setattr("services.audio_service.subprocess.run", probe)
    assert FFmpegAudioExtractor().validate_audio(str(source))


def test_transcription_error_stays_visible_and_can_be_retried(monkeypatch, tmp_path):
    source = tmp_path / "lecture.wav"
    source.write_bytes(b"media")
    provider = MagicMock()
    provider.transcribe.side_effect = [RuntimeError("provider failed"), "lecture text"]
    monkeypatch.setattr(app, "AssemblyAIProvider", lambda _: provider)
    monkeypatch.setattr(app, "paid_job", lambda _: nullcontext())
    monkeypatch.setattr(app.SESSION_FILES, "lease", lambda _: nullcontext())
    test = AppTest.from_string('''
import streamlit as st
import app
app.initialize_session_state()
if not st.session_state.get("audio_path"):
    st.session_state.audio_path = SOURCE
    st.session_state.processing_started = True
    st.session_state.authenticated_user = "test-user"
    st.session_state.assemblyai_key = "test-key"
    st.session_state.language = "ru"
app.step_transcribe()
'''.replace("SOURCE", repr(str(source)))).run()
    test.button(key="transcribe_audio_button").click().run()
    assert not test.exception
    assert len(test.error) == 1, "The error disappeared during the automatic rerun"
    assert test.session_state["audio_path"] == str(source)
    assert source.exists()
    test.run()
    assert len(test.error) == 1, "The error must survive a later rerun"
    test.button(key="transcribe_audio_button").click().run()
    assert not test.exception
    assert not test.error
    assert test.session_state["transcript"] == "lecture text"


def test_cloud_video_larger_than_instance_memory_is_not_downloaded(monkeypatch, tmp_path):
    files = SessionWorkspace(tmp_path / "workspaces")
    state = State(authorized_upload=GRANT, authenticated_user="alice", _session_id="session-a")
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(app, "SESSION_FILES", files)
    monkeypatch.setattr(Config, "GCS_UPLOAD_BUCKET", "uploads")
    storage = MagicMock()
    storage.media_metadata.return_value = {"name": "input.bin", "size": 5 * 1024**3,
                                          "content_type": "video/mp4", "generation": "12"}
    monkeypatch.setattr(app, "GCSStorageService", lambda: storage)
    assert app.load_from_gcs_uri(URI) == (True, None)
    storage.download_to_path.assert_not_called()
    assert state.cloud_media == {"uri": URI, "generation": "12", "kind": "video"}
    assert state.input_size_bytes == 5 * 1024**3
    assert state.authorized_upload == GRANT


def test_cloud_text_still_uses_bounded_download(monkeypatch, tmp_path):
    files = SessionWorkspace(tmp_path / "workspaces")
    state = State(authorized_upload=GRANT, authenticated_user="alice", _session_id="session-a")
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(app, "SESSION_FILES", files)
    monkeypatch.setattr(Config, "GCS_UPLOAD_BUCKET", "uploads")
    storage = MagicMock()
    storage.media_metadata.return_value = {"name": "input.bin", "size": 4,
                                          "content_type": "text/plain", "generation": "12"}
    storage.download_to_path.side_effect = lambda uri, path, **kw: path.write_text("text")
    monkeypatch.setattr(app, "GCSStorageService", lambda: storage)
    assert app.load_from_gcs_uri(URI) == (True, None)
    assert storage.download_to_path.call_args.kwargs["max_size_bytes"] == Config.MAX_TEXT_BYTES
    assert state.transcript == "text"


def test_read_url_pins_generation_and_rejects_another_object(monkeypatch):
    service = GCSStorageService(client=MagicMock())
    monkeypatch.setattr(service, "_get_signing_identity", lambda: ("signer@example.com", "token"))
    service.signed_media_url(URI, expected_bucket="uploads", expected_object=GRANT["object_key"], generation="12")
    kwargs = service.client.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["query_parameters"] == {"generation": "12"}
    assert kwargs["method"] == "GET"
    with pytest.raises(PermissionError):
        service.signed_media_url("gs://other/input", expected_bucket="uploads", expected_object=GRANT["object_key"], generation="12")


@pytest.mark.parametrize("url", ["http://storage.googleapis.com/uploads/uploads/input?x=y",
                                   "https://evil.example/uploads/uploads/input?x=y",
                                   "https://storage.googleapis.com/other/uploads/input?x=y",
                                   "https://storage.googleapis.com/uploads/private/input?x=y"])
def test_cloud_extraction_rejects_untrusted_urls(monkeypatch, tmp_path, url):
    from services.audio_service import MediaValidationError
    monkeypatch.setattr(Config, "GCS_UPLOAD_BUCKET", "uploads")
    run = MagicMock()
    monkeypatch.setattr("services.audio_service.subprocess.run", run)
    with pytest.raises(MediaValidationError):
        FFmpegAudioExtractor().extract_cloud_audio(url, str(tmp_path / "output.mp3"))
    run.assert_not_called()


def test_transcription_timeout_resumes_without_a_second_submission(monkeypatch, tmp_path):
    import services.transcription_service as module
    source = tmp_path / "lecture.mp3"
    source.write_bytes(b"media")
    monkeypatch.setattr(FFmpegAudioExtractor, "require_valid_audio", lambda *_: None)
    client, transcriber, resumed = MagicMock(), MagicMock(), MagicMock()
    pending = transcriber.return_value.submit.return_value
    pending.id, pending.status = "existing-job", "processing"
    pending.wait_for_completion.side_effect = module.aai.TranscriptError("poll timeout")
    resumed.status, resumed.text = "completed", "lecture transcript"
    monkeypatch.setattr(module.aai, "Client", client)
    monkeypatch.setattr(module.aai, "Transcriber", transcriber)
    transcript_class = MagicMock(return_value=resumed)
    monkeypatch.setattr(module.aai, "Transcript", transcript_class)
    remember = MagicMock()
    provider = AssemblyAIProvider("key")
    with pytest.raises(TranscriptionPending):
        provider.transcribe(str(source), on_submitted=remember)
    remember.assert_called_once_with("existing-job")
    # Resumption must work even if the old local audio has disappeared.
    source.unlink()
    assert provider.transcribe(str(source), transcript_id="existing-job") == "lecture transcript"
    transcriber.return_value.submit.assert_called_once_with(str(source))
    transcript_class.assert_called_once_with(transcript_id="existing-job", client=client.return_value)
    assert client.return_value.http_client.close.call_count == 2


def test_refresh_recovers_only_same_authenticated_user_and_password(monkeypatch, tmp_path):
    files = SessionWorkspace(tmp_path / "workspaces")
    monkeypatch.setattr(workflow, "SESSION_FILES", files)
    state = State(password_correct=True, authenticated_user="alice", auth_record="hash-a",
                  _session_id="session-a", processing_started=True, transcript="private lecture",
                  transcription_job_id="job-a", assemblyai_key="do-not-save")
    state.audio_path = str(files.new_path(state, ".mp3"))
    Path(state.audio_path).write_bytes(b"media")
    workflow.save_workflow(state)
    for user, fingerprint, authenticated in [("bob", "hash-a", True), ("alice", "rotated", True),
                                              ("alice", "hash-a", False)]:
        other = State(password_correct=authenticated, authenticated_user=user, auth_record=fingerprint)
        assert not workflow.restore_workflow(other)
        assert "transcript" not in other
    refreshed = State(password_correct=True, authenticated_user="alice", auth_record="hash-a", _session_id="new")
    assert workflow.restore_workflow(refreshed)
    assert refreshed.transcript == "private lecture"
    assert refreshed.transcription_job_id == "job-a"
    assert refreshed._session_id == "session-a"
    assert "assemblyai_key" not in refreshed
    workflow.discard_workflow(refreshed)
    assert not workflow.restore_workflow(State(password_correct=True, authenticated_user="alice", auth_record="hash-a"))


def test_pending_ui_checks_existing_job_without_consuming_another_paid_job(monkeypatch, tmp_path):
    source = tmp_path / "lecture.mp3"
    source.write_bytes(b"media")
    calls = []
    def transcribe(path, config, *, transcript_id, on_submitted):
        calls.append(transcript_id)
        if not transcript_id:
            on_submitted("job-123")
            raise TranscriptionPending("pending")
        return "completed lecture"
    provider = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr(app, "AssemblyAIProvider", lambda _: provider)
    paid = MagicMock(side_effect=lambda _: nullcontext())
    monkeypatch.setattr(app, "paid_job", paid)
    monkeypatch.setattr(app, "processing_slot", nullcontext)
    monkeypatch.setattr(app.SESSION_FILES, "lease", lambda _: nullcontext())
    test = AppTest.from_string('''
import streamlit as st
import app
app.initialize_session_state()
st.session_state.audio_path = SOURCE
st.session_state.authenticated_user = "test-user"
st.session_state.assemblyai_key = "test-key"
st.session_state.language = "ru"
app.step_transcribe()
'''.replace("SOURCE", repr(str(source)))).run()
    test.button(key="transcribe_audio_button").click().run()
    assert not test.exception
    assert not test.error
    assert test.session_state["transcription_job_id"] == "job-123"
    assert "Проверить" in test.button(key="transcribe_audio_button").label
    test.button(key="transcribe_audio_button").click().run()
    assert not test.exception
    assert test.session_state["transcript"] == "completed lecture"
    assert calls == [None, "job-123"]
    paid.assert_called_once_with("test-user")


def test_real_hour_long_mp3_reproduces_previous_validation_failure(monkeypatch, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for real media validation")
    source = tmp_path / "lecture.bin"  # GCS media has a .bin object name.
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "3660", "-codec:a", "libmp3lame",
                    "-b:a", "32k", "-f", "mp3", str(source)], check=True, timeout=60)
    assert source.stat().st_size < 32 * 1024**2
    extractor = FFmpegAudioExtractor()
    assert extractor.validate_audio(str(source))
    monkeypatch.setattr(Config, "MAX_AUDIO_SECONDS", 3600)
    assert not extractor.validate_audio(str(source))


def test_cloud_streaming_is_compressed_bounded_and_does_not_log_signed_url(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(Config, "GCS_UPLOAD_BUCKET", "uploads")
    output = tmp_path / "prepared.mp3"
    url = "https://storage.googleapis.com/uploads/uploads/input?X-Goog-Signature=private-token"
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[0].endswith("ffprobe"):
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "audio"}],
                                                      "format": {"duration": 5400}}))
        output.write_bytes(b"compressed media")
        return SimpleNamespace()
    monkeypatch.setattr("services.audio_service.subprocess.run", run)
    with caplog.at_level("DEBUG"):
        assert FFmpegAudioExtractor().extract_cloud_audio(url, str(output))
    command, options = commands[1]
    assert command[command.index("-i") + 1] == url
    assert command[command.index("-b:a") + 1] == "32k"
    assert command[command.index("-fs") + 1] == str(Config.MAX_AUDIO_BYTES)
    assert options["timeout"] == Config.MEDIA_PROCESS_TIMEOUT_SECONDS
    assert "private-token" not in caplog.text


def test_full_app_restores_lecture_after_browser_refresh(monkeypatch, tmp_path):
    files = SessionWorkspace(tmp_path / "workspaces")
    monkeypatch.setattr(workflow, "SESSION_FILES", files)
    monkeypatch.setattr(app, "SESSION_FILES", files)
    state = State(password_correct=True, authenticated_user="alice", auth_record="hash-a",
                  _session_id="old-session", processing_started=True, transcript="Recovered lecture text",
                  step=2, input_name="lecture.mp3", input_source_mode=app.SOURCE_MODE_LOCAL)
    workflow.save_workflow(state)
    monkeypatch.setattr(app, "check_password", lambda: True)
    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "test-key")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "test-key")
    test = AppTest.from_string('''
import streamlit as st
import app
st.session_state.password_correct = True
st.session_state.authenticated_user = "alice"
st.session_state.auth_record = "hash-a"
app.main()
''').run()
    assert not test.exception
    assert test.session_state["transcript"] == "Recovered lecture text"
    assert test.session_state["input_name"] == "lecture.mp3"
    assert not test.button(key="generate_summary_button").disabled
    test.button(key="start_over_sidebar_button").click().run()
    assert not test.exception
    assert test.session_state["transcript"] is None
    assert not workflow.restore_workflow(State(password_correct=True, authenticated_user="alice", auth_record="hash-a"))
