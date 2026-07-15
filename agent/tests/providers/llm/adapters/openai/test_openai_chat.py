"""Tests for OpenAIChatClient provider.

Tests cover:
- Basic chat functionality
- Multi-turn conversations
- Streaming responses
- Tool/function calling
- Error handling and retries
- Empty response handling
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.core.base import StructuredOutputSpec, ToolCallResult
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec, ToolChoice
from agent.providers.llm.core.exceptions import (
    LLMAPIError,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
    LLMStructuredOutputParseError,
)


# ---------------------------------------------------------------------------
# Mock Fixtures
# ---------------------------------------------------------------------------


class MockChoice:
    """Mock OpenAI choice object."""
    
    def __init__(
        self, 
        content: str | None = "Test response",
        tool_calls: list | None = None,
        refusal: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.message = MagicMock()
        self.message.content = content
        self.message.tool_calls = tool_calls
        self.message.refusal = refusal
        self.finish_reason = finish_reason
        self.delta = MagicMock()
        self.delta.content = content


class MockResponse:
    """Mock OpenAI response object."""
    
    def __init__(
        self, 
        content: str | None = "Test response",
        tool_calls: list | None = None,
        refusal: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.id = "chatcmpl_test"
        self.choices = [MockChoice(content, tool_calls, refusal, finish_reason)]


class MockStreamEvent:
    """Mock streaming event."""
    
    def __init__(
        self,
        content: str | None = None,
        *,
        refusal: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        choice = MagicMock()
        choice.delta = MagicMock()
        choice.delta.content = content
        choice.delta.refusal = refusal
        choice.finish_reason = finish_reason
        self.choices = [choice]


class MockToolCall:
    """Mock tool call from OpenAI."""
    
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = arguments


@pytest.fixture
def mock_openai_client():
    """Create a mock AsyncOpenAI client."""
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=MockResponse("Test response")
    )
    return mock_client


@pytest.fixture
def client_with_mock(mock_openai_client):
    """Create OpenAIChatClient with mocked underlying client."""
    with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
        mock_openai.AsyncOpenAI.return_value = mock_openai_client
        client = OpenAIChatClient(api_key="test-key", model="gpt-4")
        client._client = mock_openai_client
        return client, mock_openai_client


@pytest.mark.asyncio
async def test_structured_chat_refusal_is_not_retried(client_with_mock) -> None:
    client, mock = client_with_mock
    mock.chat.completions.create.return_value = MockResponse(
        content=None,
        refusal="I cannot help with that request.",
        finish_reason="stop",
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await client.chat_messages(
            [{"role": "user", "content": "request"}],
            _retries=2,
        )

    assert exc_info.value.outcome.provider == "openai"
    assert exc_info.value.outcome.model == "gpt-4"
    assert exc_info.value.outcome.category == "content_filter"
    assert exc_info.value.outcome.explanation == "I cannot help with that request."
    assert exc_info.value.outcome.response_id == "chatcmpl_test"
    assert mock.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_content_filter_finish_reason_is_refusal(client_with_mock) -> None:
    client, mock = client_with_mock
    mock.chat.completions.create.return_value = MockResponse(
        content=None,
        finish_reason="content_filter",
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await client.chat_messages([{"role": "user", "content": "request"}])

    assert exc_info.value.outcome.explanation is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    (
        "chat_messages",
        "chat_messages_with_usage",
        "chat_with_tools",
        "chat_with_tools_with_usage",
    ),
)
async def test_non_stream_refusal_preserves_partial_content_across_call_paths(
    client_with_mock,
    method_name: str,
) -> None:
    """Every non-stream call surface retains coexisting assistant text."""
    client, mock = client_with_mock
    mock.chat.completions.create.return_value = MockResponse(
        content="Partial answer",
        finish_reason="content_filter",
    )

    method = getattr(client, method_name)
    with pytest.raises(LLMRefusalError) as exc_info:
        if "tools" in method_name:
            await method(
                "System",
                "User",
                tools=[{"type": "function", "function": {"name": "my_tool"}}],
            )
        else:
            await method([{"role": "user", "content": "request"}])

    assert exc_info.value.outcome.partial_content == "Partial answer"


@pytest.mark.asyncio
async def test_ordinary_refusal_like_text_remains_successful(client_with_mock) -> None:
    client, mock = client_with_mock
    mock.chat.completions.create.return_value = MockResponse(
        content="I can't help with that.",
        finish_reason="stop",
    )

    result = await client.chat_messages([{"role": "user", "content": "request"}])

    assert result == "I can't help with that."


@pytest.mark.asyncio
async def test_streamed_chat_refusal_preserves_partial_content(client_with_mock) -> None:
    client, mock = client_with_mock

    async def stream():
        yield MockStreamEvent("Partial answer")
        yield MockStreamEvent(None, refusal="Blocked by ")
        yield MockStreamEvent(
            None,
            refusal="policy.",
            finish_reason="stop",
        )

    mock.chat.completions.create.return_value = stream()
    chunks: list[str] = []

    with pytest.raises(LLMRefusalError) as exc_info:
        async for chunk in client.stream_chat_messages(
            [{"role": "user", "content": "request"}]
        ):
            chunks.append(chunk)

    assert chunks == ["Partial answer"]
    assert exc_info.value.outcome.partial_content == "Partial answer"
    assert exc_info.value.outcome.explanation == "Blocked by policy."


@pytest.mark.asyncio
async def test_streamed_chat_refusal_raises_at_clean_exhaustion(
    client_with_mock,
) -> None:
    """A refusal delta does not require a later finish-reason chunk."""
    client, mock = client_with_mock
    refusal_chunk = MockStreamEvent(None, refusal="Blocked by policy.")
    refusal_chunk.id = "chatcmpl_refusal_eof"

    async def stream():
        yield MockStreamEvent("Partial answer")
        yield refusal_chunk

    mock.chat.completions.create.return_value = stream()
    chunks: list[str] = []

    with pytest.raises(LLMRefusalError) as exc_info:
        async for chunk in client.stream_chat_messages(
            [{"role": "user", "content": "request"}]
        ):
            chunks.append(chunk)

    outcome = exc_info.value.outcome
    assert chunks == ["Partial answer"]
    assert outcome.explanation == "Blocked by policy."
    assert outcome.partial_content == "Partial answer"
    assert outcome.response_id == "chatcmpl_refusal_eof"


# ---------------------------------------------------------------------------
# Initialization Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientInit:
    """Tests for client initialization."""
    
    def test_init_with_defaults(self) -> None:
        """Test initialization with default model."""
        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIChatClient(api_key="test-key")
            
            assert client.model == "gpt-4"
    
    def test_init_with_custom_model(self) -> None:
        """Test initialization with custom model."""
        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIChatClient(api_key="test-key", model="gpt-4o-mini")
            
            assert client.model == "gpt-4o-mini"
    
    def test_model_property(self) -> None:
        """Test model property returns correct value."""
        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIChatClient(api_key="key", model="gpt-3.5-turbo")
            
            assert client.model == "gpt-3.5-turbo"


# ---------------------------------------------------------------------------
# Chat Method Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientChat:
    """Tests for the chat() method."""
    
    @pytest.mark.asyncio
    async def test_chat_returns_string(self, client_with_mock) -> None:
        """Test that chat returns a string response."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse("Hello, world!")
        
        result = await client.chat("You are helpful.", "Hello!")
        
        assert result == "Hello, world!"
        assert isinstance(result, str)
    
    @pytest.mark.asyncio
    async def test_chat_passes_parameters(self, client_with_mock) -> None:
        """Test that chat passes parameters to API."""
        client, mock = client_with_mock
        
        await client.chat(
            "System prompt",
            "User prompt",
            temperature=0.5,
            max_tokens=100,
        )
        
        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 100
    
    @pytest.mark.asyncio
    async def test_chat_uses_default_parameters(self, client_with_mock) -> None:
        """Test that chat uses default parameters."""
        client, mock = client_with_mock
        
        await client.chat("System", "User")
        
        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.1
        assert call_kwargs["max_tokens"] == 10000  # High default for testing

    @pytest.mark.asyncio
    async def test_chat_messages_plain_request_kwargs_parity(
        self, client_with_mock
    ) -> None:
        """Plain chat payload field names and defaults are compatibility contract."""
        client, mock = client_with_mock
        messages = [{"role": "user", "content": "Hello"}]

        await client.chat_messages(messages, temperature=0.7, max_tokens=123)

        assert mock.chat.completions.create.call_args.kwargs == {
            "model": "gpt-4",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 123,
        }


