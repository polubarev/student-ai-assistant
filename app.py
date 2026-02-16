import streamlit as st
import streamlit.components.v1 as components
import os
import tempfile
from pathlib import Path
import hashlib
import json
import time
from datetime import datetime, timezone

from services.audio_service import AudioService, FFmpegAudioExtractor
from services.transcription_service import TranscriptionService, AssemblyAIProvider
from services.llm_service import LLMService
from services.storage_service import GCSStorageService
from config import Config
from utils.logger import get_logger, Logger
from utils.auth import check_password

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

    if mode == "put":
        upload_url_json = json.dumps(action_url)
        bucket_json = json.dumps(bucket_name)
        object_key_json = json.dumps(object_key)
        redirect_url_json = json.dumps(success_redirect_url)
        html = f"""
        <div style="border:1px solid #ddd;padding:12px;border-radius:8px">
          <div style="font-size:14px;margin-bottom:8px;">
            Загрузка большого файла (ссылка действует ограниченное время)
          </div>
          <input id="gcs-file" type="file" required />
          <button id="gcs-upload-btn" style="margin-left:8px;">Загрузить файл</button>
          <progress id="gcs-upload-progress" max="100" value="0" style="display:block;width:100%;margin-top:8px;" hidden></progress>
          <div id="gcs-upload-status" style="font-size:12px;color:#666;margin-top:8px;"></div>
        </div>
        <script>
          (function() {{
            const uploadUrl = {upload_url_json};
            const bucket = {bucket_json};
            const objectKey = {object_key_json};
            const redirectBase = {redirect_url_json};

            const btn = document.getElementById("gcs-upload-btn");
            const fileInput = document.getElementById("gcs-file");
            const status = document.getElementById("gcs-upload-status");
            const progress = document.getElementById("gcs-upload-progress");

            if (!btn || !fileInput || !status || !progress) {{
              return;
            }}

            function setStatus(text) {{
              status.textContent = text;
              try {{
                console.log("[GCS upload]", text);
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

              btn.disabled = true;
              progress.hidden = false;
              progress.max = 100;
              progress.value = 0;
              setStatus("Начинаю загрузку...");
              try {{
                await new Promise((resolve, reject) => {{
                  const xhr = new XMLHttpRequest();
                  xhr.timeout = 60 * 60 * 1000; // 1 hour for large uploads
                  xhr.open("PUT", uploadUrl, true);
                  xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");

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

                  const fallbackIndeterminateTimer = window.setTimeout(function() {{
                    if (!settled && !sawProgressEvent) {{
                      try {{
                        progress.removeAttribute("value");
                      }} catch (_e) {{
                        // no-op
                      }}
                      setStatus("Передача началась, но браузер не отдает детальный прогресс.");
                    }}
                  }}, 3000);

                  function settle(ok, errorMessage) {{
                    if (settled) {{
                      return;
                    }}
                    settled = true;
                    window.clearInterval(fallbackTimer);
                    window.clearTimeout(fallbackIndeterminateTimer);
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
                    if (xhr.status >= 200 && xhr.status < 300) {{
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
                  xhr.onloadend = function() {{
                    if (!settled) {{
                      setStatus("Загрузка завершилась с неопределенным состоянием.");
                    }}
                  }};

                  xhr.send(file);
                }});

                status.innerHTML =
                  "Файл загружен.<br/>" +
                  "Ниже на странице нажмите кнопку «Начать обработку файла»."
              }} catch (error) {{
                const rawMessage = (error && error.message) ? error.message : "";
                const prettyMessage = rawMessage === "Failed to fetch"
                  ? "браузер временно заблокировал загрузку"
                  : (rawMessage || "неизвестная ошибка");
                setStatus("Загрузка не удалась: " + prettyMessage +
                  ". Попробуйте еще раз.");
              }} finally {{
                btn.disabled = false;
              }}
            }});
          }})();
        </script>
        """
        components.html(html, height=220)
        return

    if mode == "post":
        upload_url_json = json.dumps(action_url)
        fields_json = json.dumps(fields)
        bucket_json = json.dumps(bucket_name)
        object_key_json = json.dumps(object_key)
        redirect_url_json = json.dumps(success_redirect_url)
        filename_placeholder_json = json.dumps("${filename}")
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
    root = Path(tempfile.gettempdir()) / f"vap_{os.getpid()}_{id(st.session_state)}"
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    reset_workflow()

    st.session_state.file_sig = signature
    st.session_state.input_name = file_name
    st.session_state.input_source = source_label
    st.session_state.input_size_bytes = size_bytes

    if file_kind == "video":
        st.session_state.video_path = str(local_path)
        st.session_state.processing_started = True
        return

    if file_kind == "audio":
        st.session_state.audio_path = str(local_path)
        st.session_state.step = 1  # Skip audio extraction.
        st.session_state.processing_started = True
        return

    if file_kind == "text":
        try:
            st.session_state.transcript = local_path.read_text(encoding="utf-8")
            st.session_state.step = 2  # Skip extraction and transcription.
            st.session_state.processing_started = True
            return
        except UnicodeDecodeError as exc:
            raise ValueError("Не удалось декодировать текстовый файл. Используйте кодировку UTF-8.") from exc

    raise ValueError("Неподдерживаемый тип файла.")


