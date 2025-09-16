from typing import Optional
import time
import random
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from openai import RateLimitError
from utils.logger import get_logger

logger = get_logger(__name__)


class LLMService:
    """LLM service using OpenAI."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o", **kwargs):
        self.model = model
        self.llm = ChatOpenAI(
            model=model,
            temperature=kwargs.get("temperature", 0),
            max_tokens=kwargs.get("max_tokens", None),
            timeout=kwargs.get("timeout", None),
            max_retries=kwargs.get("max_retries", 2),
            api_key=api_key,
            **{k: v for k, v in kwargs.items() if k not in ["temperature", "max_tokens", "timeout", "max_retries"]}
        )
        logger.info(f"LLMService initialized with model: {model}")

    def summarize_text(self, text: str, system_prompt: Optional[str] = None) -> str:
        """
        Summarize text using the configured LLM provider.
        
        Args:
            text: Input text to summarize
            system_prompt: Optional system prompt to guide the summarization.
            
        Returns:
            str: Summary of the text
        """
        if system_prompt is None:
            system_prompt = '''You are a helpful assistant that creates concise and informative summaries. 
            Please provide a clear, well-structured summary of the given text, highlighting the main points and key information.'''
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=text)
        ]

        max_retries = self.llm.max_retries
        base_wait_time = 1  # seconds
        start_time = time.time()

        for attempt in range(max_retries + 1):
            try:
                logger.info(f"Sending request to OpenAI (Attempt {attempt + 1}/{max_retries + 1})")
                response = self.llm.invoke(messages)
                duration = time.time() - start_time
                logger.info(f"LLM processing completed in {duration:.2f}s")
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