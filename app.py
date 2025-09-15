import streamlit as st
import os
import tempfile
from pathlib import Path
import time

from services.audio_service import AudioService, FFmpegAudioExtractor
from services.transcription_service import TranscriptionService, AssemblyAIProvider
from services.llm_service import LLMService
from config import Config
from utils.logger import get_logger, Logger

# Initialize logging
Logger.setup_logging(
    log_level=os.getenv("LOG_LEVEL", "INFO"),
    log_file=os.getenv("LOG_FILE", "logs/app.log")
)
logger = get_logger(__name__)


def main():
    # --- persist workflow flags across reruns ---
    st.session_state.setdefault("processing_started", False)
    st.session_state.setdefault("transcription_started", False)
    st.session_state.setdefault("audio_path", None)
    st.session_state.setdefault("video_bytes", None)
    st.session_state.setdefault("video_name", None)
    st.session_state.setdefault("assemblyai_key", None)
    st.session_state.setdefault("openai_key", None)
    st.session_state.setdefault("language", None)
    st.session_state.setdefault("openai_model", None)
    st.session_state.setdefault("show_transcription_before_summary", False)
    st.session_state.setdefault("transcription_displayed", False)

    logger.info("Starting Video Audio Processor application")
    
    st.set_page_config(
        page_title="Video Audio Processor",
        page_icon="🎥",
        layout="wide"
    )
    
    st.title("🎥 Video Audio Processor")
    st.markdown("Upload a video file to extract audio, transcribe it, and get an AI-generated summary.")
    
    logger.info("Application UI initialized")
    
    # Sidebar for configuration
    with st.sidebar:
        st.header("Configuration")
        
        # Check if API keys are available from environment
        has_env_keys, missing_keys = Config.validate_api_keys()
        logger.info(f"API key validation: has_keys={has_env_keys}, missing={missing_keys}")
        
        if has_env_keys:
            st.success("✅ API keys loaded from environment")
            assemblyai_key = Config.ASSEMBLYAI_API_KEY
            openai_key = Config.OPENAI_API_KEY
            logger.info("API keys loaded from environment variables")
        else:
            st.warning(f"⚠️ Missing environment variables: {', '.join(missing_keys)}")
            st.info("💡 You can set these in a .env file or enter them below")
            logger.warning(f"Missing API keys: {missing_keys}")
            
            # API Keys input fields
            assemblyai_key = st.text_input(
                "AssemblyAI API Key",
                type="password",
                value=Config.ASSEMBLYAI_API_KEY or "",
                help="Enter your AssemblyAI API key for transcription"
            )
            
            openai_key = st.text_input(
                "OpenAI API Key", 
                type="password",
                value=Config.OPENAI_API_KEY or "",
                help="Enter your OpenAI API key for text processing"
            )
        
        # Language selection
        language_options = ["ru", "en", "es", "fr", "de", "it", "pt", "ja", "ko", "zh"]
        default_lang_index = language_options.index(Config.DEFAULT_LANGUAGE) if Config.DEFAULT_LANGUAGE in language_options else 0
        
        language = st.selectbox(
            "Transcription Language",
            language_options,
            index=default_lang_index,
            help="Select the language of the audio for better transcription accuracy"
        )
        
        # Model selection
        model_options = ["gpt-5-mini", "gpt-4o"]
        default_model_index = model_options.index(Config.DEFAULT_OPENAI_MODEL) if Config.DEFAULT_OPENAI_MODEL in model_options else 0
        
        openai_model = st.selectbox(
            "OpenAI Model",
            model_options,
            index=default_model_index,
            help="Select the OpenAI model for text processing"
        )
        
        st.session_state.show_transcription_before_summary = st.checkbox(
            "Show transcription before summary",
            value=st.session_state.show_transcription_before_summary,
            help="If checked, the full transcription will be displayed after processing, allowing you to review it before generating the AI summary."
        )
    
    # Main content area
    col1, col2 = st.columns([1, 1])

    with col1:
        st.header("📁 Upload Video")
        
        uploaded_file = st.file_uploader(
            "Choose a video file",
            type=['mp4', 'avi', 'mov', 'mkv', 'wmv', 'flv', 'webm'],
            help="Supported formats: MP4, AVI, MOV, MKV, WMV, FLV, WebM"
        )
        
        if uploaded_file is not None:
            st.success(f"File uploaded: {uploaded_file.name}")
            st.info(f"File size: {uploaded_file.size / (1024*1024):.2f} MB")
            logger.info(f"File uploaded: {uploaded_file.name}, size: {uploaded_file.size} bytes")

    with col2:
        st.header("⚙️ Processing Status")
        
        if uploaded_file is not None:
            if not assemblyai_key:
                st.error("Please enter your AssemblyAI API key in the sidebar")
            elif not openai_key:
                st.error("Please enter your OpenAI API key in the sidebar")
            else:
                # Phase 0: user initiates processing. Store inputs in session, then rerun.
                if st.button("🚀 Process Video", type="primary", key="btn_process"):
                    logger.info(f"Starting video processing for file: {uploaded_file.name}")
                    st.session_state.processing_started = True
                    st.session_state.transcription_started = False
                    st.session_state.audio_path = None
                    st.session_state.video_bytes = uploaded_file.getvalue()
                    st.session_state.video_name = uploaded_file.name
                    st.session_state.assemblyai_key = assemblyai_key
                    st.session_state.openai_key = openai_key
                    st.session_state.language = language
                    st.session_state.openai_model = openai_model
                    st.rerun()
        else:
            st.info("Upload a video file to begin processing")

    # If processing has started, continue the workflow on every rerun
    if st.session_state.get("processing_started"):
        process_video(
            video_name=st.session_state.video_name,
            video_bytes=st.session_state.video_bytes,
            assemblyai_key=st.session_state.assemblyai_key,
            openai_key=st.session_state.openai_key,
            language=st.session_state.language,
            openai_model=st.session_state.openai_model
        )