def load_from_gcs_uri(
    gcs_uri: str,
    original_name: str | None = None,
    wait_for_object_seconds: int = 0,
) -> tuple[bool, str | None]:
    """Download GCS object and ingest into workflow state."""
    try:
        logger.info(
            "Attempting to ingest GCS object: uri=%s, original_name=%s, wait_for_object_seconds=%s",
            gcs_uri,
            original_name or "",
            wait_for_object_seconds,
        )
        session_tmp_root = ensure_session_tmpdir()
        storage_service = GCSStorageService()
        bucket_name, blob_name = GCSStorageService.parse_gcs_uri(gcs_uri)
        local_name = Path(blob_name).name
        local_path = session_tmp_root / local_name
        wait_seconds = max(0, int(wait_for_object_seconds or 0))
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                meta = storage_service.download_to_path(gcs_uri, local_path)
                break
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    logger.warning("GCS object not found before deadline: %s", gcs_uri)
                    try:
                        parent_prefix = str(Path(blob_name).parent)
                        if parent_prefix == ".":
                            parent_prefix = ""
                        prefix = f"{parent_prefix}/" if parent_prefix else ""
                        nearby_objects = storage_service.list_object_names(
                            bucket_name=bucket_name,
                            prefix=prefix,
                            max_results=5,
                        )
                        if nearby_objects:
                            rendered = ", ".join(nearby_objects)
                            logger.info("Nearby uploaded objects for diagnostics: %s", rendered)
                    except Exception:
                        logger.exception("Failed to list nearby GCS objects for diagnostics")
                    return (
                        False,
                        "Файл пока не найден. Возможно, загрузка еще не завершилась. "
                        "Подождите несколько секунд и попробуйте снова.",
                    )
                time.sleep(2)

        file_name = (original_name or "").strip() or str(meta.get("name") or local_name)
        file_kind = detect_file_kind(file_name, str(meta.get("content_type", "")))
        if not file_kind:
            return False, "Этот формат файла пока не поддерживается."

        current_sig = source_signature(
            gcs_uri,
            str(meta.get("size", 0)),
            str(meta.get("updated", "")),
        )
        if current_sig == st.session_state.get("file_sig"):
            return True, "Этот файл уже открыт."

        ingest_prepared_file(
            local_path=local_path,
            file_name=file_name,
            file_kind=file_kind,
            signature=current_sig,
            source_label=SOURCE_MODE_LARGE,
            size_bytes=int(meta.get("size", 0) or 0),
        )
        logger.info(
            "GCS object ingested: uri=%s, file_name=%s, file_kind=%s, size_bytes=%s",
            gcs_uri,
            file_name,
            file_kind,
            int(meta.get("size", 0) or 0),
        )
        return True, None
    except Exception as e:
        logger.exception("GCS file preparation failed")
        return False, str(e)


