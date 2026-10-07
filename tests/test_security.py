"""Exercise security boundaries without live cloud credentials or user data."""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app
from config import Config
from services.audio_service import FFmpegAudioExtractor
from services.storage_service import GCSStorageService
from utils.credentials import PASSWORD_HASHER, load_users, verify_password
from utils.limits import LimitExceeded, RateLimiter, processing_slot
from utils.uploads import authorize_upload
from utils.workspace import SessionWorkspace


class State(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


GRANT = {"bucket_name": "uploads", "object_key": "uploads/random/input.bin",
         "owner": "alice", "session_id": "session-a"}
URI = "gs://uploads/uploads/random/input.bin"


@pytest.mark.parametrize("uri,owner,session,bucket", [
    (URI, "bob", "session-a", "uploads"),
    (URI, "alice", "session-b", "uploads"),
    ("gs://other/uploads/random/input.bin", "alice", "session-a", "uploads"),
    ("gs://uploads/uploads/random/other.bin", "alice", "session-a", "uploads"),
    (URI, "alice", "session-a", "different"),
])
def test_upload_rejects_other_users_sessions_buckets_and_keys(uri, owner, session, bucket):
    with pytest.raises(PermissionError):
        authorize_upload(uri, GRANT, bucket, owner, session)


def test_only_exact_issued_object_is_authorized():
    assert authorize_upload(URI, GRANT, "uploads", "alice", "session-a") == ("uploads", GRANT["object_key"])


def test_app_rejects_forged_uri_before_creating_cloud_client(monkeypatch):
    state = State(authorized_upload=GRANT, authenticated_user="bob", _session_id="session-b")
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(Config, "GCS_UPLOAD_BUCKET", "uploads")
    client = MagicMock()
    monkeypatch.setattr(app, "GCSStorageService", client)
    assert app.load_from_gcs_uri(URI)[0] is False
    client.assert_not_called()


def storage_fixture(size=4, content_type="audio/wav", generation=12):
    client = MagicMock()
    blob = client.bucket.return_value.get_blob.return_value
    blob.size, blob.content_type, blob.generation, blob.updated = size, content_type, generation, None
    blob.download_to_file.side_effect = lambda output, **_: output.write(b"data")
    return GCSStorageService(client), client, blob


def download(service, path, **kwargs):
    return service.download_to_path(URI, path, expected_bucket="uploads", expected_object=GRANT["object_key"],
                                    max_size_bytes=10, max_text_bytes=5, **kwargs)


@pytest.mark.parametrize("size,mime,generation", [(11, "audio/wav", 12), (6, "text/plain", 12),
                                                   (None, "audio/wav", 12), (4, "audio/wav", None)])
def test_invalid_metadata_rejected_before_download(tmp_path, size, mime, generation):
    service, _, blob = storage_fixture(size, mime, generation)
    destination = tmp_path / "input.bin"
    with pytest.raises(ValueError):
        download(service, destination)
    assert not destination.exists()
    blob.download_to_file.assert_not_called()


def test_storage_rejects_bucket_before_metadata_lookup(tmp_path):
    service, client, _ = storage_fixture()
    with pytest.raises(PermissionError):
        service.download_to_path("gs://other/object", tmp_path / "input", expected_bucket="uploads",
                                 expected_object=GRANT["object_key"], max_size_bytes=10, max_text_bytes=5)
    client.bucket.assert_not_called()


def test_download_pins_generation_and_keeps_bytes_bounded(tmp_path):
    service, _, blob = storage_fixture()
    target = tmp_path / "input.bin"
    assert download(service, target)["size"] == 4
    assert target.read_bytes() == b"data"
    assert blob.download_to_file.call_args.kwargs["if_generation_match"] == 12
    assert blob.download_to_file.call_args.kwargs["raw_download"] is True


def test_download_removes_partial_file_when_stream_exceeds_limit(tmp_path):
    service, _, blob = storage_fixture()
    blob.download_to_file.side_effect = lambda output, **_: output.write(b"x" * 11)
    target = tmp_path / "input.bin"
    with pytest.raises(ValueError):
        download(service, target)
    assert not target.exists()


def test_signing_failure_has_no_unbounded_put_fallback(monkeypatch):
    service, client, _ = storage_fixture()
    monkeypatch.setattr(service, "_get_signing_identity", lambda: ("signer@example.com", "fake-token"))
    client.generate_signed_post_policy_v4.side_effect = RuntimeError("signing unavailable")
    with pytest.raises(RuntimeError):
        service.create_signed_upload_form("uploads", "uploads/random/", "https://app.example", max_size_bytes=10)
    assert ["content-length-range", 1, 10] in client.generate_signed_post_policy_v4.call_args.kwargs["conditions"]
    assert ["starts-with", "$Content-Type", ""] in client.generate_signed_post_policy_v4.call_args.kwargs["conditions"]
    client.bucket.assert_not_called()


def test_sessions_with_same_filename_have_distinct_private_paths(tmp_path):
    files = SessionWorkspace(tmp_path)
    alice, bob = State(), State()
    path_a, path_b = files.new_path(alice, ".wav"), files.new_path(bob, ".wav")
    path_a.write_bytes(b"alice")
    path_b.write_bytes(b"bob")
    assert path_a.parent != path_b.parent
    files.clear(alice)
    assert not path_a.exists()
    assert path_b.read_bytes() == b"bob"


def test_reset_preserves_new_input_and_deletes_previous_artifacts(tmp_path):
    files = SessionWorkspace(tmp_path)
    state = State()
    old, new = files.new_path(state, ".wav"), files.new_path(state, ".txt")
    old.write_bytes(b"old")
    new.write_text("new")
    files.clear(state, keep=new)
    assert not old.exists()
    assert new.read_text() == "new"


def test_cleanup_excludes_active_jobs_and_rejects_outside_paths(tmp_path):
    now = [1000]
    files = SessionWorkspace(tmp_path, clock=lambda: now[0], idle_seconds=300)
    a, b = State(), State()
    path_a, path_b = files.new_path(a), files.new_path(b)
    path_a.write_bytes(b"active")
    path_b.write_bytes(b"abandoned")
    os.utime(path_b.parent, (now[0], now[0]))
    with files.lease(a):
        now[0] += 1000
        files.cleanup_stale()
        assert path_a.exists()
        assert not path_b.exists()
    with pytest.raises(ValueError):
        files.clear(State(_workspace_id="../outside"))
    assert tmp_path.exists()


def test_app_ingest_and_reset_keep_auth_but_remove_private_artifacts(monkeypatch, tmp_path):
    files = SessionWorkspace(tmp_path)
    state = State(authenticated_user="alice", password_correct=True, authenticated_at=42, auth_record="record")
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(app, "SESSION_FILES", files)
    path = files.new_path(state, ".txt")
    path.write_text("lecture", encoding="utf-8")
    app.ingest_prepared_file(path, "input.txt", "text", "signature", "local", 7)
    assert path.exists() and state.transcript == "lecture"
    assert state.authenticated_user == "alice"
    app.reset_workflow()
    assert not path.exists() and not state.transcript
    assert state.authenticated_user == "alice" and state.auth_record == "record"


def test_argon_accepts_new_password_and_rejects_legacy_or_invalid_hash():
    encoded = PASSWORD_HASHER.hash("fresh long test password")
    assert verify_password("fresh long test password", encoded)
    assert not verify_password("incorrect", encoded)
    assert not verify_password("password", "a" * 64)
    assert not verify_password("password", "$argon2id$broken")


def test_mixed_legacy_credential_store_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"alice": "a" * 64}))
    monkeypatch.setenv("APP_USERS_FILE", str(path))
    with pytest.raises(ValueError):
        load_users()