def process_video(video_name, video_bytes, assemblyai_key, openai_key, language, openai_model):
    """Process the uploaded video through the complete pipeline with session-persisted phases."""
    start_time = time.time()
    logger.info(f"Processing video: {video_name} with language: {language}, model: {openai_model}")

    # UI elements survive reruns logically, not physically; recreate each run
    progress_bar = st.progress(0)
    status_text = st.empty()

    try:
        # Create (or reuse) a durable per-session temp folder so paths survive reruns
        session_tmp_root = Path(tempfile.gettempdir()) / f"vap_{os.getpid()}_{id(st.session_state)}"
        session_tmp_root.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Session temp dir: {session_tmp_root}")

        # Persist the uploaded video to disk if it's not already there
        video_path = session_tmp_root / video_name
        if not video_path.exists():
            with open(video_path, "wb") as f:
                f.write(video_bytes)
            logger.info(f"Saved uploaded file to: {video_path}")

        # Step 1: Extract audio once; cache path in session state
        if not st.session_state.get("audio_path"):
            status_text.text("🎵 Extracting audio from video...")
            progress_bar.progress(20)
            logger.info("Starting audio extraction")

            audio_path = session_tmp_root / "audio.wav"
            audio_service = AudioService(FFmpegAudioExtractor())
            if not audio_service.extract_audio_from_video(str(video_path), str(audio_path)):
                logger.error("Failed to extract audio from video")
                st.error("Failed to extract audio from video. Please check if FFmpeg is installed.")
                st.session_state.processing_started = False
                return

            st.session_state.audio_path = str(audio_path)
            logger.info("Audio extraction completed")
            st.success("✅ Audio extracted successfully")

        # Preview & confirm
        st.subheader("🎧 Listen to Extracted Audio")
        st.info("Listen to the extracted audio below. If you are satisfied, click the button to proceed with transcription and summarization.")
        st.audio(st.session_state.audio_path, format='audio/wav')

        if not st.session_state.get("transcription_started", False):
            if st.button("📝 Yes, Proceed with Transcription and Summarization", type="primary", key="btn_confirm_transcribe"):
                st.session_state.transcription_started = True
                st.rerun()
            # Wait for user confirmation
            return

        # Step 2: Transcribe
        with st.spinner("Transcription and summarization in progress..."):
            status_text.text("🎤 Transcribing audio...")
            progress_bar.progress(50)
            logger.info("Starting audio transcription")

            transcription_service = TranscriptionService(AssemblyAIProvider(assemblyai_key))
            transcription_config = Config.get_transcription_config(language)
            transcript = transcription_service.transcribe_audio(st.session_state.audio_path, transcription_config)
            logger.info(f"Transcription completed, length: {len(transcript)} characters")
            st.success("✅ Audio transcribed successfully")

            # Store transcript in session state
            st.session_state.transcript = transcript

            # Step 2.5: Optionally display transcription before summary
            if st.session_state.show_transcription_before_summary and not st.session_state.get("summary_started", False):
                status_text.text("📝 Reviewing transcription...")
                st.subheader("📝 Full Transcription")
                st.text_area(
                    "Review the transcribed text below. Click 'Proceed to Summary' when ready.",
                    value=transcript,
                    height=400,
                    help="Full transcription of the audio from your video"
                )
                if st.button("➡️ Proceed to Summary", type="primary", key="btn_proceed_summary"):
                    st.session_state.summary_started = True
                    st.rerun()
                return # Stop execution here, wait for user to proceed

            # Step 3: Summarize (only if transcription reviewed or option is off)
            if not st.session_state.show_transcription_before_summary or st.session_state.get("summary_started", False):
                status_text.text("🤖 Generating summary with AI...")
                progress_bar.progress(80)
                logger.info("Starting LLM processing for summary generation")

                openai_config = Config.get_openai_config()
                llm_service = LLMService(api_key=openai_key, model=openai_model, **openai_config)
                summary = llm_service.summarize_text(st.session_state.transcript)
                logger.info(f"LLM processing completed, summary length: {len(summary)} characters")

                progress_bar.progress(100)
                status_text.text("✅ Processing complete!")

                total_duration = time.time() - start_time
                logger.info(f"Total processing time: {total_duration:.2f}s")

                # Display results
                display_results(st.session_state.transcript, summary, total_duration)

    except Exception as e:
        logger.error(f"Error during video processing: {str(e)}", exc_info=True)
        st.error(f"❌ Error during processing: {str(e)}")
        progress_bar.progress(0)
        st.session_state.processing_started = False
        status_text.text("Processing failed")


