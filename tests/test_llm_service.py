import pytest
from unittest.mock import MagicMock
from services.llm_service import LLMService


@pytest.fixture
def mock_openrouter_client(mocker):
    """Fixture to mock the OpenRouter client class."""
    mock_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="This is a mock summary."))]
    mock_instance.chat.send.return_value = mock_response

    mock_class = mocker.patch("services.llm_service.OpenRouter", return_value=mock_instance)
    
    return mock_class, mock_instance


def test_summarize_text_success(mock_openrouter_client):
    """Test that summarize_text returns a summary on successful API call."""
    mock_class, mock_instance = mock_openrouter_client
    llm_service = LLMService(api_key="fake_api_key")

    summary = llm_service.summarize_text("Some long text to summarize.")

    mock_class.assert_called_once_with(api_key="fake_api_key")
    mock_instance.chat.send.assert_called_once()
    send_kwargs = mock_instance.chat.send.call_args.kwargs
    assert send_kwargs["model"] == "google/gemini-3-flash-preview"
    assert send_kwargs["temperature"] == 0
    assert send_kwargs["max_tokens"] is None
    assert send_kwargs["retries"] == 2
    assert send_kwargs["stream"] is False
    assert summary == "This is a mock summary."


def test_summarize_text_api_error(mock_openrouter_client):
    """Test that summarize_text raises an error if the API call fails."""
    _, mock_instance = mock_openrouter_client

    mock_instance.chat.send.side_effect = Exception("API Error")

    llm_service = LLMService(api_key="fake_api_key")

    with pytest.raises(RuntimeError, match="LLM processing error: API Error"):
        llm_service.summarize_text("Some text.")
