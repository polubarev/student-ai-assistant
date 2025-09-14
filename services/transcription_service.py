from typing import Optional, Dict, Any
from abc import ABC, abstractmethod
import assemblyai as aai


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
    
    def transcribe(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None) -> str:
        """
        Transcribe audio file using AssemblyAI.
        
        Args:
            audio_file_path: Path to the audio file
            config: Optional configuration parameters
            
        Returns:
            str: Transcribed text
        """
        try:
            # Default configuration
            default_config = {
                "speech_model": aai.SpeechModel.universal,
                "language_code": "ru"
            }
            
            # Merge with provided config
            if config:
                default_config.update(config)
            
            # Create transcription config
            transcription_config = aai.TranscriptionConfig(
                speech_model=default_config.get("speech_model", aai.SpeechModel.universal),
                language_code=default_config.get("language_code", "ru")
            )
            
            # Transcribe the audio
            transcriber = aai.Transcriber(config=transcription_config)
            transcript = transcriber.transcribe(audio_file_path)
            
            if transcript.status == "error":
                raise RuntimeError(f"Transcription failed: {transcript.error}")
            
            return transcript.text
            
        except Exception as e:
            raise RuntimeError(f"Transcription error: {str(e)}")


class TranscriptionService:
    """Main transcription service that manages transcription providers."""
    
    def __init__(self, provider: Optional[TranscriptionProvider] = None):
        self.provider = provider
    
    def transcribe_audio(self, audio_file_path: str, config: Optional[Dict[str, Any]] = None) -> str:
        """
        Transcribe audio file to text.
        
        Args:
            audio_file_path: Path to the audio file
            config: Optional configuration parameters
            
        Returns:
            str: Transcribed text
        """
        if not self.provider:
            raise ValueError("No transcription provider configured")
        
        return self.provider.transcribe(audio_file_path, config)