# ---------------------------------------------------------------------------
# Chat Messages Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientChatMessages:
    """Tests for the chat_messages() method."""
    
    @pytest.mark.asyncio
    async def test_chat_messages_with_history(self, client_with_mock) -> None:
        """Test multi-turn conversation."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse("Response to history")
        
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]
        
        result = await client.chat_messages(messages)
        
        assert result == "Response to history"
        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["messages"] == messages
    
    @pytest.mark.asyncio
    async def test_chat_messages_empty_response_raises(self, client_with_mock) -> None:
        """Test that empty response raises LLMResponseError."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(content=None)
        
        with pytest.raises(LLMResponseError, match="empty response"):
            await client.chat_messages([{"role": "user", "content": "test"}])
    
    @pytest.mark.asyncio
    async def test_chat_messages_whitespace_only_raises(self, client_with_mock) -> None:
        """Test that whitespace-only response raises LLMResponseError."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(content="   ")
        
        with pytest.raises(LLMResponseError, match="empty response"):
            await client.chat_messages([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_chat_messages_injects_response_format_for_structured_output(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content='{"host":"192.168.1.10","port":443}'
        )
        spec = StructuredOutputSpec(
            name="host_port",
            schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host", "port"],
                "additionalProperties": False,
            },
        )

        await client.chat_messages(
            [{"role": "user", "content": "parse this"}],
            structured_output=spec,
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        response_format = call_kwargs["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "host_port"
        assert response_format["json_schema"]["strict"] is True
        assert response_format["json_schema"]["schema"] == spec.schema

    @pytest.mark.asyncio
    async def test_chat_messages_tools_and_structured_request_kwargs_parity(
        self, client_with_mock
    ) -> None:
        """Tools, tool_choice, and response_format stay in the Chat Completions shape."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content='{"host":"192.168.1.10","port":443}'
        )
        messages = [{"role": "user", "content": "parse this"}]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "tool__nmap",
                    "description": "Run nmap",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        tool_choice = {"type": "function", "function": {"name": "tool__nmap"}}
        spec = StructuredOutputSpec(
            name="host_port",
            schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host", "port"],
                "additionalProperties": False,
            },
        )

        await client.chat_messages(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_output=spec,
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert set(call_kwargs) == {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "tools",
            "tool_choice",
            "response_format",
        }
        assert call_kwargs["model"] == "gpt-4"
        assert call_kwargs["messages"] == messages
        assert call_kwargs["temperature"] == 0.1
        assert call_kwargs["max_tokens"] == 10000
        assert call_kwargs["tools"] == tools
        assert call_kwargs["tool_choice"] == tool_choice
        assert call_kwargs["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "host_port",
                "strict": True,
                "schema": spec.schema,
            },
        }

    @pytest.mark.asyncio
    async def test_chat_messages_invalid_strict_schema_fails_before_api_call(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        invalid_spec = StructuredOutputSpec(
            name="invalid_schema",
            schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host"],  # Missing "port" for OpenAI strict mode
                "additionalProperties": False,
            },
        )

        with pytest.raises(LLMConfigurationError, match="Invalid strict schema"):
            await client.chat_messages(
                [{"role": "user", "content": "parse this"}],
                structured_output=invalid_spec,
            )
        assert mock.chat.completions.create.call_count == 0


