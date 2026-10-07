"""Authenticated rollout checks with disposable accounts and synthetic content."""

import asyncio
import base64
import json
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
import websockets
from websockets.exceptions import InvalidStatus

from scripts.build_cloud import ROOT


def wait_for_script_idle(page):
    # The running indicator appears after 500 ms; wait for reruns before clicking
    # controls which might otherwise be replaced by the preceding widget change.
    page.wait_for_timeout(800)
    page.get_by_test_id("stStatusWidgetRunningIcon").wait_for(state="hidden", timeout=180_000)


def sign_in(page, url, name, password):
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.get_by_label("Логин", exact=True).fill(name)
    page.get_by_label("Пароль", exact=True).fill(password)
    page.get_by_role("button", name="Войти", exact=True).click()
    try:
        page.get_by_role("heading", name="Настройки", exact=True).wait_for(timeout=60_000)
        wait_for_script_idle(page)
    except PlaywrightTimeoutError:
        alerts = " ".join(page.get_by_test_id("stAlert").all_inner_texts())
        raise RuntimeError("Staging login did not complete: " + alerts) from None


def assert_transcript(page, value):
    field = page.get_by_label("Транскрибированный текст", exact=True)
    field.wait_for(timeout=60_000)
    assert field.input_value() == value


async def reject_foreign_origin(url):
    context = ssl.create_default_context(cafile=str(ROOT / ".cache/gcloud-ca.pem"))
    try:
        async with websockets.connect(url.replace("https://", "wss://") + "/_stcore/stream",
                                      origin="https://untrusted.example", subprotocols=["streamlit"], ssl=context,
                                      open_timeout=30):
            raise AssertionError("Cross-origin WebSocket was accepted")
    except InvalidStatus as exc:
        assert exc.response.status_code == 403


