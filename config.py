import os
from dotenv import load_dotenv
from typing import Optional

# Load environment variables from .env file
load_dotenv()


class Config:
    """Configuration class for managing environment variables and defaults."""
    
    # API Keys
    ASSEMBLYAI_API_KEY: Optional[str] = os.getenv("ASSEMBLYAI_API_KEY")
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    
    # Default Configuration
    DEFAULT_LANGUAGE: str = os.getenv("DEFAULT_LANGUAGE", "ru")
    DEFAULT_OPENAI_MODEL: str = os.getenv("DEFAULT_OPENAI_MODEL", "gpt-4o")
    
    # OpenAI Configuration
    OPENAI_TEMPERATURE: float = float(os.getenv("OPENAI_TEMPERATURE", "0"))
    OPENAI_MAX_TOKENS: Optional[int] = int(os.getenv("OPENAI_MAX_TOKENS")) if os.getenv("OPENAI_MAX_TOKENS") else None
    OPENAI_TIMEOUT: Optional[int] = int(os.getenv("OPENAI_TIMEOUT")) if os.getenv("OPENAI_TIMEOUT") else None
    OPENAI_MAX_RETRIES: int = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
    
    # Transcription Configuration
    TRANSCRIPTION_SPEECH_MODEL: str = os.getenv("TRANSCRIPTION_SPEECH_MODEL", "universal")
    
    # FFmpeg Configuration
    FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")
    
    @classmethod
    def get_openai_config(cls) -> dict:
        """Get OpenAI configuration as a dictionary."""
        config = {
            "temperature": cls.OPENAI_TEMPERATURE,
            "max_retries": cls.OPENAI_MAX_RETRIES,
        }
        
        if cls.OPENAI_MAX_TOKENS:
            config["max_tokens"] = cls.OPENAI_MAX_TOKENS
        
        if cls.OPENAI_TIMEOUT:
            config["timeout"] = cls.OPENAI_TIMEOUT
            
        return config
    
    @classmethod
    def get_transcription_config(cls, language: Optional[str] = None) -> dict:
        """Get transcription configuration as a dictionary."""
        return {
            "speech_model": cls.TRANSCRIPTION_SPEECH_MODEL,
            "language_code": language or cls.DEFAULT_LANGUAGE
        }
    
    @classmethod
    def validate_api_keys(cls) -> tuple[bool, list[str]]:
        """
        Validate that required API keys are present.
        
        Returns:
            tuple: (is_valid, missing_keys)
        """
        missing_keys = []
        
        if not cls.ASSEMBLYAI_API_KEY:
            missing_keys.append("ASSEMBLYAI_API_KEY")
        
        if not cls.OPENAI_API_KEY:
            missing_keys.append("OPENAI_API_KEY")
        
        return len(missing_keys) == 0, missing_keys

