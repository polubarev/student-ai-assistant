from typing import Optional
from openrouter import OpenRouter
from utils.logger import get_logger

logger = get_logger(__name__)


class LLMService:
    """LLM service using the OpenRouter official Python SDK."""

    def __init__(self, api_key: Optional[str] = None, model: str = "google/gemini-3-flash-preview", **kwargs):
        self.model = model
        self.temperature = kwargs.get("temperature", 0)
        self.max_tokens = kwargs.get("max_tokens", None)
        self.timeout = kwargs.get("timeout", None)
        self.max_retries = kwargs.get("max_retries", 2)
        self.http_referer = kwargs.get("http_referer")
        self.x_title = kwargs.get("x_title")

        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if self.http_referer:
            client_kwargs["http_referer"] = self.http_referer
        if self.x_title:
            client_kwargs["x_title"] = self.x_title

        self.client = OpenRouter(**client_kwargs)
        logger.info(f"LLMService initialized with OpenRouter model: {model}")

        if self.timeout is not None:
            logger.warning(
                "OPENROUTER_TIMEOUT is set, but explicit timeout forwarding is not configured in current SDK integration."
            )

    @staticmethod
    def _extract_content(response) -> str:
        """Extract text content from OpenRouter response object."""
        choices = getattr(response, "choices", None)
        if choices is None and isinstance(response, dict):
            choices = response.get("choices")

        if not choices:
            raise RuntimeError("OpenRouter response did not contain choices.")

        first_choice = choices[0]
        message = first_choice.get("message") if isinstance(first_choice, dict) else getattr(first_choice, "message", None)
        if message is None:
            raise RuntimeError("OpenRouter response choice did not contain a message.")

        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if content is None:
            raise RuntimeError("OpenRouter response message did not contain content.")

        if isinstance(content, str):
            return content

        # Content can be a list of parts in some providers.
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif isinstance(item.get("content"), str):
                        parts.append(item["content"])
                else:
                    part_text = getattr(item, "text", None) or getattr(item, "content", None)
                    if isinstance(part_text, str):
                        parts.append(part_text)
            if parts:
                return "\n".join(parts).strip()

        raise RuntimeError("OpenRouter response content format is unsupported.")

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
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        try:
            logger.info(f"Sending request to OpenRouter model {self.model}")
            response = self.client.chat.send(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                retries=self.max_retries,
                stream=False,
            )
            return self._extract_content(response)
        except Exception as e:
            logger.error(f"LLM processing error: {str(e)}", exc_info=True)
            raise RuntimeError(f"LLM processing error: {str(e)}") from e
