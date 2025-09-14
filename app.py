import streamlit as st
import os
import tempfile
from pathlib import Path
import time

from services.audio_service import AudioService, FFmpegAudioExtractor
from services.transcription_service import TranscriptionService, AssemblyAIProvider
from services.llm_service import LLMService, OpenAIProvider
from config import Config
from utils.logger import get_logger, Logger

# Initialize logging
Logger.setup_logging(
    log_level=os.getenv("LOG_LEVEL", "INFO"),
    log_file=os.getenv("LOG_FILE", "logs/app.log")
)
logger = get_logger(__name__)


def main():
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
                if st.button("🚀 Process Video", type="primary"):
                    logger.info(f"Starting video processing for file: {uploaded_file.name}")
                    process_video(uploaded_file, assemblyai_key, openai_key, language, openai_model)
        else:
            st.info("Upload a video file to begin processing")


def process_video(uploaded_file, assemblyai_key, openai_key, language, openai_model):
    """Process the uploaded video through the complete pipeline."""
    start_time = time.time()
    logger.info(f"Processing video: {uploaded_file.name} with language: {language}, model: {openai_model}")
    
    # Create temporary directory for processing
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        logger.debug(f"Created temporary directory: {temp_path}")
        
        # Save uploaded file
        video_path = temp_path / uploaded_file.name
        with open(video_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        logger.info(f"Saved uploaded file to: {video_path}")
        
        # Initialize progress tracking
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        try:
            # Step 1: Extract audio
            status_text.text("🎵 Extracting audio from video...")
            progress_bar.progress(20)
            logger.info("Starting audio extraction")
            
            audio_path = temp_path / "audio.wav"
            audio_service = AudioService(FFmpegAudioExtractor())
            
            audio_start_time = time.time()
            if not audio_service.extract_audio_from_video(str(video_path), str(audio_path)):
                logger.error("Failed to extract audio from video")
                st.error("Failed to extract audio from video. Please check if FFmpeg is installed.")
                return
            
            audio_duration = time.time() - audio_start_time
            logger.info(f"Audio extraction completed in {audio_duration:.2f}s")
            st.success("✅ Audio extracted successfully")
            
            # Step 2: Transcribe audio
            status_text.text("🎤 Transcribing audio...")
            progress_bar.progress(50)
            logger.info("Starting audio transcription")
            
            transcription_service = TranscriptionService(
                AssemblyAIProvider(assemblyai_key)
            )
            
            transcription_config = Config.get_transcription_config(language)
            logger.debug(f"Transcription config: {transcription_config}")
            
            transcription_start_time = time.time()
            transcript = transcription_service.transcribe_audio(
                str(audio_path), 
                transcription_config
            )
            
            transcription_duration = time.time() - transcription_start_time
            logger.info(f"Transcription completed in {transcription_duration:.2f}s, length: {len(transcript)} characters")
            st.success("✅ Audio transcribed successfully")
            
            # Step 3: Process with LLM
            status_text.text("🤖 Generating summary with AI...")
            progress_bar.progress(80)
            logger.info("Starting LLM processing for summary generation")
            
            openai_config = Config.get_openai_config()
            logger.debug(f"OpenAI config: {openai_config}")
            
            llm_service = LLMService(
                OpenAIProvider(api_key=openai_key, model=openai_model, **openai_config)
            )
            
            llm_start_time = time.time()
            summary = llm_service.summarize_text(transcript)
            
            llm_duration = time.time() - llm_start_time
            logger.info(f"LLM processing completed in {llm_duration:.2f}s, summary length: {len(summary)} characters")
            
            total_duration = time.time() - start_time
            logger.info(f"Total processing time: {total_duration:.2f}s")
            
            progress_bar.progress(100)
            status_text.text("✅ Processing complete!")
            
            # Display results
            display_results(transcript, summary)
            
        except Exception as e:
            logger.error(f"Error during video processing: {str(e)}", exc_info=True)
            st.error(f"❌ Error during processing: {str(e)}")
            progress_bar.progress(0)
            status_text.text("Processing failed")


def display_results(transcript, summary):
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
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Transcript Length", f"{len(transcript)} characters")
    
    with col2:
        st.metric("Word Count", f"{len(transcript.split())} words")
    
    with col3:
        st.metric("Summary Length", f"{len(summary)} characters")


if __name__ == "__main__":
    logger.info("Application starting")
    try:
        main()
    except Exception as e:
        logger.critical(f"Fatal error in main application: {str(e)}", exc_info=True)
        raise
