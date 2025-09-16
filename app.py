import streamlit as st
import os
import tempfile
from pathlib import Path
import time
import hashlib

from services.audio_service import AudioService, FFmpegAudioExtractor
from services.transcription_service import TranscriptionService, AssemblyAIProvider
from services.llm_service import LLMService
from config import Config
from utils.logger import get_logger, Logger

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

def file_signature(name: str, data: bytes) -> str:
    h = hashlib.sha256()
    h.update(name.encode("utf-8"))
    h.update(b"::")
    h.update(data)
    return h.hexdigest()[:16]


def ensure_session_tmpdir() -> Path:
    root = Path(tempfile.gettempdir()) / f"vap_{os.getpid()}_{id(st.session_state)}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def reset_workflow(preserve_keys: bool = True):
    """Reset all workflow artifacts but optionally keep API/model settings."""
    keep = {}
    if preserve_keys:
        for k in [
            "assemblyai_key",
            "openai_key",
            "language",
            "openai_model",
            "system_prompt",
            "show_transcription_before_summary",
        ]:
            keep[k] = st.session_state.get(k)

    # Store system prompt before clearing
    system_prompt_content = st.session_state.get("system_prompt")

    st.session_state.clear()

    # Restore preserved values and base state
    st.session_state.update({
        "step": 0,
        "processing_started": False,
        "transcription_started": False,
        "summary_started": False,
        "audio_path": None,
        "video_bytes": None,
        "video_name": None,
        "video_path": None,
        "file_sig": None,
        "transcript": None,
        "summary": None,
        "transcription_displayed": False,
        **keep,
    })
    if system_prompt_content:
        st.session_state.system_prompt = system_prompt_content


# -------------------------
# UI Sections
# -------------------------

def sidebar_config():
    with st.sidebar:
        st.header("Configuration")

        has_env_keys, missing_keys = Config.validate_api_keys()
        logger.info(f"API key validation: has_keys={has_env_keys}, missing={missing_keys}")

        if has_env_keys:
            st.success("✅ API keys loaded from environment")
            assemblyai_key = Config.ASSEMBLYAI_API_KEY
            openai_key = Config.OPENAI_API_KEY
        else:
            if missing_keys:
                st.warning(f"⚠️ Missing environment variables: {', '.join(missing_keys)}")
                st.info("💡 You can set these in a .env file or enter them below")
            assemblyai_key = st.text_input(
                "AssemblyAI API Key",
                type="password",
                value=st.session_state.get("assemblyai_key") or Config.ASSEMBLYAI_API_KEY or "",
                help="Enter your AssemblyAI API key for transcription",
            )
            openai_key = st.text_input(
                "OpenAI API Key",
                type="password",
                value=st.session_state.get("openai_key") or Config.OPENAI_API_KEY or "",
                help="Enter your OpenAI API key for text processing",
            )

        language_options = ["ru", "en", "es", "fr", "de", "it", "pt", "ja", "ko", "zh"]
        default_lang_index = (
            language_options.index(Config.DEFAULT_LANGUAGE)
            if getattr(Config, "DEFAULT_LANGUAGE", None) in language_options
            else 0
        )
        language = st.selectbox(
            "Transcription Language",
            language_options,
            index=default_lang_index,
            help="Select the language of the audio for better transcription accuracy",
        )

        model_options = ["gpt-5-mini", "gpt-4o"]
        default_model_index = (
            model_options.index(Config.DEFAULT_OPENAI_MODEL)
            if getattr(Config, "DEFAULT_OPENAI_MODEL", None) in model_options
            else 0
        )
        openai_model = st.selectbox(
            "OpenAI Model",
            model_options,
            index=default_model_index,
            help="Select the OpenAI model for text processing",
        )

        # New: system prompt editor
        system_prompt = st.text_area(
            "System Prompt",
            value=st.session_state.get("system_prompt", ""),
            height=140,
            help="Customize the system prompt used by the summarizer",
        )

        # New: checkbox to show transcription before summary
        show_before = st.checkbox(
            "Show transcription before summary",
            value=st.session_state.get("show_transcription_before_summary", False),
            help="Review transcription before summarization",
        )

        # Persist selections
        st.session_state.assemblyai_key = assemblyai_key
        st.session_state.openai_key = openai_key
        st.session_state.language = language
        st.session_state.openai_model = openai_model
        st.session_state.system_prompt = system_prompt
        st.session_state.show_transcription_before_summary = show_before