def initialize_session_state():
    """Initialize session state with default values for a new workflow."""
    defaults = {
        "step": 0,
        "processing_started": False,
        "transcription_started": False,
        "summary_started": False,
        "audio_path": None,
        "video_path": None,
        "file_sig": None,
        "input_source_mode": SOURCE_MODE_LOCAL,
        "input_source": None,
        "input_name": None,
        "input_size_bytes": None,
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


def reset_workflow():
    """Reset workflow artifacts, preserving auth and config."""
    # Store settings and auth keys before clearing
    preserved_values = {
        "system_prompt": st.session_state.get("system_prompt"),
        "password_correct": st.session_state.get("password_correct"),
        "username": st.session_state.get("username"),
        "assemblyai_key": st.session_state.get("assemblyai_key"),
        "openrouter_key": st.session_state.get("openrouter_key") or st.session_state.get("openai_key"),
        "language": st.session_state.get("language"),
        "openrouter_model": st.session_state.get("openrouter_model") or st.session_state.get("openai_model"),
        "show_transcription_before_summary": st.session_state.get("show_transcription_before_summary"),
        "input_source_mode": st.session_state.get("input_source_mode"),
        "gcs_upload_form": st.session_state.get("gcs_upload_form"),
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
            assemblyai_key = Config.ASSEMBLYAI_API_KEY
            openrouter_key = Config.OPENROUTER_API_KEY
        else:
            if missing_keys:
                st.warning("⚠️ Приложение настроено не полностью.")
                st.info("Если вы пользователь, обратитесь к администратору.")
            assemblyai_key = st.text_input(
                "Ключ сервиса распознавания речи (для администратора)",
                type="password",
                value=st.session_state.get("assemblyai_key") or Config.ASSEMBLYAI_API_KEY or "",
                help="Служебная настройка",
            )
            openrouter_key = st.text_input(
                "Ключ сервиса ИИ (для администратора)",
                type="password",
                value=st.session_state.get("openrouter_key") or Config.OPENROUTER_API_KEY or "",
                help="Служебная настройка",
            )

        language_options = ["ru", "en", "es", "fr", "de", "it", "pt", "ja", "ko", "zh"]
        default_lang_index = (
            language_options.index(Config.DEFAULT_LANGUAGE)
            if getattr(Config, "DEFAULT_LANGUAGE", None) in language_options
            else 0
        )
        language = st.selectbox(
            "Язык транскрибации",
            language_options,
            index=default_lang_index,
            help="Выберите язык аудио для более точной транскрибации",
        )

        openrouter_model = st.text_input(
            "Модель ИИ (для администратора)",
            value=st.session_state.get("openrouter_model") or Config.DEFAULT_OPENROUTER_MODEL,
            help="Служебная настройка",
        )

        # New: system prompt editor
        system_prompt = st.text_area(
            "Инструкции для ИИ (для администратора)",
            value=st.session_state.get("system_prompt", ""),
            height=140,
            help="Служебная настройка",
        )

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

        st.divider()
        st.header("⚙️ Статус обработки")

        if st.session_state.get("processing_started") and st.session_state.get("input_name"):
            st.success("Файл загружен")
            st.caption(f"Файл: {st.session_state.get('input_name')}")
            st.caption(f"Способ загрузки: {st.session_state.get('input_source')}")
            if st.session_state.get("input_size_bytes") is not None:
                st.caption(f"Размер: {st.session_state.get('input_size_bytes') / (1024 * 1024):.2f} MB")

            if st.session_state.get("summary"):
                st.info("Саммари готово.")
            elif st.session_state.get("transcript"):
                st.info("Транскрипция готова. Можно делать саммари.")
            elif st.session_state.get("audio_path"):
                st.info("Аудио готово. Можно запускать транскрибацию.")
            elif st.session_state.get("video_path"):
                st.info("Видео загружено. Можно извлечь аудио.")
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

    redirect_bucket = st.query_params.get("bucket")
    redirect_key = st.query_params.get("key")
    redirect_original_name = st.query_params.get("original_name")
    if isinstance(redirect_bucket, list):
        redirect_bucket = redirect_bucket[0] if redirect_bucket else None
    if isinstance(redirect_key, list):
        redirect_key = redirect_key[0] if redirect_key else None
    if isinstance(redirect_original_name, list):
        redirect_original_name = redirect_original_name[0] if redirect_original_name else None
    if redirect_bucket and redirect_key:
        redirected_uri = f"gs://{redirect_bucket}/{redirect_key}"
        if redirected_uri != st.session_state.get("handled_redirect_key"):
            ok, message = load_from_gcs_uri(
                redirected_uri,
                original_name=redirect_original_name,
                wait_for_object_seconds=12,
            )
            if ok:
                st.session_state.handled_redirect_key = redirected_uri
                try:
                    st.query_params.clear()
                except Exception:
                    pass
                if not message:
                    st.toast("Файл загружен и готов к обработке.")
                    st.rerun()
                else:
                    st.info(message)
            else:
                st.error(f"❌ Не удалось открыть загруженный файл. {message}")

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
                        try:
                            session_tmp_root = ensure_session_tmpdir()
                            local_path = session_tmp_root / Path(uploaded_file.name).name
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
                            logger.exception("Local file preparation failed")
                            st.error("❌ Не удалось открыть загруженный файл. Попробуйте еще раз.")
                    else:
                        st.info("Этот файл уже загружен.")
    else:
        upload_bucket = (getattr(Config, "GCS_UPLOAD_BUCKET", "") or "").removeprefix("gs://")
        if has_direct_gcs_upload_config():
            st.subheader("Большая загрузка файла")
            st.caption("Подготовьте загрузку, затем выберите файл и нажмите «Загрузить файл».")
            if st.button("Подготовить загрузку", key="prepare_direct_gcs_upload"):
                try:
                    with st.spinner("Подготавливаю загрузку..."):
                        key_prefix = f"uploads/{id(st.session_state)}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}/"
                        storage_service = GCSStorageService()
                        form_data = storage_service.create_signed_upload_form(
                            bucket_name=upload_bucket,
                            key_prefix=key_prefix,
                            success_redirect_url=build_gcs_upload_redirect_url(),
                        )
                        logger.info(
                            "Prepared direct GCS upload form: mode=%s, bucket=%s, key_prefix=%s, object_key=%s, expires_at=%s",
                            form_data.get("mode"),
                            form_data.get("bucket_name"),
                            form_data.get("key_prefix", ""),
                            form_data.get("object_key", ""),
                            form_data.get("expires_at"),
                        )
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

                with st.expander("Проблема с загрузкой? Открыть режим поддержки"):
                    st.caption("Этот раздел нужен только если обычная кнопка выше не сработала.")
                    manual_uri = st.text_input(
                        "Техническая ссылка на файл",
                        placeholder=f"gs://{upload_bucket}/path/to/file.m4a",
                        key="manual_gcs_uri_input",
                    ).strip()
                    if st.button("Открыть файл по ссылке", key="ingest_manual_gcs_uri", disabled=not manual_uri):
                        with st.spinner("Открываю файл..."):
                            ok, message = load_from_gcs_uri(manual_uri, wait_for_object_seconds=20)
                        if ok and not message:
                            st.session_state.handled_redirect_key = manual_uri
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

    disabled = not st.session_state.get("processing_started") or not st.session_state.get("video_path")

    if st.button("🎵 Извлечь аудио из видео", disabled=disabled, key="extract_audio_button"):
        try:
            with st.spinner("Извлекаю аудио..."):
                session_tmp_root = ensure_session_tmpdir()
                audio_path = Path(session_tmp_root) / "audio.wav"
                audio_service = AudioService(FFmpegAudioExtractor())
                ok = audio_service.extract_audio_from_video(str(st.session_state.video_path), str(audio_path))
                if not ok:
                    st.error("Не удалось обработать видеофайл. Попробуйте другой файл.")
                    return
                st.session_state.audio_path = str(audio_path)
                st.session_state.step = max(st.session_state.get("step", 0), 1)
            st.toast("Аудио извлечено.")
        except Exception as e:
            logger.exception("Audio extraction failed")
            st.error(f"❌ Ошибка при извлечении аудио: {e}")
        st.rerun()


def step_transcribe():
    st.subheader("Шаг 2 — Транскрибация аудио")

    if st.session_state.get("transcript"):
        st.success("✅ Транскрибация уже готова")
        with st.expander("Предпросмотр транскрипции"):
            st.write((st.session_state.get("transcript") or "")[:1000] + ("..." if len(st.session_state.get("transcript") or "") > 1000 else ""))
        return

    disabled = not st.session_state.get("audio_path")

    if st.button("📝 Транскрибировать аудио", disabled=disabled, key="transcribe_audio_button"):
        try:
            with st.spinner("Транскрибирую..."):
                transcription_service = TranscriptionService(AssemblyAIProvider(st.session_state.assemblyai_key))
                transcription_config = Config.get_transcription_config(st.session_state.language)
                transcript = transcription_service.transcribe_audio(st.session_state.audio_path, transcription_config)
            st.session_state.transcript = transcript
            st.session_state.step = max(st.session_state.get("step", 0), 2)
            st.toast("Транскрибация завершена.")
        except Exception as e:
            logger.exception("Transcription failed")
            st.error(f"❌ Ошибка при транскрибации: {e}")
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
            with st.spinner("Генерирую саммари с LLM..."):
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
            st.session_state.step = max(st.session_state.get("step", 0), 3)
            st.toast("Саммари сгенерировано.")
        except Exception as e:
            logger.exception("Summarization failed")
            st.error(f"❌ Ошибка при суммаризации: {e}")
        st.rerun()


def section_results():
    if not st.session_state.get("summary") and not st.session_state.get("transcript"):
        return

    st.header("📋 Результаты")
    tab1, tab2 = st.tabs(["📝 Полная транскрипция", "📊 AI-саммари"])

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
            st.download_button(
                label="📥 Скачать транскрипцию",
                data=transcript_text,
                file_name="transcript.txt",
                mime="text/plain",
                key="download_transcript",
            )

    if summary_text:
        with tab2:
            st.subheader("AI-саммари")
            st.markdown(
                summary_text,
                unsafe_allow_html=True,
            )
            st.download_button(
                label="📥 Скачать саммари",
                data=summary_text,
                file_name="summary.txt",
                mime="text/plain",
                key="download_summary",
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
        if st.session_state.get("video_path"):
            step_extract_audio()

        # Step 2: Transcription (if audio is available)
        if st.session_state.get("audio_path"):
            step_transcribe()

        # Gate for review-before-summary flow
        step_review_transcript_gate()

        # Step 3: Summarization (if transcript is available)
        if st.session_state.get("transcript"):
            step_summarize()

    # Results section
    section_results()


if __name__ == "__main__":
    logger.info("Application starting")
    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal error in main application: {str(e)}", exc_info=True)
        raise