def test_login_form_clears_password_and_revokes_rotated_credentials(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"test-user": PASSWORD_HASHER.hash("test-password-long")}))
    monkeypatch.setenv("APP_USERS_FILE", str(path))
    test = AppTest.from_string("from utils.auth import check_password\nimport streamlit as st\nif check_password(): st.write('private result')").run()
    test.text_input(key="username").set_value("test-user")
    test.text_input(key="password").set_value("test-password-long")
    test.button[0].click().run()
    assert not test.exception
    assert test.session_state["password_correct"] is True
    assert "password" not in test.session_state
    path.write_text(json.dumps({"test-user": PASSWORD_HASHER.hash("rotated-password-long")}))
    test.run()
    assert not test.exception
    assert len(test.text_input) == 2
    assert not any(m.value == "private result" for m in test.markdown)


def test_rate_limit_survives_new_sessions_and_expires():
    now = [1000]
    limiter = RateLimiter(2, 300, clock=lambda: now[0], max_keys=2)
    assert limiter.consume("alice") and limiter.consume("alice")
    assert not limiter.consume("alice")
    assert limiter.consume("bob")
    assert not limiter.consume("new attacker identity")
    now[0] += 301
    assert limiter.consume("alice")


def test_processing_slot_rejects_parallel_jobs_and_releases_after_failure():
    with processing_slot():
        with pytest.raises(LimitExceeded):
            with processing_slot():
                pass
    with processing_slot():
        pass


def test_media_probe_is_bounded_and_rejects_long_audio(monkeypatch, tmp_path):
    media = tmp_path / "audio.wav"
    media.write_bytes(b"fake media")
    probe = MagicMock(return_value=SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "audio"}],
                                                                         "format": {"duration": Config.MAX_AUDIO_SECONDS + 1}})))
    monkeypatch.setattr(subprocess, "run", probe)
    assert not FFmpegAudioExtractor().validate_audio(str(media))
    command = probe.call_args.args[0]
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
    assert "hls" not in command[command.index("-format_whitelist") + 1]
    assert probe.call_args.kwargs["timeout"] == 15