def display_results(transcript, summary, total_duration):
    """Display the transcription and summary results."""
    logger.info("Displaying processing results")
    
    st.header("📋 Results")
    
    # Create tabs for transcript and summary
    tab1, tab2 = st.tabs(["📝 Full Transcript", "📊 AI Summary"])
    
    with tab1:
        st.subheader("Transcription")
        st.text_area(
            "Transcribed Text",
            value=transcript,
            height=300,
            help="Full transcription of the audio from your video"
        )
        
        # Download button for transcript
        st.download_button(
            label="📥 Download Transcript",
            data=transcript,
            file_name="transcript.txt",
            mime="text/plain"
        )
    
    with tab2:
        st.subheader("AI-Generated Summary")
        st.text_area(
            "Summary",
            value=summary,
            height=300,
            help="AI-generated summary of the transcribed content"
        )
        
        # Download button for summary
        st.download_button(
            label="📥 Download Summary",
            data=summary,
            file_name="summary.txt",
            mime="text/plain"
        )
    
    # Statistics
    st.subheader("📊 Statistics")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Transcript Length", f"{len(transcript)} characters")
    
    with col2:
        st.metric("Word Count", f"{len(transcript.split())} words")
    
    with col3:
        st.metric("Summary Length", f"{len(summary)} characters")
    
    with col4:
        st.metric("Processing Time", f"{total_duration:.2f}s")


if __name__ == "__main__":
    logger.info("Application starting")
    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal error in main application: {str(e)}", exc_info=True)
        raise
