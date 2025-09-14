import os
from dotenv import load_dotenv
from typing import Optional
from utils.logger import get_logger

# Load environment variables from .env file
load_dotenv()

logger = get_logger(__name__)


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
    def log_configuration(cls):
        """Log the current configuration (excluding sensitive data)."""
        logger.info("Configuration loaded:")
        logger.info(f"  Default Language: {cls.DEFAULT_LANGUAGE}")
        logger.info(f"  Default OpenAI Model: {cls.DEFAULT_OPENAI_MODEL}")
        logger.info(f"  OpenAI Temperature: {cls.OPENAI_TEMPERATURE}")
        logger.info(f"  OpenAI Max Retries: {cls.OPENAI_MAX_RETRIES}")
        logger.info(f"  Transcription Speech Model: {cls.TRANSCRIPTION_SPEECH_MODEL}")
        logger.info(f"  FFmpeg Path: {cls.FFMPEG_PATH}")
        
        # Log API key status (without values)
        has_assemblyai = bool(cls.ASSEMBLYAI_API_KEY)
        has_openai = bool(cls.OPENAI_API_KEY)
        logger.info(f"  AssemblyAI API Key: {'✓' if has_assemblyai else '✗'}")
        logger.info(f"  OpenAI API Key: {'✓' if has_openai else '✗'}")
        
        # Log optional configurations
        if cls.OPENAI_MAX_TOKENS:
            logger.info(f"  OpenAI Max Tokens: {cls.OPENAI_MAX_TOKENS}")
        if cls.OPENAI_TIMEOUT:
            logger.info(f"  OpenAI Timeout: {cls.OPENAI_TIMEOUT}")

    @classmethod
    def get_openai_config(cls) -> dict:
        """Get OpenAI configuration as a dictionary."""
        logger.debug("Getting OpenAI configuration")
        config = {
            "temperature": cls.OPENAI_TEMPERATURE,
            "max_retries": cls.OPENAI_MAX_RETRIES,
        }
        
        if cls.OPENAI_MAX_TOKENS:
            config["max_tokens"] = cls.OPENAI_MAX_TOKENS
        
        if cls.OPENAI_TIMEOUT:
            config["timeout"] = cls.OPENAI_TIMEOUT
        
        logger.debug(f"OpenAI config: {config}")
        return config
    
    @classmethod
    def get_transcription_config(cls, language: Optional[str] = None) -> dict:
        """Get transcription configuration as a dictionary."""
        logger.debug(f"Getting transcription configuration for language: {language or cls.DEFAULT_LANGUAGE}")
        config = {
            "speech_model": cls.TRANSCRIPTION_SPEECH_MODEL,
            "language_code": language or cls.DEFAULT_LANGUAGE
        }
        logger.debug(f"Transcription config: {config}")
        return config
    
    @classmethod
    def validate_api_keys(cls) -> tuple[bool, list[str]]:
        """
        Validate that required API keys are present.
        
        Returns:
            tuple: (is_valid, missing_keys)
        """
        logger.debug("Validating API keys")
        missing_keys = []
        
        if not cls.ASSEMBLYAI_API_KEY:
            missing_keys.append("ASSEMBLYAI_API_KEY")
        
        if not cls.OPENAI_API_KEY:
            missing_keys.append("OPENAI_API_KEY")
        
        is_valid = len(missing_keys) == 0
        logger.debug(f"API key validation result: valid={is_valid}, missing={missing_keys}")
        return is_valid, missing_keys

# Log configuration on module load
Config.log_configuration()