def main():
    path = ROOT / ".cache/rollout-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    accounts = list(json.loads((ROOT / "secrets/validation-logins.json").read_text(encoding="utf-8")).items())
    def track_object(uri):
        state.setdefault("validation_objects", []).append(uri)
        latest = json.loads(path.read_text(encoding="utf-8"))
        latest["validation_objects"] = state["validation_objects"]
        path.write_text(json.dumps(latest, indent=2), encoding="utf-8")
    marker_a = "VALIDATION_A: Photosynthesis converts light energy into chemical energy. Plants absorb carbon dioxide and water and release oxygen."
    marker_b = "VALIDATION_B: This separate lecture discusses gravity and mass."
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
            headless=True, chromium_sandbox=True,
        )
        try:
            context_a = browser.new_context(accept_downloads=True)
            context_b = browser.new_context(accept_downloads=True)
            a, b = context_a.new_page(), context_b.new_page()
            a.set_default_timeout(60_000)
            b.set_default_timeout(60_000)
            sign_in(a, state["stage_url"], *accounts[0])
            sign_in(b, state["stage_url"], *accounts[1])
            print("Two independent accounts signed in.", flush=True)
            for page in (a, b):
                page.get_by_role("button", name="Начать заново", exact=False).click()
                wait_for_script_idle(page)
            a.get_by_text("Большая загрузка", exact=True).first.click()
            a.get_by_role("button", name="Подготовить загрузку", exact=True).click()
            frame = a.frame_locator("iframe").locator("#gcs-post-file")
            frame.wait_for()
            upload_frame = next(f for f in a.frames if f.locator("#gcs-post-file").count())
            scripts = upload_frame.locator("script").all_text_contents()
            source = "\n".join(scripts)
            key = json.loads(re.search(r"const objectKeyKnown = (.*?);", source).group(1))
            bucket = json.loads(re.search(r"const bucket = (.*?);", source).group(1))
            fields = json.loads(re.search(r"const fields = (.*?);", source).group(1))
            track_object(f"gs://{bucket}/{key}")
            policy = json.loads(base64.b64decode(fields["policy"]))
            assert ["content-length-range", 1, 10 * 1024**3] in policy["conditions"]
            frame.set_input_files({"name": "same.txt", "mimeType": "text/plain", "buffer": marker_a.encode()})
            upload_frame.locator("#gcs-post-upload-btn").click()
            upload_frame.get_by_text("Файл загружен.", exact=False).wait_for(timeout=90_000)
            a.get_by_role("button", name="Начать обработку файла", exact=True).click()
            assert_transcript(a, marker_a)
            print("Signed, bounded GCS upload and authorized read passed.", flush=True)
            sign_in(b, state["stage_url"] + "/?" + urlencode({"bucket": bucket, "key": key}), *accounts[1])
            assert b.get_by_label("Транскрибированный текст", exact=True).count() == 0
            b.locator('input[type="file"]').set_input_files({"name": "same.txt", "mimeType": "text/plain", "buffer": marker_b.encode()})
            assert_transcript(b, marker_b)
            assert_transcript(a, marker_a)
            print("Forged import rejected and session content remained isolated.", flush=True)
            a.get_by_role("button", name="Сгенерировать саммари", exact=False).click()
            pdf_link = a.get_by_role("link", name="Скачать саммари (.pdf)", exact=False)
            pdf_link.wait_for(timeout=180_000)
            href = pdf_link.get_attribute("href")
            assert href and href.startswith("data:application/pdf;base64,")
            contents = base64.b64decode(href.split(",", 1)[1])
            assert contents.startswith(b"%PDF")
            (ROOT / ".cache/live-summary.pdf").write_bytes(contents)
            with a.expect_download(timeout=30_000) as download:
                pdf_link.click()
            assert not download.value.failure()
            download.value.save_as(str(ROOT / ".cache/live-downloaded-summary.pdf"))
            assert (ROOT / ".cache/live-downloaded-summary.pdf").read_bytes().startswith(b"%PDF")
            print("Live LLM summary, PDF export and browser download passed.", flush=True)
            state["validation_object"] = f"gs://{bucket}/{key}"
            sign_in(a, state["stage_url"], *accounts[0])
            assert_transcript(a, marker_a)
            a.get_by_role("tab", name="📊 AI-саммари", exact=True).click()
            a.get_by_role("link", name="Скачать саммари (.pdf)", exact=False).wait_for(timeout=60_000)
            print("Authenticated refresh recovered the lecture and summary.", flush=True)
            a.get_by_role("button", name="Начать заново", exact=False).click()
            a.get_by_label("Транскрибированный текст", exact=True).wait_for(state="hidden")
            a.get_by_label("Язык транскрибации", exact=True).fill("en")
            a.get_by_label("Язык транскрибации", exact=True).press("Enter")
            wait_for_script_idle(a)
            a.get_by_role("button", name="Подготовить загрузку", exact=True).click()
            wait_for_script_idle(a)
            frame = a.frame_locator("iframe").locator("#gcs-post-file")
            frame.wait_for()
            upload_frame = next(f for f in a.frames if f.locator("#gcs-post-file").count())
            source = "\n".join(upload_frame.locator("script").all_text_contents())
            media_key = json.loads(re.search(r"const objectKeyKnown = (.*?);", source).group(1))
            track_object(f"gs://{bucket}/{media_key}")
            frame.set_input_files({"name": "synthetic.wav", "mimeType": "audio/wav",
                                   "buffer": (ROOT / ".cache/rollout-speech.wav").read_bytes()})
            upload_frame.locator("#gcs-post-upload-btn").click()
            upload_frame.get_by_text("Файл загружен.", exact=False).wait_for(timeout=90_000)
            a.get_by_role("button", name="Начать обработку файла", exact=True).click()
            a.get_by_role("button", name="Транскрибировать аудио", exact=False).click()
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if a.get_by_label("Транскрибированный текст", exact=True).count():
                    break
                pending = a.get_by_role("button", name="Проверить транскрипцию", exact=False)
                if pending.count():
                    pending.click()
                a.wait_for_timeout(2000)
            transcript = a.get_by_label("Транскрибированный текст", exact=True)
            transcript.wait_for(timeout=30_000)
            speech = transcript.input_value()
            assert len(speech) > 20 and "plants" in speech.lower()
            sign_in(a, state["stage_url"], *accounts[0])
            assert_transcript(a, speech)
            state["media_checks_passed"] = True
            print("GCS signed-read streaming, audio preparation, speech transcription and refresh recovery passed.", flush=True)
        finally:
            browser.close()
    asyncio.run(reject_foreign_origin(state["stage_url"]))
    print("Cross-origin WebSocket rejected with HTTP 403.", flush=True)
    latest = json.loads(path.read_text(encoding="utf-8"))
    latest["validation_object"] = state["validation_object"]
    latest["validation_objects"] = state["validation_objects"]
    latest["media_checks_passed"] = state["media_checks_passed"]
    latest["validated_revision"] = state["stage_revision"]
    latest["validated_image_digest"] = state["image_digest"]
    latest["live_checks_passed"] = (latest.get("stage_revision") == state["stage_revision"]
                                    and latest.get("image_digest") == state["image_digest"])
    path.write_text(json.dumps(latest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