def test_ffmpeg_timeout_removes_partial_output(monkeypatch, tmp_path):
    media, output = tmp_path / "input.mp4", tmp_path / "output.wav"
    media.write_bytes(b"input")
    extractor = FFmpegAudioExtractor()
    monkeypatch.setattr(extractor, "validate_audio", lambda _: True)
    def timed_out(*args, **kwargs):
        output.write_bytes(b"partial")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timed_out)
    assert not extractor.extract_audio(str(media), str(output))
    assert not output.exists()


def test_pdf_worker_blocks_resources_and_has_a_timeout(monkeypatch):
    import services.pdf_service as module
    for uri in ("http://169.254.169.254/metadata", "https://example.com/image", "file:///secrets/users.json", "data:image/svg+xml,svg"):
        with pytest.raises(ValueError):
            module.deny_external_resource(uri)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-worker")
    worker = MagicMock(return_value=SimpleNamespace(stdout=b"%PDF-fixture"))
    monkeypatch.setattr(module.subprocess, "run", worker)
    assert app.build_summary_pdf_bytes("<script>alert('test')</script>") == b"%PDF-fixture"
    arguments = worker.call_args.kwargs
    assert arguments["timeout"] == 30
    assert "OPENROUTER_API_KEY" not in arguments["env"]
    assert b"&lt;script&gt;" in arguments["input"]


def test_pdf_worker_timeout_is_sanitized(monkeypatch):
    import services.pdf_service as module
    monkeypatch.setattr(module.subprocess, "run", MagicMock(side_effect=subprocess.TimeoutExpired("worker", 30)))
    with pytest.raises(RuntimeError, match="PDF export could not be completed"):
        module.render_pdf("<p>text</p>")


def test_summary_html_is_escaped_and_pdf_is_cached_per_session(monkeypatch):
    from tests.test_downloads import _StreamlitStub
    st = _StreamlitStub()
    st.session_state = State(st.session_state)
    st.session_state.summary = "<style>body{display:none}</style>"
    calls = []
    original_markdown = st.markdown
    st.markdown = lambda body, **kw: (calls.append((body, kw)), original_markdown(body))
    render = MagicMock(return_value=b"%PDF")
    monkeypatch.setattr(app, "st", st)
    monkeypatch.setattr(app, "build_summary_pdf_bytes", render)
    app.section_results()
    app.section_results()
    render.assert_called_once()
    summary_calls = [kw for body, kw in calls if body == st.session_state.summary]
    assert all(kw["unsafe_allow_html"] is False for kw in summary_calls)
    st.session_state.summary = "changed"
    app.section_results()
    assert render.call_count == 2


def test_password_reset_writes_hashes_without_printing_passwords(monkeypatch, tmp_path, capsys):
    from scripts import reset_passwords
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["reset_passwords", "new-user"])
    monkeypatch.setattr(reset_passwords, "getpass", lambda _: "fresh-test-password")
    reset_passwords.main()
    users = json.loads(Path("secrets/users.json").read_text())
    assert verify_password("fresh-test-password", users["new-user"])
    assert "fresh-test-password" not in capsys.readouterr().out
    assert not list(Path("secrets").glob("*.tmp"))


def test_transcription_uses_private_client_and_bounded_polling(monkeypatch, tmp_path):
    import services.transcription_service as module
    source = tmp_path / "input.wav"
    source.write_bytes(b"input")
    client_class, transcriber = MagicMock(), MagicMock()
    transcript = transcriber.return_value.submit.return_value
    transcript.status, transcript.text = "completed", "lecture transcript"
    monkeypatch.setattr(module.aai, "Client", client_class)
    monkeypatch.setattr(module.aai, "Transcriber", transcriber)
    monkeypatch.setattr(module.FFmpegAudioExtractor, "require_valid_audio", lambda *_: None)
    previous_key = module.aai.settings.api_key
    assert module.AssemblyAIProvider("key-a").transcribe(str(source)) == "lecture transcript"
    assert module.aai.settings.api_key == previous_key
    assert client_class.call_args.kwargs["settings"].api_key == "key-a"
    assert transcriber.call_args.kwargs["client"] is client_class.return_value
    transcript.wait_for_completion.assert_called_once_with(poll_timeout=Config.TRANSCRIPTION_POLL_SECONDS)
    client_class.return_value.http_client.close.assert_called_once()


def test_llm_rejects_excessive_transcripts_without_a_paid_request(monkeypatch):
    import services.llm_service as module
    client = MagicMock()
    monkeypatch.setattr(module, "OpenRouter", client)
    service = module.LLMService(api_key="test-key")
    with pytest.raises(ValueError):
        service.summarize_text("x" * (Config.MAX_TRANSCRIPT_CHARS + 1))
    client.return_value.chat.send.assert_not_called()


def test_ffprobe_accepts_real_local_wav_when_available(tmp_path):
    import shutil
    import wave
    if not shutil.which("ffprobe"):
        pytest.skip("FFprobe is installed in the production image and CI")
    media = tmp_path / "input.wav"
    with wave.open(str(media), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000)
    assert FFmpegAudioExtractor().validate_audio(str(media))
