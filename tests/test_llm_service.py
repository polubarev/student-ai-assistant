import pytest
from unittest.mock import MagicMock
from services.llm_service import LLMService


@pytest.fixture
def mock_chat_openai(mocker):  # mocker is a fixture provided by pytest-mock
    """Fixture to mock the ChatOpenAI class."""
    mock_instance = MagicMock()
    mock_instance.invoke.return_value.content = "This is a mock summary."

    # Patch the ChatOpenAI class in the llm_service module
    mock_class = mocker.patch("services.llm_service.ChatOpenAI", return_value=mock_instance)
    
    return mock_class, mock_instance


def test_summarize_text_success(mock_chat_openai):
    """Test that summarize_text returns a summary on successful API call."""
    mock_class, mock_instance = mock_chat_openai

    # Initialize the service. This will use the mocked ChatOpenAI.
    llm_service = LLMService(api_key="fake_api_key")

    # Call the method to be tested
    summary = llm_service.summarize_text("Some long text to summarize.")

    # Assertions
    # Check if ChatOpenAI was initialized correctly
    mock_class.assert_called_once_with(
        model="gpt-4o",
        temperature=0,
        max_tokens=None,
        timeout=None,
        max_retries=2,
        api_key="fake_api_key"
    )

    # Check if the invoke method was called on the instance
    mock_instance.invoke.assert_called_once()
    
    # Check that the summary is the one we faked
    assert summary == "This is a mock summary."


def test_summarize_text_api_error(mock_chat_openai):
    """Test that summarize_text raises an error if the API call fails."""
    mock_class, mock_instance = mock_chat_openai

    # Configure the mock to raise an exception
    mock_instance.invoke.side_effect = Exception("API Error")

    llm_service = LLMService(api_key="fake_api_key")

    # Assert that a RuntimeError is raised when the API call fails
    with pytest.raises(RuntimeError, match="LLM processing error: API Error"):
        llm_service.summarize_text("Some text.")
