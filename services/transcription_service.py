from typing import Optional, Dict, Any
from abc import ABC, abstractmethod
import time
import assemblyai as aai
from utils.logger import get_logger
from config import Config
from services.audio_service import FFmpegAudioExtractor
from services.audio_service import MediaValidationError

logger = get_logger(__name__)


class TranscriptionPending(RuntimeError):
    """The submitted job is still running and can be resumed without a new upload."""


class TranscriptionFailed(RuntimeError):
    """A terminal provider failure; a new job may be submitted explicitly."""


class TranscriptionProvider(ABC):
    """Abstract base class for transcription services."""
    
    @abstractmethod
    def transcribe(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None, *,
                   transcript_id=None, on_submitted=None) -> str:
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
        logger.info("AssemblyAIProvider initialized with API key")
    
    def transcribe(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None, *,
                   transcript_id=None, on_submitted=None) -> str:
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
        
        client = None
        try:
            if not transcript_id:
                FFmpegAudioExtractor().require_valid_audio(audio_file_path)
            client = aai.Client(settings=aai.Settings(api_key=self.api_key, http_timeout=30))
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
            transcriber = aai.Transcriber(client=client, config=transcription_config)
            if transcript_id:
                transcript = aai.Transcript(transcript_id=transcript_id, client=client)
            else:
                transcript = transcriber.submit(audio_file_path)
                if on_submitted:
                    on_submitted(transcript.id)
            try:
                transcript.wait_for_completion(poll_timeout=Config.TRANSCRIPTION_POLL_SECONDS)
            except aai.TranscriptError as exc:
                if exc.status_code is None and transcript.status in ("queued", "processing"):
                    raise TranscriptionPending("Аудио обрабатывается. Нажмите «Проверить транскрипцию» через минуту.") from None
                raise
            
            duration = time.time() - start_time
            
            if transcript.status == "error":
                logger.error("AssemblyAI transcription failed")
                raise TranscriptionFailed("Не удалось распознать аудио. Файл сохранён; можно попробовать снова.")
            
            logger.info(f"Transcription completed in {duration:.2f}s, status: {transcript.status}")
            if not transcript.text or len(transcript.text) > Config.MAX_TRANSCRIPT_CHARS:
                raise TranscriptionFailed("Результат пустой или слишком длинный. Разделите лекцию на части.")
            logger.info("Transcribed text length: %s characters", len(transcript.text))
            return transcript.text
        except (MediaValidationError, TranscriptionPending, TranscriptionFailed):
            raise
        except Exception as e:
            duration = time.time() - start_time
            logger.error("Transcription failed: %s", type(e).__name__)
            raise RuntimeError("Transcription failed") from None
        finally:
            if client is not None:
                client.http_client.close()


class TranscriptionService:
    """Main transcription service that manages transcription providers."""
    
    def __init__(self, provider: Optional[TranscriptionProvider] = None):
        self.provider = provider
        logger.info(f"TranscriptionService initialized with provider: {type(self.provider).__name__ if self.provider else 'None'}")
    
    def transcribe_audio(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None, **job_options) -> str:
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
        
        result = self.provider.transcribe(audio_file_path, config, **job_options)
        logger.info(f"TranscriptionService: Transcription completed, result length: {len(result)} characters")
        return result

