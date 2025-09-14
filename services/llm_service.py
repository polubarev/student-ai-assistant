from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage


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
        self.llm = ChatOpenAI(
            model=model,
            temperature=kwargs.get("temperature", 0),
            max_tokens=kwargs.get("max_tokens", None),
            timeout=kwargs.get("timeout", None),
            max_retries=kwargs.get("max_retries", 2),
            api_key=api_key,
            **{k: v for k, v in kwargs.items() if k not in ["temperature", "max_tokens", "timeout", "max_retries"]}
        )
    
    def process_text(self, text: str, system_prompt: str, **kwargs) -> str:
        """
        Process text using OpenAI.
        
        Args:
            text: Input text to process
            system_prompt: System prompt for the LLM
            **kwargs: Additional parameters
            
        Returns:
            str: Processed text response
        """
        try:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=text)
            ]
            
            response = self.llm.invoke(messages)
            return response.content
            
        except Exception as e:
            raise RuntimeError(f"LLM processing error: {str(e)}")


class LLMService:
    """Main LLM service that manages LLM providers."""
    
    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider
    
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
        if not self.provider:
            raise ValueError("No LLM provider configured")
        
        return self.provider.process_text(text, system_prompt, **kwargs)
    
    def summarize_text(self, text: str, **kwargs) -> str:
        """
        Summarize text using the configured LLM provider.
        
        Args:
            text: Input text to summarize
            **kwargs: Additional parameters
            
        Returns:
            str: Summary of the text
        """
        system_prompt = """You are a helpful assistant that creates concise and informative summaries. 
        Please provide a clear, well-structured summary of the given text, highlighting the main points and key information."""
        
        return self.process_text(text, system_prompt, **kwargs)

