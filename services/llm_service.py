from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
import time
import sys
import random
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from openai import RateLimitError
from utils.logger import get_logger, Logger
from config import Config

logger = get_logger(__name__)


class LLMProvider(ABC):
    """Abstract base class for LLM services."""

    @abstractmethod
    def process_text(self, text: str, system_prompt: str, **kwargs) -> str:
        """
        Process text using the LLM.
        
        Args:
            text: Input text to process
            system_prompt: System prompt for the LLM
            **kwargs: Additional parameters
            
        Returns:
            str: Processed text response
        """
        pass


class OpenAIProvider(LLMProvider):
    """LLM service using OpenAI."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o", **kwargs):
        self.model = model
        logger.info(f"OpenAIProvider initializing with model: {model}")

        # Log configuration (excluding sensitive data)
        safe_kwargs = {k: v for k, v in kwargs.items()
                       if not any(sensitive in k.lower()
                                for sensitive in ['key', 'token', 'secret', 'password'])}
        logger.debug(f"OpenAI configuration: {safe_kwargs}")

        self.llm = ChatOpenAI(
            model=model,
            temperature=kwargs.get("temperature", 0),
            max_tokens=kwargs.get("max_tokens", None),
            timeout=kwargs.get("timeout", None),
            max_retries=kwargs.get("max_retries", 2),
            api_key=api_key,
            **{k: v for k, v in kwargs.items() if k not in ["temperature", "max_tokens", "timeout", "max_retries"]}
        )
        logger.info("OpenAIProvider initialized successfully")

    def process_text(self, text: str, system_prompt: str, **kwargs) -> str:
        """
        Process text using OpenAI with exponential backoff for rate limiting.
        
        Args:
            text: Input text to process
            system_prompt: System prompt for the LLM
            **kwargs: Additional parameters
            
        Returns:
            str: Processed text response
        """
        start_time = time.time()
        logger.info(f"Starting LLM processing with model: {self.model}")
        logger.debug(f"Input text length: {len(text)} characters")
        logger.debug(f"System prompt length: {len(system_prompt)} characters")

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=text)
        ]

        max_retries = self.llm.max_retries
        base_wait_time = 1  # seconds

        for attempt in range(max_retries + 1):
            try:
                logger.info(f"Sending request to OpenAI (Attempt {attempt + 1}/{max_retries + 1})")
                response = self.llm.invoke(messages)

                duration = time.time() - start_time
                logger.info(f"LLM processing completed in {duration:.2f}s")
                logger.info(f"Response length: {len(response.content)} characters")

                return response.content

            except RateLimitError as e:
                duration = time.time() - start_time
                logger.warning(f"OpenAI rate limit error on attempt {attempt + 1}: {e}")

                if attempt < max_retries:
                    wait_time = (base_wait_time * (2 ** attempt)) + random.uniform(0, 1)
                    logger.info(f"Rate limit hit. Retrying in {wait_time:.2f} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"OpenAI rate limit or quota error after {duration:.2f}s and {max_retries} retries: {e}", exc_info=True)
                    raise RuntimeError(f"OpenAI API request failed after multiple retries. This may be due to rate limiting or insufficient quota. Please check your OpenAI plan and billing details. Original error: {e}") from e

            except Exception as e:
                duration = time.time() - start_time
                logger.error(f"LLM processing error after {duration:.2f}s: {str(e)}", exc_info=True)
                raise RuntimeError(f"LLM processing error: {str(e)}")


class LLMService:
    """Main LLM service that manages LLM providers."""
    
    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider
        logger.info(f"LLMService initialized with provider: {type(self.provider).__name__ if self.provider else 'None'}")
    
    def process_text(self, text: str, system_prompt: str, **kwargs) -> str:
        """
        Process text using the configured LLM provider.
        
        Args:
            text: Input text to process
            system_prompt: System prompt for the LLM
            **kwargs: Additional parameters
            
        Returns:
            str: Processed text response
        """
        logger.info(f"LLMService: Starting text processing, input length: {len(text)} characters")
        
        if not self.provider:
            logger.error("No LLM provider configured")
            raise ValueError("No LLM provider configured")
        
        result = self.provider.process_text(text, system_prompt, **kwargs)
        logger.info(f"LLMService: Text processing completed, result length: {len(result)} characters")
        return result
    
    def summarize_text(self, text: str, **kwargs) -> str:
        """
        Summarize text using the configured LLM provider.
        
        Args:
            text: Input text to summarize
            **kwargs: Additional parameters
            
        Returns:
            str: Summary of the text
        """
        logger.info(f"LLMService: Starting text summarization, input length: {len(text)} characters")
        
        system_prompt = '''You are a helpful assistant that creates concise and informative summaries. 
        Please provide a clear, well-structured summary of the given text, highlighting the main points and key information.'''
        
        result = self.process_text(text, system_prompt, **kwargs)
        logger.info(f"LLMService: Text summarization completed, summary length: {len(result)} characters")
        return result

if __name__ == '__main__':
    # Add project root to Python path
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Setup logging for debugging
    from utils.logger import Logger
    Logger.setup_logging(log_level="DEBUG")

    logger.info("Running llm_service.py in debug mode.")

    # Load configuration to get API key
    try:
        api_key = Config.OPENAI_API_KEY
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables. Please set it in your .env file.")
        
        openai_config = Config.get_openai_config()

    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    # Create a sample text
    sample_text = (
        "Artificial intelligence (AI) is intelligence demonstrated by machines, "
        "in contrast to the natural intelligence displayed by humans and other animals. "
        "Leading AI textbooks define the field as the study of 'intelligent agents': "
        "any device that perceives its environment and takes actions that maximize its "
        "chance of successfully achieving its goals."
    )

    # Initialize the provider and service
    try:
        logger.info("Initializing OpenAIProvider...")
        # Set a low timeout to test retry logic
        openai_config['timeout'] = 5
        openai_provider = OpenAIProvider(api_key=api_key, model="gpt-5-mini", **openai_config)
        
        logger.info("Initializing LLMService...")
        llm_service = LLMService(provider=openai_provider)

        # Process the text multiple times to test rate limiting
        num_requests = 5
        logger.info(f"Simulating {num_requests} requests to test rate limiting and retry logic...")

        for i in range(num_requests):
            try:
                logger.info(f"--- Request {i + 1}/{num_requests} ---")
                summary = llm_service.summarize_text(f"{sample_text} (Request {i+1})")
                print(f"\n--- Summary {i + 1} ---")
                print(summary)
                print("-----------------")
            except RuntimeError as e:
                print(f"\n--- Error on Request {i + 1} ---")
                print(f"Caught a runtime error: {e}")
                print("-----------------")

    except RuntimeError as e:
        print(f"\n--- General Error ---")
        print(f"Caught a runtime error during initialization: {e}")
        print("---------------------")
    except Exception as e:
        print(f"\n--- Unexpected Error ---")
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        print(f"An unexpected error occurred: {e}")
        print("------------------------")