class TestOpenAIChatClientStructuredUsage:
    """Tests for structured payload extraction in usage-tracking flows."""

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_populates_structured_output(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content='{"host":"192.168.1.10","port":443}'
        )
        spec = StructuredOutputSpec(
            name="host_port",
            schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host", "port"],
                "additionalProperties": False,
            },
        )

        response = await client.chat_messages_with_usage(
            [{"role": "user", "content": "parse this"}],
            structured_output=spec,
        )

        assert response.structured_output == {"host": "192.168.1.10", "port": 443}

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_plain_request_kwargs_parity(
        self, client_with_mock
    ) -> None:
        """Usage-returning chat keeps the same Chat Completions request shape."""
        client, mock = client_with_mock
        messages = [{"role": "user", "content": "Hello"}]

        await client.chat_messages_with_usage(messages, temperature=0.2, max_tokens=456)

        assert mock.chat.completions.create.call_args.kwargs == {
            "model": "gpt-4",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 456,
        }

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_passes_tools_with_structured_output(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content='{"host":"192.168.1.10","port":443}'
        )
        spec = StructuredOutputSpec(
            name="host_port",
            schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host", "port"],
                "additionalProperties": False,
            },
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "tool__nmap",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        await client.chat_messages_with_usage(
            [{"role": "user", "content": "parse this"}],
            structured_output=spec,
            tools=tools,
            tool_choice="none",
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["tools"] == tools
        assert call_kwargs["tool_choice"] == "none"

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_invalid_structured_content_raises(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(content="not-json")
        spec = StructuredOutputSpec(
            name="host_port",
            schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                },
                "required": ["host", "port"],
                "additionalProperties": False,
            },
        )

        with pytest.raises(LLMStructuredOutputParseError) as exc_info:
            await client.chat_messages_with_usage(
                [{"role": "user", "content": "parse this"}],
                structured_output=spec,
            )

        assert exc_info.value.schema_name == "host_port"
        assert exc_info.value.raw_content == "not-json"
        assert exc_info.value.parse_reason == "json_decode_error"
        assert exc_info.value.diagnostics.get("finish_reason") is None