# -------------------------
# Core Steps
# -------------------------

def step_upload_and_prepare():
    st.header("📁 Upload File")
    uploaded_file = st.file_uploader(
        "Choose a video, audio, or transcript file",
        type=["mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "mp3", "wav", "m4a", "txt"],
        help="Supported formats: Video (mp4, avi, ...), Audio (mp3, wav, ...), Text (txt)",
    )

    if uploaded_file is not None:
        st.success(f"File uploaded: {uploaded_file.name}")
        st.info(f"File size: {uploaded_file.size / (1024*1024):.2f} MB")

        data = uploaded_file.getvalue()
        sig = file_signature(uploaded_file.name, data)

        if st.session_state.get("file_sig") and st.session_state.file_sig != sig:
            reset_workflow(preserve_keys=True)
            st.toast("New file detected. Workflow reset.")

        st.session_state.file_sig = sig
        file_type = uploaded_file.type

        # Reset paths and content
        st.session_state.video_path = None
        st.session_state.audio_path = None
        st.session_state.transcript = None
        st.session_state.video_bytes = None
        st.session_state.video_name = None

        session_tmp_root = ensure_session_tmpdir()

        if "video" in file_type:
            st.session_state.video_bytes = data
            st.session_state.video_name = uploaded_file.name
            video_path = session_tmp_root / st.session_state.video_name
            if not video_path.exists():
                with open(video_path, "wb") as f:
                    f.write(st.session_state.video_bytes)
            st.session_state.video_path = str(video_path)
            st.session_state.step = 0
            st.session_state.processing_started = True
            st.success("Video loaded. Ready for audio extraction.")

        elif "audio" in file_type:
            audio_path = session_tmp_root / uploaded_file.name
            if not audio_path.exists():
                with open(audio_path, "wb") as f:
                    f.write(data)
            st.session_state.audio_path = str(audio_path)
            st.session_state.step = 1  # Skip audio extraction
            st.session_state.processing_started = True
            st.success("Audio loaded. Ready for transcription.")

        elif "text" in file_type:
            try:
                transcript = data.decode("utf-8")
                st.session_state.transcript = transcript
                st.session_state.step = 2  # Skip extraction and transcription
                st.session_state.processing_started = True
                st.success("Transcript loaded. Ready for summarization.")
            except UnicodeDecodeError:
                st.error("Could not decode the text file. Please ensure it is UTF-8 encoded.")
                return

    with st.container():
        st.header("⚙️ Processing Status")

        if not st.session_state.get("processing_started"):
            st.info("Upload a file to begin.")
            return

        if not st.session_state.get("assemblyai_key") and st.session_state.get("step", 0) < 2:
            st.error("Please enter your AssemblyAI API key in the sidebar for transcription.")
            return
        if not st.session_state.get("openai_key"):
            st.error("Please enter your OpenAI API key in the sidebar for summarization.")
            return

        if st.button("🔄 Start Over", help="Reset the workflow"):
            reset_workflow(preserve_keys=True)
            st.rerun()


def step_extract_audio():
    st.subheader("Step 1 — Extract Audio")
    disabled = not st.session_state.get("processing_started") or not st.session_state.get("video_path")

    # Preview: nothing heavy yet
    if st.session_state.get("audio_path"):
        st.success("✅ Audio already extracted")
        st.audio(st.session_state.audio_path, format="audio/wav")

    if st.button("🎵 Extract audio from video", disabled=disabled):
        try:
            session_tmp_root = ensure_session_tmpdir()
            audio_path = Path(session_tmp_root) / "audio.wav"
            audio_service = AudioService(FFmpegAudioExtractor())
            ok = audio_service.extract_audio_from_video(str(st.session_state.video_path), str(audio_path))
            if not ok:
                st.error("Failed to extract audio from video. Please check if FFmpeg is installed.")
                return
            st.session_state.audio_path = str(audio_path)
            st.session_state.step = max(st.session_state.get("step", 0), 1)
            st.toast("Audio extracted.")
            st.rerun()
        except Exception as e:
            logger.exception("Audio extraction failed")
            st.error(f"❌ Error extracting audio: {e}")


def step_transcribe():
    st.subheader("Step 2 — Transcribe Audio")
    disabled = not st.session_state.get("audio_path")

    if st.session_state.get("transcript"):
        st.success("✅ Already transcribed")
        with st.expander("Preview transcript"):
            st.write((st.session_state.get("transcript") or "")[:1000] + ("..." if len(st.session_state.get("transcript") or "") > 1000 else ""))

    if st.button("📝 Transcribe audio", disabled=disabled):
        try:
            with st.spinner("Transcribing..."):
                transcription_service = TranscriptionService(AssemblyAIProvider(st.session_state.assemblyai_key))
                transcription_config = Config.get_transcription_config(st.session_state.language)
                transcript = transcription_service.transcribe_audio(st.session_state.audio_path, transcription_config)
            st.session_state.transcript = transcript
            st.session_state.step = max(st.session_state.get("step", 0), 2)
            st.toast("Transcription complete.")
            st.rerun()
        except Exception as e:
            logger.exception("Transcription failed")
            st.error(f"❌ Error during transcription: {e}")


def step_review_transcript_gate():
    if st.session_state.get("transcript") and st.session_state.get("show_transcription_before_summary") and not st.session_state.get("summary"):
        st.subheader("Review Transcription Before Summary")
        st.text_area(
            "Transcribed Text (read-only)",
            value=st.session_state.get("transcript") or "",
            height=350,
        )
        if st.button("➡️ Proceed to Summary"):
            st.session_state.summary_started = True
            st.rerun()
        st.stop()


def step_summarize():
    st.subheader("Step 3 — Summarize Transcript")
    disabled = not st.session_state.get("transcript") or (
        st.session_state.get("show_transcription_before_summary") and not st.session_state.get("summary_started")
    )

    if st.session_state.get("summary"):
        st.success("✅ Summary already generated")
        with st.expander("Preview summary"):
            st.write(st.session_state.get("summary") or "")

    if st.button("🤖 Generate summary", disabled=disabled):
        try:
            with st.spinner("Summarizing with LLM..."):
                openai_config = Config.get_openai_config()
                llm_service = LLMService(
                    api_key=st.session_state.openai_key,
                    model=st.session_state.openai_model,
                    **openai_config,
                )
                # Try passing system prompt if supported
                try:
                    summary = llm_service.summarize_text(
                        st.session_state.get("transcript") or "",
                        system_prompt=st.session_state.get("system_prompt"),
                    )
                except TypeError:
                    # Fallback for older LLMService signature
                    summary = llm_service.summarize_text(st.session_state.get("transcript") or "")
            st.session_state.summary = summary
            st.session_state.step = max(st.session_state.get("step", 0), 3)
            st.toast("Summary generated.")
            st.rerun()
        except Exception as e:
            logger.exception("Summarization failed")
            st.error(f"❌ Error during summarization: {e}")


def section_results():
    if not st.session_state.get("summary") and not st.session_state.get("transcript"):
        return

    st.header("📋 Results")
    tab1, tab2 = st.tabs(["📝 Full Transcript", "📊 AI Summary"])

    transcript_text = st.session_state.get("transcript") or ""
    summary_text = st.session_state.get("summary") or ""

    if transcript_text:
        with tab1:
            st.subheader("Transcription")
            st.text_area(
                "Transcribed Text",
                value=transcript_text,
                height=300,
            )
            st.download_button(
                label="📥 Download Transcript",
                data=transcript_text,
                file_name="transcript.txt",
                mime="text/plain",
                key="download_transcript",
            )

    if summary_text:
        with tab2:
            st.subheader("AI-Generated Summary")
            st.text_area(
                "Summary",
                value=summary_text,
                height=300,
            )
            st.download_button(
                label="📥 Download Summary",
                data=summary_text,
                file_name="summary.txt",
                mime="text/plain",
                key="download_summary",
            )

    # Stats
    if transcript_text or summary_text:
        st.subheader("📊 Statistics")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Transcript Length", f"{len(transcript_text)} characters")
        with col2:
            wc = len(transcript_text.split())
            st.metric("Word Count", f"{wc} words")
        with col3:
            st.metric("Summary Length", f"{len(summary_text)} characters")


# -------------------------
# App Entry
# -------------------------

def main():
    logger.info("Starting Student AI Assistant application")

    # Base state init
    if "step" not in st.session_state:
        reset_workflow(preserve_keys=False)  # Full reset on first load
        with open("data/system_prompt.md", "r") as f:
            st.session_state.system_prompt = f.read()

    st.set_page_config(page_title="Student AI Assistant", page_icon="🎓", layout="wide")
    st.title("🎓 Student AI Assistant")
    st.markdown("Upload a video, audio, or transcript to get started.")

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
