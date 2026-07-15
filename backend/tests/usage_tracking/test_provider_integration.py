"""Tests for LLM provider integration with usage tracking.

These tests verify that LLM providers correctly capture and return
token usage data from API responses.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.providers.llm.core.base import LLMResponse, LLMStreamingResponse
from agent.providers.llm.contracts.compat import extract_content, extract_usage, has_usage
from backend.services.usage_tracking.models import UsageData


class TestLLMResponse:
    """Tests for LLMResponse dataclass."""
    
    def test_llm_response_with_usage(self):
        """LLMResponse should store content and usage."""
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        
        response = LLMResponse(
            content="Hello, world!",
            usage=usage,
            raw=None,
        )
        
        assert response.content == "Hello, world!"
        assert response.usage is not None
        assert response.usage.prompt_tokens == 100
        assert response.usage.completion_tokens == 50
    
    def test_llm_response_without_usage(self):
        """LLMResponse should work without usage data."""
        response = LLMResponse(
            content="Hello, world!",
            usage=None,
            raw=None,
        )
        
        assert response.content == "Hello, world!"
        assert response.usage is None


class TestBackwardCompatibility:
    """Tests for backward compatibility helpers."""
    
    def test_extract_content_from_string(self):
        """extract_content should return string as-is."""
        result = extract_content("Hello, world!")
        
        assert result == "Hello, world!"
    
    def test_extract_content_from_llm_response(self):
        """extract_content should extract content from LLMResponse."""
        response = LLMResponse(
            content="Hello from response!",
            usage=None,
            raw=None,
        )
        
        result = extract_content(response)
        
        assert result == "Hello from response!"
    
    def test_extract_usage_from_string(self):
        """extract_usage should return None for string."""
        result = extract_usage("Hello, world!")
        
        assert result is None
    
    def test_extract_usage_from_llm_response(self):
        """extract_usage should extract usage from LLMResponse."""
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        response = LLMResponse(
            content="Hello!",
            usage=usage,
            raw=None,
        )
        
        result = extract_usage(response)
        
        assert result is not None
        assert result.total_tokens == 150
    
    def test_extract_usage_from_llm_response_without_usage(self):
        """extract_usage should return None if LLMResponse has no usage."""
        response = LLMResponse(
            content="Hello!",
            usage=None,
            raw=None,
        )
        
        result = extract_usage(response)
        
        assert result is None
    
    def test_has_usage_string(self):
        """has_usage should return False for string."""
        assert has_usage("Hello") is False
    
    def test_has_usage_with_usage(self):
        """has_usage should return True when usage present."""
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        response = LLMResponse(content="Hello", usage=usage, raw=None)
        
        assert has_usage(response) is True
    
    def test_has_usage_without_usage(self):
        """has_usage should return False when usage not present."""
        response = LLMResponse(content="Hello", usage=None, raw=None)
        
        assert has_usage(response) is False


class TestOpenAIChatClientUsageExtraction:
    """Tests for OpenAIChatClient usage extraction."""
    
    def test_extract_usage_from_chat_response(self):
        """Should extract usage from Chat Completions API response."""
        # Mock response with usage
        mock_response = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_response.usage.prompt_tokens_details = None
        
        usage = UsageData.from_openai_chat_response(mock_response, "gpt-4o-mini")
        
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150
        assert usage.model == "gpt-4o-mini"
    
    def test_extract_usage_with_cached_tokens(self):
        """Should extract cached tokens from prompt_tokens_details."""
        mock_response = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_response.usage.prompt_tokens_details = MagicMock()
        mock_response.usage.prompt_tokens_details.cached_tokens = 30
        
        usage = UsageData.from_openai_chat_response(mock_response, "gpt-4o")
        
        assert usage.cached_tokens == 30
    
    def test_extract_usage_missing_usage(self):
        """Should return empty usage when response has no usage."""
        mock_response = MagicMock()
        mock_response.usage = None
        
        usage = UsageData.from_openai_chat_response(mock_response, "gpt-4o-mini")
        
        assert usage.is_empty()


class TestOpenAIResponsesClientUsageExtraction:
    """Tests for OpenAIResponsesClient usage extraction."""
    
    def test_extract_usage_from_responses_api(self):
        """Should extract usage from Responses API format."""
        mock_response = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 200
        mock_response.usage.output_tokens = 100
        mock_response.usage.total_tokens = 300
        mock_response.usage.input_tokens_details = MagicMock()
        mock_response.usage.input_tokens_details.cached_tokens = 20
        mock_response.usage.output_tokens_details = MagicMock()
        mock_response.usage.output_tokens_details.reasoning_tokens = 0
        
        usage = UsageData.from_openai_responses_api(mock_response, "gpt-5")
        
        # Responses API uses input_tokens → prompt_tokens
        assert usage.prompt_tokens == 200
        # Responses API uses output_tokens → completion_tokens
        assert usage.completion_tokens == 100
        assert usage.total_tokens == 300
        assert usage.cached_tokens == 20
        assert usage.model == "gpt-5"
    
    def test_extract_reasoning_tokens(self):
        """Should extract reasoning_tokens for GPT-5 extended thinking."""
        mock_response = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 200
        mock_response.usage.output_tokens = 100
        mock_response.usage.total_tokens = 800
        mock_response.usage.input_tokens_details = MagicMock()
        mock_response.usage.input_tokens_details.cached_tokens = 0
        mock_response.usage.output_tokens_details = MagicMock()
        mock_response.usage.output_tokens_details.reasoning_tokens = 500
        
        usage = UsageData.from_openai_responses_api(mock_response, "gpt-5-pro")
        
        assert usage.reasoning_tokens == 500
    
    def test_extract_usage_missing_usage(self):
        """Should return empty usage when response has no usage."""
        mock_response = MagicMock()
        mock_response.usage = None
        
        usage = UsageData.from_openai_responses_api(mock_response, "gpt-5")
        
        assert usage.is_empty()


class TestLLMStreamingResponse:
    """Tests for LLMStreamingResponse."""
    
    @pytest.mark.asyncio
    async def test_streaming_response_structure(self):
        """LLMStreamingResponse should have iterator and usage accessor."""
        captured_usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        
        async def content_gen():
            yield "Hello"
            yield " "
            yield "World"
        
        def get_usage():
            return captured_usage
        
        response = LLMStreamingResponse(
            content_iterator=content_gen(),
            get_final_usage=get_usage,
        )
        
        # Consume iterator
        chunks = []
        async for chunk in response.content_iterator:
            chunks.append(chunk)
        
        assert chunks == ["Hello", " ", "World"]
        
        # Get usage after iteration
        usage = response.get_final_usage()
        assert usage is not None
        assert usage.total_tokens == 150
    
    @pytest.mark.asyncio
    async def test_streaming_response_without_usage(self):
        """Streaming response should handle missing usage gracefully."""
        async def content_gen():
            yield "Hello"
        
        def get_usage():
            return None
        
        response = LLMStreamingResponse(
            content_iterator=content_gen(),
            get_final_usage=get_usage,
        )
        
        # Consume iterator
        async for _ in response.content_iterator:
            pass
        
        # Should return None
        assert response.get_final_usage() is None


class TestOpenAIChatClientIntegration:
    """Integration tests for OpenAIChatClient with mocked API."""
    
    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_returns_llm_response(self):
        """chat_messages_with_usage should return LLMResponse with usage."""
        from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
        
        # Mock the client
        with patch('agent.providers.llm.adapters.openai.chat.openai') as mock_openai:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "Hello from GPT!"
            mock_response.usage = MagicMock()
            mock_response.usage.prompt_tokens = 10
            mock_response.usage.completion_tokens = 5
            mock_response.usage.total_tokens = 15
            mock_response.usage.prompt_tokens_details = None
            
            # Setup async client mock
            mock_async_client = AsyncMock()
            mock_async_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai.AsyncOpenAI.return_value = mock_async_client
            
            # Create client and call
            client = OpenAIChatClient(api_key="test-key", model="gpt-4o-mini")
            result = await client.chat_messages_with_usage([
                {"role": "user", "content": "Hello"}
            ])
            
            # Verify result type and content
            assert isinstance(result, LLMResponse)
            assert result.content == "Hello from GPT!"
            assert result.usage is not None
            assert result.usage.prompt_tokens == 10
            assert result.usage.completion_tokens == 5
            assert result.usage.total_tokens == 15


class TestOpenAIResponsesClientIntegration:
    """Integration tests for OpenAIResponsesClient with mocked API."""
    
    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_returns_llm_response(self):
        """chat_messages_with_usage should return LLMResponse with usage."""
        from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
        
        # Mock the client
        with patch('agent.providers.llm.adapters.openai.responses.client.openai') as mock_openai:
            # Setup mock response (Responses API format)
            mock_response = MagicMock()
            mock_response.output_text = "Hello from GPT-5!"
            mock_response.output = []
            mock_response.usage = MagicMock()
            mock_response.usage.input_tokens = 20
            mock_response.usage.output_tokens = 10
            mock_response.usage.total_tokens = 30
            mock_response.usage.input_tokens_details = MagicMock()
            mock_response.usage.input_tokens_details.cached_tokens = 4
            mock_response.usage.output_tokens_details = MagicMock()
            mock_response.usage.output_tokens_details.reasoning_tokens = 0
            
            # Setup async client mock
            mock_async_client = AsyncMock()
            mock_async_client.responses.create = AsyncMock(return_value=mock_response)
            mock_openai.AsyncOpenAI.return_value = mock_async_client
            
            # Create client and call
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5")
            result = await client.chat_messages_with_usage([
                {"role": "user", "content": "Hello"}
            ])
            
            # Verify result type and content
            assert isinstance(result, LLMResponse)
            assert result.content == "Hello from GPT-5!"
            assert result.usage is not None
            assert result.usage.prompt_tokens == 20  # input_tokens → prompt_tokens
            assert result.usage.completion_tokens == 10  # output_tokens → completion_tokens
            assert result.usage.total_tokens == 30
            assert result.usage.cached_tokens == 4