# ---------------------------------------------------------------------------
# Streaming Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientStreaming:
    """Tests for the stream_chat_messages() method."""
    
    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self, client_with_mock) -> None:
        """Test that streaming yields text chunks."""
        client, mock = client_with_mock
        
        # Create async generator for stream
        async def mock_stream():
            for chunk in ["Hello", ", ", "world", "!"]:
                yield MockStreamEvent(chunk)
        
        mock.chat.completions.create.return_value = mock_stream()
        
        chunks = []
        async for chunk in client.stream_chat_messages([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)
        
        assert chunks == ["Hello", ", ", "world", "!"]
    
    @pytest.mark.asyncio
    async def test_stream_skips_empty_chunks(self, client_with_mock) -> None:
        """Test that streaming skips empty/None chunks."""
        client, mock = client_with_mock
        
        async def mock_stream():
            yield MockStreamEvent("Hello")
            yield MockStreamEvent(None)
            yield MockStreamEvent("")
            yield MockStreamEvent("World")
        
        mock.chat.completions.create.return_value = mock_stream()
        
        chunks = []
        async for chunk in client.stream_chat_messages([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)
        
        assert chunks == ["Hello", "World"]
    
    @pytest.mark.asyncio
    async def test_stream_passes_parameters(self, client_with_mock) -> None:
        """Test that streaming passes correct parameters."""
        client, mock = client_with_mock
        
        async def mock_stream():
            yield MockStreamEvent("chunk")
        
        mock.chat.completions.create.return_value = mock_stream()
        
        messages = [{"role": "user", "content": "test"}]
        async for _ in client.stream_chat_messages(
            messages, temperature=0.7, max_tokens=500
        ):
            pass
        
        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["stream"] is True
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 500

    @pytest.mark.asyncio
    async def test_stream_with_usage_captures_final_usage_after_consumption(
        self, client_with_mock
    ) -> None:
        """Streaming usage remains available only after the stream is consumed."""
        client, mock = client_with_mock
        final_chunk = MagicMock()
        final_chunk.choices = []
        final_chunk.usage = MagicMock()
        final_chunk.usage.prompt_tokens = 6
        final_chunk.usage.completion_tokens = 3
        final_chunk.usage.total_tokens = 9
        final_chunk.usage.prompt_tokens_details = MagicMock()
        final_chunk.usage.prompt_tokens_details.cached_tokens = 2

        async def mock_stream():
            yield MockStreamEvent("Hello")
            yield final_chunk

        mock.chat.completions.create.return_value = mock_stream()

        stream_response = await client.stream_chat_messages_with_usage(
            [{"role": "user", "content": "Hi"}]
        )
        chunks = []
        async for chunk in stream_response.content_iterator:
            chunks.append(chunk)

        usage = stream_response.get_final_usage()

        assert chunks == ["Hello"]
        assert usage is not None
        assert usage.prompt_tokens == 6
        assert usage.completion_tokens == 3
        assert usage.total_tokens == 9
        assert usage.cached_tokens == 2
        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["stream"] is True
        assert call_kwargs["stream_options"] == {"include_usage": True}

    @pytest.mark.asyncio
    async def test_stream_refusal_waits_for_final_usage_chunk(
        self, client_with_mock
    ) -> None:
        """Refusal outcomes retain usage delivered after the finish chunk."""
        client, mock = client_with_mock
        finish_chunk = MockStreamEvent(
            None,
            refusal="Blocked by policy.",
            finish_reason="stop",
        )
        finish_chunk.id = "chatcmpl_refusal_usage"
        usage_chunk = MagicMock()
        usage_chunk.choices = []
        usage_chunk.usage = MagicMock()
        usage_chunk.usage.prompt_tokens = 8
        usage_chunk.usage.completion_tokens = 2
        usage_chunk.usage.total_tokens = 10
        usage_chunk.usage.prompt_tokens_details = MagicMock()
        usage_chunk.usage.prompt_tokens_details.cached_tokens = 3

        async def mock_stream():
            yield MockStreamEvent("Partial answer")
            yield finish_chunk
            yield usage_chunk

        mock.chat.completions.create.return_value = mock_stream()
        stream_response = await client.stream_chat_messages_with_usage(
            [{"role": "user", "content": "request"}]
        )
        chunks: list[str] = []

        with pytest.raises(LLMRefusalError) as exc_info:
            async for chunk in stream_response.content_iterator:
                chunks.append(chunk)

        outcome = exc_info.value.outcome
        assert chunks == ["Partial answer"]
        assert outcome.explanation == "Blocked by policy."
        assert outcome.partial_content == "Partial answer"
        assert outcome.response_id == "chatcmpl_refusal_usage"
        assert outcome.usage is not None
        assert outcome.usage.prompt_tokens == 8
        assert outcome.usage.completion_tokens == 2
        assert outcome.usage.total_tokens == 10
        assert outcome.usage.cached_tokens == 3
        assert stream_response.get_final_usage() is outcome.usage

    @pytest.mark.asyncio
    async def test_stream_refusal_without_finish_reason_preserves_final_usage(
        self, client_with_mock
    ) -> None:
        """Clean exhaustion raises the refusal after capturing the usage chunk."""
        client, mock = client_with_mock
        refusal_chunk = MockStreamEvent(None, refusal="Blocked by policy.")
        refusal_chunk.id = "chatcmpl_refusal_eof_usage"
        usage_chunk = MagicMock()
        usage_chunk.choices = []
        usage_chunk.usage = MagicMock()
        usage_chunk.usage.prompt_tokens = 13
        usage_chunk.usage.completion_tokens = 5
        usage_chunk.usage.total_tokens = 18
        usage_chunk.usage.prompt_tokens_details = MagicMock()
        usage_chunk.usage.prompt_tokens_details.cached_tokens = 4

        async def mock_stream():
            yield MockStreamEvent("Partial answer")
            yield refusal_chunk
            yield usage_chunk

        mock.chat.completions.create.return_value = mock_stream()
        stream_response = await client.stream_chat_messages_with_usage(
            [{"role": "user", "content": "request"}]
        )
        chunks: list[str] = []

        with pytest.raises(LLMRefusalError) as exc_info:
            async for chunk in stream_response.content_iterator:
                chunks.append(chunk)

        outcome = exc_info.value.outcome
        assert chunks == ["Partial answer"]
        assert outcome.explanation == "Blocked by policy."
        assert outcome.partial_content == "Partial answer"
        assert outcome.response_id == "chatcmpl_refusal_eof_usage"
        assert outcome.usage is not None
        assert outcome.usage.prompt_tokens == 13
        assert outcome.usage.completion_tokens == 5
        assert outcome.usage.total_tokens == 18
        assert outcome.usage.cached_tokens == 4
        assert stream_response.get_final_usage() is outcome.usage


# ---------------------------------------------------------------------------
# Tool Calling Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientToolCalling:
    """Tests for the chat_with_tools() method."""
    
    @pytest.mark.asyncio
    async def test_chat_with_tools_returns_result(self, client_with_mock) -> None:
        """Test that tool calling returns ToolCallResult."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content="I'll search for that.",
            tool_calls=[
                MockToolCall("call_1", "search_web", '{"query": "test"}'),
            ],
        )
        
        result = await client.chat_with_tools(
            "You can search.",
            "Find info about AI",
            tools=[{"type": "function", "function": {"name": "search_web"}}],
        )
        
        assert isinstance(result, ToolCallResult)
        assert result.content == "I'll search for that."
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "search_web"
        assert result.tool_calls[0].arguments == '{"query": "test"}'
    
    @pytest.mark.asyncio
    async def test_chat_with_tools_no_tool_calls(self, client_with_mock) -> None:
        """Test result when model doesn't call tools."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content="I don't need to search for that.",
            tool_calls=None,
        )
        
        result = await client.chat_with_tools(
            "You can search.",
            "What is 2+2?",
            tools=[{"type": "function", "function": {"name": "search_web"}}],
        )
        
        assert result.content == "I don't need to search for that."
        assert result.tool_calls is None
    
    @pytest.mark.asyncio
    async def test_chat_with_tools_multiple_calls(self, client_with_mock) -> None:
        """Test result with multiple tool calls."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(
            content=None,
            tool_calls=[
                MockToolCall("call_1", "search_web", '{"query": "AI"}'),
                MockToolCall("call_2", "get_weather", '{"city": "NYC"}'),
            ],
        )
        
        result = await client.chat_with_tools(
            "System",
            "User",
            tools=[],
        )
        
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].name == "search_web"
        assert result.tool_calls[1].name == "get_weather"
    
    @pytest.mark.asyncio
    async def test_chat_with_tools_passes_tool_choice(self, client_with_mock) -> None:
        """Test that tool_choice is passed to API."""
        client, mock = client_with_mock
        
        await client.chat_with_tools(
            "System",
            "User",
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
            tool_choice={"type": "function", "function": {"name": "my_tool"}},
        )
        
        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "my_tool"},
        }

    @pytest.mark.asyncio
    async def test_chat_with_tools_translates_neutral_tool_spec_and_choice(
        self, client_with_mock
    ) -> None:
        """Neutral tool contracts should translate to the existing Chat payload."""
        client, mock = client_with_mock
        spec = FunctionToolSpec(
            tool_id="net.nmap",
            name="tool__net_nmap",
            description="Run nmap",
            parameters_schema={"type": "object", "properties": {}},
        )

        await client.chat_with_tools(
            "System",
            "User",
            tools=[spec],
            tool_choice=ToolChoice("specific", function_name="tool__net_nmap"),
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "tool__net_nmap",
                    "description": "Run nmap",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        assert call_kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "tool__net_nmap"},
        }

    @pytest.mark.asyncio
    async def test_chat_with_tools_passes_parallel_tool_calls(self, client_with_mock) -> None:
        """Test that parallel_tool_calls is passed to API when set."""
        client, mock = client_with_mock

        await client.chat_with_tools(
            "System",
            "User",
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
            parallel_tool_calls=False,
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["parallel_tool_calls"] is False

    @pytest.mark.asyncio
    async def test_chat_with_tools_with_usage_passes_parallel_tool_calls(
        self, client_with_mock
    ) -> None:
        """Test that usage-tracked tool calls pass parallel_tool_calls."""
        client, mock = client_with_mock

        await client.chat_with_tools_with_usage(
            "System",
            "User",
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
            parallel_tool_calls=True,
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["parallel_tool_calls"] is True
    
    @pytest.mark.asyncio
    async def test_chat_with_tools_raw_response_preserved(self, client_with_mock) -> None:
        """Test that raw response is preserved in result."""
        client, mock = client_with_mock
        mock_response = MockResponse("content")
        mock.chat.completions.create.return_value = mock_response
        
        result = await client.chat_with_tools("System", "User", tools=[])
        
        assert result.raw is mock_response


# ---------------------------------------------------------------------------
# Retry Logic Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientRetry:
    """Tests for retry behavior."""
    
    @pytest.mark.asyncio
    async def test_retries_on_api_error(self, client_with_mock) -> None:
        """Test that transient errors trigger retry."""
        client, mock = client_with_mock
        
        # Fail twice, then succeed
        mock.chat.completions.create.side_effect = [
            Exception("Temporary error"),
            Exception("Temporary error"),
            MockResponse("Success after retry"),
        ]
        
        result = await client.chat_messages(
            [{"role": "user", "content": "test"}],
            _retries=2,
        )
        
        assert result == "Success after retry"
        assert mock.chat.completions.create.call_count == 3
    
    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, client_with_mock) -> None:
        """Test that error is raised after max retries."""
        client, mock = client_with_mock
        mock.chat.completions.create.side_effect = Exception("Persistent error")
        
        with pytest.raises(LLMAPIError):
            await client.chat_messages(
                [{"role": "user", "content": "test"}],
                _retries=2,
            )
        
        # 3 attempts total (initial + 2 retries)
        assert mock.chat.completions.create.call_count == 3
    
    @pytest.mark.asyncio
    async def test_response_parse_error_retries_then_fails_closed(
        self, client_with_mock
    ) -> None:
        """Retry empty provider content but fail closed after three attempts."""
        client, mock = client_with_mock
        mock.chat.completions.create.return_value = MockResponse(content=None)
        
        with pytest.raises(LLMResponseError):
            await client.chat_messages(
                [{"role": "user", "content": "test"}],
                _retries=5,
            )
        
        assert mock.chat.completions.create.call_count == 3


# ---------------------------------------------------------------------------
# Factory Integration Tests
# ---------------------------------------------------------------------------


class TestOpenAIChatClientFactoryIntegration:
    """Tests for factory integration."""
    
    def test_factory_returns_openai_chat_client(self) -> None:
        """Test that factory creates OpenAIChatClient for GPT-4 models."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory
        
        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            
            client = LLMClientFactory.get_client(model="gpt-4", api_key="test-key")
            
            assert isinstance(client, OpenAIChatClient)
            assert client.model == "gpt-4"
    
    def test_factory_handles_gpt4_variants(self) -> None:
        """Test that factory handles GPT-4 model variants."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory
        
        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            
            variants = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4-0125-preview"]
            for variant in variants:
                client = LLMClientFactory.get_client(model=variant, api_key="key")
                assert isinstance(client, OpenAIChatClient)
    
    def test_factory_handles_gpt35_variants(self) -> None:
        """Test that factory handles GPT-3.5 model variants."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory
        
        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            
            variants = ["gpt-3.5-turbo", "gpt-3.5-turbo-16k"]
            for variant in variants:
                client = LLMClientFactory.get_client(model=variant, api_key="key")
                assert isinstance(client, OpenAIChatClient)
