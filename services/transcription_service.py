from typing import Optional, Dict, Any
from abc import ABC, abstractmethod
import time
import assemblyai as aai
from utils.logger import get_logger

logger = get_logger(__name__)


class TranscriptionProvider(ABC):
    """Abstract base class for transcription services."""
    
    @abstractmethod
    def transcribe(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None) -> str:
        """
        Transcribe audio file to text.
        
        Args:
            audio_file_path: Path to the audio file
            config: Optional configuration parameters
            
        Returns:
            str: Transcribed text
        """
        pass


class AssemblyAIProvider(TranscriptionProvider):
    """Transcription service using AssemblyAI."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        aai.settings.api_key = api_key
        logger.info("AssemblyAIProvider initialized with API key")
    
    def transcribe(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None) -> str:
        """
        Transcribe audio file using AssemblyAI.
        
        Args:
            audio_file_path: Path to the audio file
            config: Optional configuration parameters
            
        Returns:
            str: Transcribed text
        """
        start_time = time.time()
        logger.info(f"Starting transcription of {audio_file_path}")
        
        try:
            # Default configuration
            default_config = {
                "speech_model": aai.SpeechModel.universal,
                "language_code": "ru"
            }
            
            # Merge with provided config
            if config:
                default_config.update(config)
                logger.debug(f"Using custom config: {config}")
            
            logger.debug(f"Final transcription config: {default_config}")
            
            # Create transcription config
            transcription_config = aai.TranscriptionConfig(
                speech_model=default_config.get("speech_model", aai.SpeechModel.universal),
                language_code=default_config.get("language_code", "ru")
            )
            
            # Transcribe the audio
            logger.info("Sending audio to AssemblyAI for transcription")
            transcriber = aai.Transcriber(config=transcription_config)
            transcript = transcriber.transcribe(audio_file_path)
            
            duration = time.time() - start_time
            
            if transcript.status == "error":
                logger.error(f"AssemblyAI transcription failed after {duration:.2f}s: {transcript.error}")
                raise RuntimeError(f"Transcription failed: {transcript.error}")
            
            logger.info(f"Transcription completed in {duration:.2f}s, status: {transcript.status}")
            logger.info(f"Transcribed text length: {len(transcript.text)} characters")
            
            return transcript.text
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Transcription error after {duration:.2f}s: {str(e)}", exc_info=True)
            raise RuntimeError(f"Transcription error: {str(e)}")


class TranscriptionService:
    """Main transcription service that manages transcription providers."""
    
    def __init__(self, provider: Optional[TranscriptionProvider] = None):
        self.provider = provider
        logger.info(f"TranscriptionService initialized with provider: {type(self.provider).__name__ if self.provider else 'None'}")
    
    def transcribe_audio(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None) -> str:
        """
        Transcribe audio file to text.
        
        Args:
            audio_file_path: Path to the audio file
            config: Optional configuration parameters
            
        Returns:
            str: Transcribed text
        """
        logger.info(f"TranscriptionService: Starting transcription of {audio_file_path}")
        
        if not self.provider:
            logger.error("No transcription provider configured")
            raise ValueError("No transcription provider configured")
        
        result = self.provider.transcribe(audio_file_path, config)
        logger.info(f"TranscriptionService: Transcription completed, result length: {len(result)} characters")
        return result

