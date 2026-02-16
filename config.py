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
    OPENROUTER_API_KEY: Optional[str] = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    
    # Default Configuration
    DEFAULT_LANGUAGE: str = os.getenv("DEFAULT_LANGUAGE", "ru")
    DEFAULT_OPENROUTER_MODEL: str = (
        os.getenv("DEFAULT_OPENROUTER_MODEL")
        or os.getenv("DEFAULT_OPENAI_MODEL")
        or "google/gemini-3-flash-preview"
    )
    
    # OpenRouter configuration (with backward compatibility on OPENAI_* keys)
    OPENROUTER_TEMPERATURE: float = float(
        os.getenv("OPENROUTER_TEMPERATURE") or os.getenv("OPENAI_TEMPERATURE") or "0"
    )
    OPENROUTER_MAX_TOKENS: Optional[int] = (
        int(os.getenv("OPENROUTER_MAX_TOKENS") or os.getenv("OPENAI_MAX_TOKENS"))
        if (os.getenv("OPENROUTER_MAX_TOKENS") or os.getenv("OPENAI_MAX_TOKENS"))
        else None
    )
    OPENROUTER_TIMEOUT: Optional[int] = (
        int(os.getenv("OPENROUTER_TIMEOUT") or os.getenv("OPENAI_TIMEOUT"))
        if (os.getenv("OPENROUTER_TIMEOUT") or os.getenv("OPENAI_TIMEOUT"))
        else None
    )
    OPENROUTER_MAX_RETRIES: int = int(
        os.getenv("OPENROUTER_MAX_RETRIES") or os.getenv("OPENAI_MAX_RETRIES") or "2"
    )
    OPENROUTER_HTTP_REFERER: Optional[str] = os.getenv("OPENROUTER_HTTP_REFERER")
    OPENROUTER_X_TITLE: Optional[str] = os.getenv("OPENROUTER_X_TITLE") or "Student AI Assistant"
    
    # Transcription Configuration
    TRANSCRIPTION_SPEECH_MODEL: str = os.getenv("TRANSCRIPTION_SPEECH_MODEL", "universal")
    
    # FFmpeg Configuration
    FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")

    # GCS upload flow (for large files on Cloud Run)
    GCS_UPLOAD_BUCKET: Optional[str] = os.getenv("GCS_UPLOAD_BUCKET")
    APP_BASE_URL: Optional[str] = os.getenv("APP_BASE_URL")
    GCS_SIGNER_SERVICE_ACCOUNT_EMAIL: Optional[str] = os.getenv("GCS_SIGNER_SERVICE_ACCOUNT_EMAIL")
    
    @classmethod
    def log_configuration(cls):
        """Log the current configuration (excluding sensitive data)."""
        logger.info("Configuration loaded:")
        logger.info(f"  Default Language: {cls.DEFAULT_LANGUAGE}")
        logger.info(f"  Default OpenRouter Model: {cls.DEFAULT_OPENROUTER_MODEL}")
        logger.info(f"  OpenRouter Temperature: {cls.OPENROUTER_TEMPERATURE}")
        logger.info(f"  OpenRouter Max Retries: {cls.OPENROUTER_MAX_RETRIES}")
        logger.info(f"  Transcription Speech Model: {cls.TRANSCRIPTION_SPEECH_MODEL}")
        logger.info(f"  FFmpeg Path: {cls.FFMPEG_PATH}")
        logger.info(f"  GCS Upload Bucket: {cls.GCS_UPLOAD_BUCKET or '<not set>'}")
        logger.info(f"  App Base URL: {cls.APP_BASE_URL or '<not set>'}")
        logger.info(
            f"  GCS Signer Service Account: {cls.GCS_SIGNER_SERVICE_ACCOUNT_EMAIL or '<auto>'}"
        )
        
        # Log API key status (without values)
        has_assemblyai = bool(cls.ASSEMBLYAI_API_KEY)
        has_openrouter = bool(cls.OPENROUTER_API_KEY)
        logger.info(f"  AssemblyAI API Key: {'✓' if has_assemblyai else '✗'}")
        logger.info(f"  OpenRouter API Key: {'✓' if has_openrouter else '✗'}")
        
        # Log optional configurations
        if cls.OPENROUTER_MAX_TOKENS:
            logger.info(f"  OpenRouter Max Tokens: {cls.OPENROUTER_MAX_TOKENS}")
        if cls.OPENROUTER_TIMEOUT:
            logger.info(f"  OpenRouter Timeout: {cls.OPENROUTER_TIMEOUT}")

    @classmethod
    def get_llm_config(cls) -> dict:
        """Get LLM (OpenRouter) configuration as a dictionary."""
        logger.debug("Getting OpenRouter configuration")
        config = {
            "temperature": cls.OPENROUTER_TEMPERATURE,
            "max_retries": cls.OPENROUTER_MAX_RETRIES,
            "http_referer": cls.OPENROUTER_HTTP_REFERER,
            "x_title": cls.OPENROUTER_X_TITLE,
        }
        
        if cls.OPENROUTER_MAX_TOKENS:
            config["max_tokens"] = cls.OPENROUTER_MAX_TOKENS
        
        if cls.OPENROUTER_TIMEOUT:
            config["timeout"] = cls.OPENROUTER_TIMEOUT
        
        logger.debug(f"OpenRouter config: {config}")
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
        
        if not cls.OPENROUTER_API_KEY:
            missing_keys.append("OPENROUTER_API_KEY")
        
        is_valid = len(missing_keys) == 0
        logger.debug(f"API key validation result: valid={is_valid}, missing={missing_keys}")
        return is_valid, missing_keys

# Log configuration on module load
Config.log_configuration()
