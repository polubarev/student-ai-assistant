import streamlit as st
import streamlit.components.v1 as components
import os
import secrets
from pathlib import Path
import hashlib
import json
import time

from services.audio_service import FFmpegAudioExtractor
from services.audio_service import MediaValidationError
from services.transcription_service import TranscriptionService, AssemblyAIProvider, TranscriptionPending, TranscriptionFailed
from services.llm_service import LLMService
from services.storage_service import GCSStorageService
from config import Config
from utils.logger import get_logger, Logger
from utils.auth import check_password
from utils.downloads import build_download_link
from utils.limits import LimitExceeded, UPLOAD_REQUESTS, paid_job, processing_slot
from utils.uploads import authorize_upload
from utils.workspace import SESSION_FILES
from utils.workflow import save_workflow as save_checkpoint, restore_workflow, discard_workflow

# -------------------------
# Logging
# -------------------------
Logger.setup_logging(
    log_level=os.getenv("LOG_LEVEL", "INFO"),
    log_file=os.getenv("LOG_FILE", "logs/app.log")
)
logger = get_logger(__name__)


# -------------------------
# Helpers
# -------------------------

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
TEXT_EXTENSIONS = {".txt"}
SUPPORTED_UPLOAD_TYPES = sorted({ext[1:] for ext in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | TEXT_EXTENSIONS})
LOCAL_UPLOAD_LIMIT_MB = 32
LOCAL_UPLOAD_LIMIT_BYTES = LOCAL_UPLOAD_LIMIT_MB * 1024 * 1024
SOURCE_MODE_LOCAL = "Локальная загрузка"
SOURCE_MODE_LARGE = "Большая загрузка"


def save_workflow():
    try:
        save_checkpoint(st.session_state)
    except OSError:
        logger.warning("Could not save workflow checkpoint")


def prepare_cloud_audio():
    """Resolve the session grant afresh and stream a bounded audio derivative."""
    media = st.session_state.get("cloud_media")
    if not media:
        raise MediaValidationError("Аудиофайл недоступен. Загрузите его снова.")
    bucket, key = authorize_upload(media["uri"], st.session_state.get("authorized_upload"),
                                   Config.GCS_UPLOAD_BUCKET, st.session_state.get("authenticated_user"),
                                   st.session_state.get("_session_id"))
    signed_url = GCSStorageService().signed_media_url(
        media["uri"], expected_bucket=bucket, expected_object=key, generation=media["generation"],
    )
    audio_path = SESSION_FILES.new_path(st.session_state, ".mp3")
    if not FFmpegAudioExtractor().extract_cloud_audio(signed_url, str(audio_path)):
        raise MediaValidationError("Не удалось подготовить аудио. Файл сохранён в облаке; попробуйте снова.")
    st.session_state.audio_path = str(audio_path)
    st.session_state.step = 1
    save_workflow()
    return str(audio_path)


def format_size(size_bytes: int | float | None) -> str:
    """Format byte size in human-readable units."""
    if size_bytes is None:
        return "—"
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "—"
    if size < 0:
        return "—"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
    return "—"


def build_summary_pdf_html(markdown_text: str) -> str:
    """Render Markdown into a printable HTML document (GFM-like tables included)."""
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise RuntimeError(
            "Для PDF-экспорта нужен пакет markdown-it-py. "
            "Установите зависимости из requirements.txt."
        ) from exc

    md = MarkdownIt("commonmark", {"html": False})
    md.enable(["table", "strikethrough"])
    body_html = md.render(markdown_text or "")

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    @page {{
      size: A4;
      margin: 14mm 12mm;
    }}
    body {{
      font-family: "Noto Sans", "DejaVu Sans", "Arial Unicode MS", "Arial", sans-serif;
      font-size: 12px;
      line-height: 1.5;
      color: #111;
      margin: 0;
      word-break: break-word;
    }}
    h1, h2, h3, h4, h5, h6 {{
      margin: 0.95em 0 0.45em;
      line-height: 1.25;
      page-break-after: avoid;
    }}
    h1 {{ font-size: 24px; }}
    h2 {{ font-size: 20px; }}
    h3 {{ font-size: 17px; }}
    h4 {{ font-size: 15px; }}
    h5 {{ font-size: 13px; }}
    h6 {{ font-size: 12px; }}
    p, ul, ol, pre, blockquote, table {{
      margin: 0 0 0.8em;
    }}
    ul, ol {{
      padding-left: 1.3em;
    }}
    code, pre {{
      font-family: "SFMono-Regular", "Menlo", "Consolas", "Liberation Mono", monospace;
    }}
    code {{
      background: #f4f4f4;
      border-radius: 4px;
      padding: 0.1em 0.3em;
    }}
    pre {{
      background: #f7f7f7;
      border: 1px solid #e4e4e4;
      border-radius: 8px;
      padding: 10px 12px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    pre code {{
      background: transparent;
      border-radius: 0;
      padding: 0;
    }}
    blockquote {{
      border-left: 4px solid #cccccc;
      margin-left: 0;
      padding-left: 12px;
      color: #444444;
    }}
    hr {{
      border: 0;
      border-top: 1px solid #d9d9d9;
      margin: 1.1em 0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      page-break-inside: avoid;
    }}
    th, td {{
      border: 1px solid #d0d0d0;
      padding: 8px 10px;
      vertical-align: top;
      text-align: left;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #f3f5f7;
      font-weight: 700;
    }}
    tr:nth-child(even) td {{
      background: #fcfcfc;
    }}
  </style>
</head>
<body>
{body_html}
</body>
</html>"""


def build_summary_pdf_bytes(text: str) -> bytes:
    """Render trusted formatting and escaped Markdown in a bounded static worker."""
    from services.pdf_service import render_pdf
    if len(text) > Config.MAX_TRANSCRIPT_CHARS:
        raise ValueError("Summary exceeds the PDF export limit")
    return render_pdf(build_summary_pdf_html(text))


def source_signature(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"::")
    return h.hexdigest()[:16]


def has_direct_gcs_upload_config() -> bool:
    return bool(getattr(Config, "GCS_UPLOAD_BUCKET", "")) and bool(getattr(Config, "APP_BASE_URL", ""))


def build_gcs_upload_redirect_url() -> str:
    base_url = (getattr(Config, "APP_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Загрузка больших файлов сейчас недоступна.")
    return f"{base_url}/?gcs_upload=1"


def render_gcs_upload_form(form_data: dict) -> None:
    """Render a direct browser-to-GCS upload form."""
    mode = str(form_data.get("mode", "post"))
    fields = form_data.get("fields", {})
    action_url = form_data.get("url", "")
    expires_at = form_data.get("expires_at", "")
    bucket_name = str(form_data.get("bucket_name", ""))
    object_key = str(form_data.get("object_key", ""))
    success_redirect_url = str(form_data.get("success_redirect_url", ""))

    if mode == "post":
        upload_url_json = json.dumps(action_url)
        fields_json = json.dumps(fields)
        bucket_json = json.dumps(bucket_name)
        object_key_json = json.dumps(object_key)
        redirect_url_json = json.dumps(success_redirect_url)
        filename_placeholder_json = json.dumps("${filename}")
        upload_limit = int(form_data["max_size_bytes"])
        html = f"""
        <div style="border:1px solid #ddd;padding:12px;border-radius:8px">
          <div style="font-size:14px;margin-bottom:8px;">
            Загрузка большого файла (ссылка действует ограниченное время)
          </div>
          <input id="gcs-post-file" type="file" required />
          <button id="gcs-post-upload-btn" style="margin-left:8px;">Загрузить файл</button>
          <progress id="gcs-post-upload-progress" max="100" value="0" style="display:block;width:100%;margin-top:8px;" hidden></progress>
          <div id="gcs-post-upload-status" style="font-size:12px;color:#666;margin-top:8px;"></div>
        </div>
        <script>
          (function() {{
            const uploadUrl = {upload_url_json};
            const fields = {fields_json};
            const maxFileBytes = {upload_limit};
            const bucket = {bucket_json};
            const objectKeyKnown = {object_key_json};
            const redirectBase = {redirect_url_json};
            const filenamePlaceholder = {filename_placeholder_json};

            const btn = document.getElementById("gcs-post-upload-btn");
            const fileInput = document.getElementById("gcs-post-file");
            const status = document.getElementById("gcs-post-upload-status");
            const progress = document.getElementById("gcs-post-upload-progress");

            if (!btn || !fileInput || !status || !progress) {{
              return;
            }}

            function setStatus(text) {{
              status.textContent = text;
              try {{
                console.log("[GCS upload POST]", text);
              }} catch (_e) {{
                // no-op
              }}
            }}

            function formatBytes(value) {{
              const units = ["B", "KB", "MB", "GB", "TB"];
              let size = Number(value || 0);
              let idx = 0;
              while (size >= 1024 && idx < units.length - 1) {{
                size /= 1024;
                idx += 1;
              }}
              return (idx === 0 ? size.toFixed(0) : size.toFixed(1)) + " " + units[idx];
            }}

            function resolveObjectKey(fileName) {{
              if (objectKeyKnown) {{
                return objectKeyKnown;
              }}
              const keyField = typeof fields.key === "string" ? fields.key : "";
              if (!keyField) {{
                return "";
              }}
              if (keyField.includes(filenamePlaceholder)) {{
                return keyField.replace(filenamePlaceholder, fileName || "upload.bin");
              }}
              return keyField;
            }}

            setStatus("Готово к загрузке. Выберите файл и нажмите кнопку.");
            fileInput.addEventListener("change", function() {{
              const file = fileInput.files && fileInput.files[0];
              if (!file) {{
                setStatus("Файл не выбран.");
                return;
              }}
              setStatus("Выбран файл: " + file.name + " (" + formatBytes(file.size || 0) + ")");
            }});

            btn.addEventListener("click", async function(event) {{
              event.preventDefault();
              const file = fileInput.files && fileInput.files[0];
              if (!file) {{
                setStatus("Сначала выберите файл.");
                return;
              }}

              if (file.size > maxFileBytes) {{
                setStatus("Файл превышает допустимый размер (" + formatBytes(maxFileBytes) + ").");
                return;
              }}

              btn.disabled = true;
              progress.hidden = false;
              progress.max = 100;
              progress.value = 0;
              setStatus("Начинаю загрузку...");
              try {{
                await new Promise((resolve, reject) => {{
                  const xhr = new XMLHttpRequest();
                  xhr.timeout = 60 * 60 * 1000; // 1 hour for large uploads
                  xhr.open("POST", uploadUrl, true);

                  let sawProgressEvent = false;
                  let settled = false;
                  const startedAt = Date.now();
                  let fallbackPercent = 0;
                  const fallbackTimer = window.setInterval(function() {{
                    if (settled || sawProgressEvent) {{
                      return;
                    }}
                    fallbackPercent = Math.min(95, fallbackPercent + (fallbackPercent < 60 ? 4 : 1));
                    progress.value = fallbackPercent;
                    const elapsedSeconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
                    setStatus("Загрузка... " + elapsedSeconds + " с");
                  }}, 700);

                  function settle(ok, errorMessage) {{
                    if (settled) {{
                      return;
                    }}
                    settled = true;
                    window.clearInterval(fallbackTimer);
                    if (ok) {{
                      progress.max = 100;
                      progress.value = 100;
                      resolve();
                    }} else {{
                      reject(new Error(errorMessage));
                    }}
                  }}

                  xhr.upload.addEventListener("progress", function(progressEvent) {{
                    sawProgressEvent = true;
                    if (progressEvent.lengthComputable) {{
                      const percent = Math.min(100, Math.round((progressEvent.loaded / progressEvent.total) * 100));
                      progress.max = 100;
                      progress.value = percent;
                      setStatus(
                        "Загрузка: " + percent + "% (" +
                        formatBytes(progressEvent.loaded) + " / " +
                        formatBytes(progressEvent.total) + ")"
                      );
                    }} else {{
                      setStatus("Загрузка: " + formatBytes(progressEvent.loaded));
                    }}
                  }});

                  xhr.upload.onloadstart = function() {{
                    setStatus("Соединение установлено, начинаю передачу файла...");
                  }};

                  xhr.onload = function() {{
                    if (xhr.status >= 200 && xhr.status < 400) {{
                      settle(true, "");
                    }} else {{
                      settle(false, "Ошибка загрузки, статус " + xhr.status);
                    }}
                  }};
                  xhr.onerror = function() {{
                    settle(false, "Failed to fetch");
                  }};
                  xhr.onabort = function() {{
                    settle(false, "Загрузка прервана");
                  }};
                  xhr.ontimeout = function() {{
                    settle(false, "Превышено время ожидания загрузки");
                  }};

                  const formData = new FormData();
                  Object.keys(fields || {{}}).forEach(function(key) {{
                    const value = fields[key];
                    if (value !== undefined && value !== null) {{
                      formData.append(key, String(value));
                    }}
                  }});
                  const mimeTypes = {{
                    mp4: "video/mp4", avi: "video/x-msvideo", mov: "video/quicktime",
                    mkv: "video/x-matroska", wmv: "video/x-ms-wmv", flv: "video/x-flv",
                    webm: "video/webm", mp3: "audio/mpeg", wav: "audio/wav", m4a: "audio/mp4",
                    flac: "audio/flac", aac: "audio/aac", ogg: "audio/ogg", txt: "text/plain",
                  }};
                  const extension = (file.name || "").toLowerCase().split(".").pop();
                  formData.append("Content-Type", mimeTypes[extension] || file.type || "application/octet-stream");
                  formData.append("file", file);
                  xhr.send(formData);
                }});

                const objectKeyResolved = resolveObjectKey(file.name || "upload.bin");
                if (!bucket || !objectKeyResolved) {{
                  status.innerHTML =
                    "Файл загружен. Ниже нажмите кнопку «Начать обработку файла».";
                  return;
                }}
                status.innerHTML =
                  "Файл загружен.<br/>" +
                  "Ниже на странице нажмите кнопку «Начать обработку файла».";
              }} catch (error) {{
                const rawMessage = (error && error.message) ? error.message : "";
                setStatus(
                  "Загрузка не удалась: " +
                  (rawMessage || "неизвестная ошибка") +
                  ". Попробуйте еще раз."
                );
              }} finally {{
                btn.disabled = false;
              }}
            }});
          }})();
        </script>
        """
        components.html(html, height=240)
        return

    st.error(f"Неподдерживаемый режим загрузки: {mode}")


def ensure_session_tmpdir() -> Path:
    return SESSION_FILES.ensure(st.session_state)


def detect_file_kind(file_name: str, mime_type: str = "") -> str | None:
    """Detect input kind from mime type first, then filename extension."""
    if mime_type:
        if "video" in mime_type:
            return "video"
        if "audio" in mime_type:
            return "audio"
        if "text" in mime_type:
            return "text"

    extension = Path(file_name).suffix.lower()
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in TEXT_EXTENSIONS:
        return "text"
    return None


def ingest_prepared_file(
    local_path: Path,
    file_name: str,
    file_kind: str,
    signature: str,
    source_label: str,
    size_bytes: int,
) -> None:
    """Store prepared local file path in session state and move workflow forward."""
    if size_bytes > Config.MAX_UPLOAD_BYTES:
        raise ValueError(f"Файл превышает допустимый размер ({format_size(Config.MAX_UPLOAD_BYTES)}).")
    transcript = None
    if file_kind == "text":
        if size_bytes > Config.MAX_TEXT_BYTES:
            raise ValueError("Текстовый файл превышает допустимый размер (2 MB).")
        try:
            transcript = local_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError("Используйте текстовый файл в кодировке UTF-8.") from None
        if len(transcript) > Config.MAX_TRANSCRIPT_CHARS:
            raise ValueError("Транскрипция слишком длинная. Разделите её на части.")
    reset_workflow(prepared_path=local_path)

    st.session_state.file_sig = signature
    st.session_state.input_name = file_name
    st.session_state.input_source = source_label
    st.session_state.input_size_bytes = size_bytes

    if file_kind == "video":
        st.session_state.video_path = str(local_path)
        st.session_state.processing_started = True
        save_workflow()
        return

    if file_kind == "audio":
        st.session_state.audio_path = str(local_path)
        st.session_state.step = 1  # Skip audio extraction.
        st.session_state.processing_started = True
        save_workflow()
        return

    if file_kind == "text":
        st.session_state.transcript = transcript
        st.session_state.step = 2
        st.session_state.processing_started = True
        save_workflow()
        return

    raise ValueError("Неподдерживаемый тип файла.")


def load_from_gcs_uri(
    gcs_uri: str, original_name: str | None = None, wait_for_object_seconds: int = 0,
) -> tuple[bool, str | None]:
    """Authorize and inspect cloud media; only bounded text files are downloaded."""
    local_path = None
    try:
        bucket, key = authorize_upload(
            gcs_uri, st.session_state.get("authorized_upload"), Config.GCS_UPLOAD_BUCKET,
            st.session_state.get("authenticated_user"), st.session_state.get("_session_id"),
        )
        with processing_slot(), SESSION_FILES.lease(st.session_state):
            storage_service = GCSStorageService()
            deadline = time.monotonic() + min(20, max(0, wait_for_object_seconds))
            while True:
                try:
                    meta = storage_service.media_metadata(gcs_uri, expected_bucket=bucket, expected_object=key)
                    break
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        return False, "Файл пока не найден. Завершите загрузку и попробуйте снова."
                    time.sleep(2)
        file_name = (original_name or "").strip() or str(meta["name"])
        file_kind = detect_file_kind(file_name, str(meta["content_type"]))
        if not file_kind:
            raise ValueError("Этот формат файла пока не поддерживается.")
        current_sig = source_signature(gcs_uri, str(meta["generation"]))
        if current_sig == st.session_state.get("file_sig"):
            return True, "Этот файл уже открыт."
        if file_kind == "text":
            with processing_slot(), SESSION_FILES.lease(st.session_state):
                local_path = SESSION_FILES.new_path(st.session_state, ".txt")
                storage_service.download_to_path(
                    gcs_uri, local_path, expected_bucket=bucket, expected_object=key,
                    max_size_bytes=Config.MAX_TEXT_BYTES, max_text_bytes=Config.MAX_TEXT_BYTES,
                )
            ingest_prepared_file(local_path, file_name, file_kind, current_sig, SOURCE_MODE_LARGE, int(meta["size"]))
        else:
            grant = dict(st.session_state.authorized_upload)
            reset_workflow()
            st.session_state.authorized_upload = grant
            st.session_state.cloud_media = {"uri": gcs_uri, "generation": meta["generation"], "kind": file_kind}
            st.session_state.file_sig = current_sig
            st.session_state.input_name = file_name
            st.session_state.input_source = SOURCE_MODE_LARGE
            st.session_state.input_size_bytes = int(meta["size"])
            st.session_state.processing_started = True
            st.session_state.step = 1 if file_kind == "audio" else 0
        save_workflow()
        logger.info("Authorized upload prepared: kind=%s size=%s", file_kind, meta["size"])
        return True, None
    except PermissionError:
        return False, "Загрузка не принадлежит этому сеансу. Подготовьте новую загрузку."
    except LimitExceeded as exc:
        return False, str(exc)
    except Exception:
        if local_path:
            local_path.unlink(missing_ok=True)
        logger.warning("Upload preparation failed")
        return False, "Не удалось открыть файл. Проверьте формат и допустимый размер."


def initialize_session_state():
    """Initialize session state with default values for a new workflow."""
    defaults = {
        "_session_id": secrets.token_hex(32),
        "step": 0,
        "processing_started": False,
        "transcription_started": False,
        "summary_started": False,
        "audio_path": None,
        "cloud_media": None,
        "transcription_job_id": None,
        "transcription_error": None,
        "extraction_error": None,
        "video_path": None,
        "file_sig": None,
        "input_source_mode": SOURCE_MODE_LOCAL,
        "input_source": None,
        "input_name": None,
        "input_size_bytes": None,
        "results_tab_default": None,
        "gcs_upload_form": None,
        "handled_redirect_key": None,
        "transcript": None,
        "summary": None,
        "transcription_displayed": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # Backward compatibility for sessions created before localization.
    legacy_mode = st.session_state.get("input_source_mode")
    if legacy_mode == "Local upload":
        st.session_state.input_source_mode = SOURCE_MODE_LOCAL
    elif legacy_mode == "Large file upload":
        st.session_state.input_source_mode = SOURCE_MODE_LARGE

    # Backward compatibility for provider switch: OpenAI -> OpenRouter.
    if "openrouter_key" not in st.session_state and st.session_state.get("openai_key"):
        st.session_state.openrouter_key = st.session_state.get("openai_key")
    if "openrouter_model" not in st.session_state and st.session_state.get("openai_model"):
        st.session_state.openrouter_model = st.session_state.get("openai_model")


def reset_workflow(prepared_path=None):
    """Reset workflow artifacts, preserving auth and config."""
    discard_workflow(st.session_state)
    SESSION_FILES.clear(st.session_state, keep=prepared_path)
    # Store settings and auth keys before clearing
    preserved_values = {
        "system_prompt": st.session_state.get("system_prompt"),
        "password_correct": st.session_state.get("password_correct"),
        "username": st.session_state.get("username"),
        "authenticated_user": st.session_state.get("authenticated_user"),
        "authenticated_at": st.session_state.get("authenticated_at"),
        "auth_record": st.session_state.get("auth_record"),
        "_session_id": st.session_state.get("_session_id"),
        "_workspace_id": st.session_state.get("_workspace_id"),
        "assemblyai_key": st.session_state.get("assemblyai_key"),
        "openrouter_key": st.session_state.get("openrouter_key") or st.session_state.get("openai_key"),
        "language": st.session_state.get("language"),
        "openrouter_model": st.session_state.get("openrouter_model") or st.session_state.get("openai_model"),
        "show_transcription_before_summary": st.session_state.get("show_transcription_before_summary"),
        "input_source_mode": st.session_state.get("input_source_mode"),
        "gcs_upload_form": st.session_state.get("gcs_upload_form") if prepared_path else None,
        "authorized_upload": st.session_state.get("authorized_upload") if prepared_path else None,
    }

    st.session_state.clear()

    # Restore preserved values
    for key, value in preserved_values.items():
        if value is not None:
            st.session_state[key] = value

    # Initialize workflow state
    initialize_session_state()
# -------------------------
# UI Sections
# -------------------------

def sidebar_config():
    with st.sidebar:
        st.header("Настройки")

        has_env_keys, missing_keys = Config.validate_api_keys()
        logger.info(f"API key validation: has_keys={has_env_keys}, missing={missing_keys}")

        if has_env_keys:
            st.success("✅ Приложение готово к работе")
        else:
            st.warning("Сервис настроен не полностью. Обратитесь к администратору.")
        assemblyai_key = Config.ASSEMBLYAI_API_KEY
        openrouter_key = Config.OPENROUTER_API_KEY

        language_options = ["ru", "en", "es", "fr", "de", "it", "pt", "ja", "ko", "zh"]
        default_lang_index = (
            language_options.index(st.session_state.get("language", Config.DEFAULT_LANGUAGE))
            if st.session_state.get("language", Config.DEFAULT_LANGUAGE) in language_options
            else 0
        )
        language = st.selectbox(
            "Язык транскрибации",
            language_options,
            index=default_lang_index,
            help="Выберите язык аудио для более точной транскрибации",
        )

        openrouter_model = Config.DEFAULT_OPENROUTER_MODEL
        system_prompt = st.session_state.get("system_prompt", "")

        # New: checkbox to show transcription before summary
        show_before = st.checkbox(
            "Показывать транскрипцию перед саммари",
            value=st.session_state.get("show_transcription_before_summary", False),
            help="Проверьте транскрипцию перед суммаризацией",
        )

        # Persist selections
        st.session_state.assemblyai_key = assemblyai_key
        st.session_state.openrouter_key = openrouter_key
        st.session_state.language = language
        st.session_state.openrouter_model = openrouter_model.strip()
        st.session_state.system_prompt = system_prompt
        st.session_state.show_transcription_before_summary = show_before

        if st.button("Выйти", key="logout"):
            discard_workflow(st.session_state)
            SESSION_FILES.clear(st.session_state)
            st.session_state.clear()
            st.rerun()

        st.divider()
        st.header("⚙️ Статус обработки")

        if st.session_state.get("processing_started") and st.session_state.get("input_name"):
            st.success("Файл загружен")
            st.caption(f"Файл: {st.session_state.get('input_name')}")
            st.caption(f"Способ загрузки: {st.session_state.get('input_source')}")
            if st.session_state.get("input_size_bytes") is not None:
                st.caption(f"Размер: {format_size(st.session_state.get('input_size_bytes'))}")

            if st.session_state.get("summary"):
                st.info("Саммари готово.")
            elif st.session_state.get("transcript"):
                st.info("Транскрипция готова. Можно делать саммари.")
            elif st.session_state.get("audio_path"):
                st.info("Аудио готово. Можно запускать транскрибацию.")
            elif st.session_state.get("video_path"):
                st.info("Видео загружено. Можно извлечь аудио.")
            elif st.session_state.get("cloud_media"):
                st.info("Файл в облаке готов к обработке.")
            else:
                st.info("Файл готов к обработке.")
        else:
            st.info("Загрузите файл, чтобы начать.")

        if st.button("🔄 Начать заново", help="Сбросить процесс", key="start_over_sidebar_button"):
            reset_workflow()
            st.toast("Процесс сброшен.")
            st.rerun()


# -------------------------
# Core Steps
# -------------------------

def step_upload_and_prepare():
    st.header("📁 Входной файл")
    st.caption(
        f"Если файл небольшой (до ~{LOCAL_UPLOAD_LIMIT_MB} MB), выберите «Локальная загрузка». "
        "Для больших файлов используйте «Большая загрузка»."
    )

    source_mode = st.radio(
        "Источник",
        options=[SOURCE_MODE_LOCAL, SOURCE_MODE_LARGE],
        key="input_source_mode",
        horizontal=True,
    )

    if source_mode == SOURCE_MODE_LOCAL:
        uploaded_file = st.file_uploader(
            "Выберите видео, аудио или файл транскрипции",
            type=SUPPORTED_UPLOAD_TYPES,
            help="Поддерживаются форматы: видео, аудио и текст UTF-8.",
        )

        if uploaded_file is not None:
            if uploaded_file.size > LOCAL_UPLOAD_LIMIT_BYTES:
                st.error(
                    f"Размер файла {uploaded_file.size / (1024 * 1024):.2f} MB. Для такого размера используйте «Большая загрузка»."
                )
                st.info("Переключитесь на «Большая загрузка» ниже.")
            else:
                file_kind = detect_file_kind(uploaded_file.name, uploaded_file.type or "")
                if not file_kind:
                    st.error("Неподдерживаемый тип файла.")
                else:
                    data = uploaded_file.getvalue()
                    current_sig = source_signature(
                        uploaded_file.name,
                        str(uploaded_file.size),
                        uploaded_file.type or "",
                        hashlib.sha256(data).hexdigest()[:16],
                    )
                    if current_sig != st.session_state.get("file_sig"):
                        local_path = None
                        try:
                            if not UPLOAD_REQUESTS.consume(st.session_state.authenticated_user):
                                raise LimitExceeded("Лимит загрузок достигнут. Попробуйте через час.")
                            if file_kind == "text" and uploaded_file.size > Config.MAX_TEXT_BYTES:
                                raise ValueError("Текстовый файл превышает допустимый размер (2 MB).")
                            local_path = SESSION_FILES.new_path(st.session_state, Path(uploaded_file.name).suffix)
                            local_path.write_bytes(data)

                            ingest_prepared_file(
                                local_path=local_path,
                                file_name=uploaded_file.name,
                                file_kind=file_kind,
                                signature=current_sig,
                                source_label=SOURCE_MODE_LOCAL,
                                size_bytes=uploaded_file.size,
                            )
                            st.toast("Обнаружен новый файл. Начинаю обработку...")
                            st.rerun()
                        except Exception as e:
                            if local_path:
                                local_path.unlink(missing_ok=True)
                            logger.warning("Local upload preparation failed")
                            st.error(str(e) if isinstance(e, (ValueError, LimitExceeded)) else "Не удалось открыть файл.")
                    else:
                        st.info("Этот файл уже загружен.")
    else:
        upload_bucket = (getattr(Config, "GCS_UPLOAD_BUCKET", "") or "").removeprefix("gs://")
        st.caption(f"Максимальный размер: {format_size(Config.MAX_UPLOAD_BYTES)}. Длительность аудио: до {Config.MAX_AUDIO_SECONDS / 3600:g} ч.")
        if has_direct_gcs_upload_config():
            st.subheader("Большая загрузка файла")
            st.caption("Подготовьте загрузку, затем выберите файл и нажмите «Загрузить файл».")
            if st.button("Подготовить загрузку", key="prepare_direct_gcs_upload"):
                try:
                    if not UPLOAD_REQUESTS.consume(st.session_state.authenticated_user):
                        raise LimitExceeded("Лимит загрузок достигнут. Попробуйте через час.")
                    with st.spinner("Подготавливаю загрузку..."):
                        key_prefix = f"uploads/{st.session_state._session_id}/"
                        storage_service = GCSStorageService()
                        form_data = storage_service.create_signed_upload_form(
                            bucket_name=upload_bucket,
                            key_prefix=key_prefix,
                            success_redirect_url=build_gcs_upload_redirect_url(),
                            max_size_bytes=Config.MAX_UPLOAD_BYTES,
                        )
                        st.session_state.authorized_upload = {
                            "bucket_name": form_data["bucket_name"], "object_key": form_data["object_key"],
                            "owner": st.session_state.authenticated_user,
                            "session_id": st.session_state._session_id,
                        }
                        st.session_state.gcs_upload_form = form_data
                except Exception as e:
                    logger.exception("Failed to prepare signed GCS upload")
                    st.error(
                        "❌ Не удалось подготовить загрузку. Попробуйте еще раз или обратитесь к администратору."
                    )

            if st.session_state.get("gcs_upload_form"):
                render_gcs_upload_form(st.session_state.gcs_upload_form)

                pending_form = st.session_state.get("gcs_upload_form") or {}
                pending_bucket = str(pending_form.get("bucket_name", "")).strip()
                pending_object_key = str(pending_form.get("object_key", "")).strip()

                if pending_bucket and pending_object_key:
                    pending_uri = f"gs://{pending_bucket}/{pending_object_key}"
                    st.caption(
                        "После загрузки нажмите кнопку ниже, чтобы сразу перейти к обработке."
                    )
                    if st.button("Начать обработку файла", key="ingest_pending_put_upload"):
                        with st.spinner("Открываю загруженный файл..."):
                            ok, message = load_from_gcs_uri(pending_uri, wait_for_object_seconds=20)
                        if ok and not message:
                            st.session_state.handled_redirect_key = pending_uri
                            st.toast("Файл готов к обработке.")
                            st.rerun()
                        elif ok and message:
                            st.info(message)
                        else:
                            st.error(f"❌ Не удалось открыть файл: {message}")

        else:
            st.info(
                "Загрузка больших файлов сейчас недоступна. Обратитесь к администратору."
            )

    if not st.session_state.get("processing_started"):
        return

    if not st.session_state.get("assemblyai_key") and st.session_state.get("step", 0) < 2:
        st.error("Сервис распознавания речи пока не настроен. Обратитесь к администратору.")
        return
    if not st.session_state.get("openrouter_key"):
        st.error("Сервис ИИ пока не настроен. Обратитесь к администратору.")
        return


def step_extract_audio():
    st.subheader("Шаг 1 — Извлечение аудио")

    if st.session_state.get("audio_path"):
        st.success("✅ Аудио уже извлечено")
        st.audio(st.session_state.audio_path)
        return

    if st.session_state.get("extraction_error"):
        st.error(st.session_state.extraction_error)
    disabled = not st.session_state.get("processing_started") or not (st.session_state.get("video_path") or st.session_state.get("cloud_media"))

    if st.button("🎵 Извлечь аудио из видео", disabled=disabled, key="extract_audio_button"):
        st.session_state.extraction_error = None
        try:
            with st.spinner("Извлекаю аудио..."), processing_slot(), SESSION_FILES.lease(st.session_state):
                if st.session_state.get("cloud_media"):
                    prepare_cloud_audio()
                else:
                    audio_path = SESSION_FILES.new_path(st.session_state, ".mp3")
                    extractor = FFmpegAudioExtractor()
                    extractor.require_valid_audio(st.session_state.video_path)
                    if not extractor.extract_audio(str(st.session_state.video_path), str(audio_path)):
                        raise MediaValidationError("Не удалось обработать видео. Файл сохранён; попробуйте снова.")
                    st.session_state.audio_path = str(audio_path)
                st.session_state.step = max(st.session_state.get("step", 0), 1)
            st.toast("Аудио извлечено.")
        except Exception as e:
            logger.exception("Audio extraction failed")
            st.session_state.extraction_error = str(e) if isinstance(e, (LimitExceeded, MediaValidationError)) else "Не удалось обработать видео. Файл сохранён; попробуйте снова."
        save_workflow()
        st.rerun()


def step_transcribe():
    st.subheader("Шаг 2 — Транскрибация аудио")

    if st.session_state.get("transcript"):
        st.success("✅ Транскрибация уже готова")
        with st.expander("Предпросмотр транскрипции"):
            st.write((st.session_state.get("transcript") or "")[:1000] + ("..." if len(st.session_state.get("transcript") or "") > 1000 else ""))
        return

    if st.session_state.get("transcription_error"):
        st.error(st.session_state.transcription_error)
    job_id = st.session_state.get("transcription_job_id")
    if job_id:
        st.info("Лекция отправлена на распознавание. Проверка продолжит тот же запрос без повторной отправки файла.")
    disabled = not (job_id or st.session_state.get("audio_path") or st.session_state.get("cloud_media")) or not st.session_state.get("assemblyai_key")

    label = "🔎 Проверить транскрипцию" if job_id else "📝 Транскрибировать аудио"
    if st.button(label, disabled=disabled, key="transcribe_audio_button"):
        st.session_state.transcription_error = None
        try:
            limit = processing_slot() if job_id else paid_job(st.session_state.authenticated_user)
            with st.spinner("Проверяю транскрипцию..." if job_id else "Подготавливаю и распознаю аудио..."), limit, SESSION_FILES.lease(st.session_state):
                if not job_id and not st.session_state.get("audio_path"):
                    prepare_cloud_audio()
                transcription_service = TranscriptionService(AssemblyAIProvider(st.session_state.assemblyai_key))
                transcription_config = Config.get_transcription_config(st.session_state.language)
                def remember_job(identifier):
                    st.session_state.transcription_job_id = identifier
                    save_workflow()
                transcript = transcription_service.transcribe_audio(
                    st.session_state.audio_path, transcription_config,
                    transcript_id=job_id, on_submitted=remember_job,
                )
            st.session_state.transcript = transcript
            st.session_state.transcription_job_id = None
            st.session_state.step = max(st.session_state.get("step", 0), 2)
            st.toast("Транскрибация завершена.")
        except TranscriptionPending:
            pass  # Keep the provider job ID and show its pending status after rerun.
        except Exception as e:
            logger.exception("Transcription failed")
            if isinstance(e, TranscriptionFailed):
                st.session_state.transcription_job_id = None
            st.session_state.transcription_error = str(e) if isinstance(e, (LimitExceeded, MediaValidationError, TranscriptionFailed)) else "Не удалось распознать аудио. Файл сохранён; попробуйте позже."
        save_workflow()
        st.rerun()


def step_review_transcript_gate():
    if st.session_state.get("transcript") and st.session_state.get("show_transcription_before_summary") and not st.session_state.get("summary"):
        st.subheader("Проверьте транскрипцию перед саммари")
        st.text_area(
            "Транскрибированный текст (только чтение)",
            value=st.session_state.get("transcript") or "",
            height=350,
        )
        if st.button("➡️ Перейти к саммари", key="proceed_to_summary_button"):
            st.session_state.summary_started = True
            save_workflow()
            st.rerun()
        st.stop()


def step_summarize():
    st.subheader("Шаг 3 — Саммари транскрипции")

    if st.session_state.get("summary"):
        st.success("✅ Саммари уже сгенерировано")
        with st.expander("Предпросмотр саммари"):
            st.write(st.session_state.get("summary") or "")
        return

    disabled = not st.session_state.get("transcript") or (
        st.session_state.get("show_transcription_before_summary") and not st.session_state.get("summary_started")
    )

    if st.button("🤖 Сгенерировать саммари", disabled=disabled, key="generate_summary_button"):
        try:
            with st.spinner("Генерирую саммари с LLM..."), paid_job(st.session_state.authenticated_user):
                llm_config = Config.get_llm_config()
                llm_service = LLMService(
                    api_key=st.session_state.openrouter_key,
                    model=(st.session_state.openrouter_model or Config.DEFAULT_OPENROUTER_MODEL),
                    **llm_config,
                )
                summary = llm_service.summarize_text(
                    st.session_state.get("transcript") or "",
                    system_prompt=st.session_state.get("system_prompt"),
                )
            st.session_state.summary = summary
            st.session_state.results_tab_default = "📊 AI-саммари"
            st.session_state.step = max(st.session_state.get("step", 0), 3)
            save_workflow()
            st.toast("Саммари сгенерировано.")
        except Exception as e:
            logger.exception("Summarization failed")
            st.error(str(e) if isinstance(e, LimitExceeded) else "Не удалось создать саммари. Попробуйте позже.")
        st.rerun()


def section_results():
    if not st.session_state.get("summary") and not st.session_state.get("transcript"):
        return

    st.header("📋 Результаты")
    st.info(
        "😄 Чтобы скачать саммари, доскролль до конца страницы и после скачивания закрывай сайт: "
        "каждая минута, пока он открыт, стоит денег старшему брату."
    )
    tab_labels = ["📝 Полная транскрипция", "📊 AI-саммари"]
    default_tab = st.session_state.get("results_tab_default")
    if default_tab not in tab_labels:
        default_tab = None
    try:
        tab1, tab2 = st.tabs(tab_labels, default=default_tab)
    except TypeError:
        tab1, tab2 = st.tabs(tab_labels)
    if default_tab:
        # Apply one-time auto-switch right after summary generation.
        st.session_state.results_tab_default = None

    transcript_text = st.session_state.get("transcript") or ""
    summary_text = st.session_state.get("summary") or ""

    if transcript_text:
        with tab1:
            st.subheader("Транскрипция")
            st.text_area(
                "Транскрибированный текст",
                value=transcript_text,
                height=300,
            )
            st.markdown(
                build_download_link(
                    transcript_text, "transcript.txt", "text/plain", "📥 Скачать транскрипцию"
                ),
                unsafe_allow_html=True,
            )

    if summary_text:
        with tab2:
            st.subheader("AI-саммари")
            st.markdown(
                summary_text,
                unsafe_allow_html=False,
            )
            pdf_summary_bytes = None
            pdf_export_error = None
            try:
                summary_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
                if st.session_state.get("pdf_summary_hash") != summary_hash:
                    with processing_slot():
                        st.session_state["pdf_summary_bytes"] = build_summary_pdf_bytes(summary_text)
                    st.session_state["pdf_summary_hash"] = summary_hash
                pdf_summary_bytes = st.session_state["pdf_summary_bytes"]
            except Exception as e:
                logger.exception("Failed to build summary PDF")
                pdf_export_error = e

            pdf_col, txt_col = st.columns([1.4, 1])
            with pdf_col:
                if pdf_summary_bytes:
                    st.markdown(
                        build_download_link(
                            pdf_summary_bytes, "summary.pdf", "application/pdf",
                            "📥 Скачать саммари (.pdf)",
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("PDF-экспорт временно недоступен.")
                    if pdf_export_error:
                        st.caption("Попробуйте скачать текстовую версию.")
            with txt_col:
                st.markdown(
                    build_download_link(
                        summary_text, "summary.txt", "text/plain", "Скачать .txt (опционально)"
                    ),
                    unsafe_allow_html=True,
                )

    # Stats
    if transcript_text or summary_text:
        st.subheader("📊 Статистика")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Длина транскрипции", f"{len(transcript_text)} символов")
        with col2:
            wc = len(transcript_text.split())
            st.metric("Количество слов", f"{wc} слов")
        with col3:
            st.metric("Длина саммари", f"{len(summary_text)} символов")


# -------------------------
# App Entry
# -------------------------

def main():
    st.set_page_config(page_title="ИИ-помощник для учебы", page_icon="🎓", layout="wide")

    if not check_password():
        st.stop()

    logger.info("Starting Student AI Assistant application")

    # Initialize session state for the workflow.
    # This ensures all keys are present without resetting auth/config.
    initialize_session_state()
    if restore_workflow(st.session_state):
        st.toast("Предыдущая лекция восстановлена. Можно продолжить обработку.")

    # Load system prompt on first run or if it's empty
    if not st.session_state.get("system_prompt"):
        try:
            with open("data/system_prompt.md", "r") as f:
                st.session_state.system_prompt = f.read()
        except FileNotFoundError:
            logger.warning("System prompt file not found. Using a default prompt.")
            st.session_state.system_prompt = "Вы полезный помощник, который делает короткие и понятные саммари."

    st.title("🎓 ИИ-помощник для учебы")
    st.markdown("Загрузите небольшой файл или используйте режим «Большая загрузка» для крупных файлов.")

    sidebar_config()

    # Step 0: Upload & prepare
    step_upload_and_prepare()

    # Conditional UI based on progress
    if st.session_state.get("processing_started"):
        # Step 1: Extract audio (if video was uploaded)
        cloud_kind = (st.session_state.get("cloud_media") or {}).get("kind")
        if st.session_state.get("video_path") or cloud_kind == "video":
            step_extract_audio()

        # Step 2: Transcription (if audio is available)
        if st.session_state.get("audio_path") or cloud_kind == "audio" or st.session_state.get("transcription_job_id"):
            step_transcribe()

        # Gate for review-before-summary flow
        step_review_transcript_gate()

        # Step 3: Summarization (if transcript is available)
        if st.session_state.get("transcript"):
            step_summarize()

    # Results section
    section_results()
    save_workflow()


if __name__ == "__main__":
    logger.info("Application starting")
    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal error in main application: {str(e)}", exc_info=True)
        raise
