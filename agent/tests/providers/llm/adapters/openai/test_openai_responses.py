"""Tests for OpenAIResponsesClient provider.

Tests cover:
- Basic chat functionality
- Multi-turn conversations (message format conversion)
- Streaming responses
- Tool/function calling with format conversion
- Error handling and retries
- Empty response handling
- Factory integration and registration
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
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
# Mock Fixtures for Responses API
# ---------------------------------------------------------------------------


class MockResponsesResponse:
    """Mock Responses API response object."""

    def __init__(
        self,
        output_text: str | None = "Test response",
        output: list | None = None,
        incomplete_reason: str | None = None,
        response_id: str | None = None,
    ) -> None:
        self.id = response_id
        self.output_text = output_text
        self.output = output or []
        self.incomplete_details = (
            MagicMock(reason=incomplete_reason) if incomplete_reason else None
        )


class MockStreamEvent:
    """Mock Responses API streaming event."""

    def __init__(
        self,
        event_type: str = "response.output_text.delta",
        delta: str | None = None,
        refusal: str | None = None,
        response: object | None = None,
    ) -> None:
        self.type = event_type
        self.delta = delta
        self.refusal = refusal
        self.response = response


class MockAsyncContextManager:
    """Mock async context manager for streaming."""

    def __init__(self, events: list) -> None:
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration


@pytest.fixture
def mock_openai_client():
    """Create a mock AsyncOpenAI client for Responses API."""
    mock_client = MagicMock()
    mock_client.responses = MagicMock()
    mock_client.responses.create = AsyncMock(
        return_value=MockResponsesResponse("Test response")
    )
    mock_client.responses.stream = MagicMock()
    return mock_client


@pytest.fixture
def client_with_mock(mock_openai_client):
    """Create OpenAIResponsesClient with mocked underlying client."""
    with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
        mock_openai.AsyncOpenAI.return_value = mock_openai_client
        client = OpenAIResponsesClient(api_key="test-key", model="gpt-5")
        client._client = mock_openai_client
        return client, mock_openai_client


@pytest.mark.asyncio
async def test_responses_refusal_block_is_not_retried(client_with_mock) -> None:
    client, mock = client_with_mock
    mock.responses.create.return_value = MockResponsesResponse(
        output_text=None,
        output=[
            {
                "type": "message",
                "content": [
                    {"type": "refusal", "refusal": "Blocked by policy."}
                ],
            }
        ],
        response_id="resp_refusal",
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await client.chat_messages(
            [{"role": "user", "content": "request"}],
            _retries=2,
        )

    assert exc_info.value.outcome.provider == "openai"
    assert exc_info.value.outcome.model == "gpt-5"
    assert exc_info.value.outcome.category == "content_filter"
    assert exc_info.value.outcome.explanation == "Blocked by policy."
    assert exc_info.value.outcome.response_id == "resp_refusal"
    assert mock.responses.create.call_count == 1


@pytest.mark.asyncio
async def test_responses_content_filter_incomplete_is_refusal(client_with_mock) -> None:
    client, mock = client_with_mock
    mock.responses.create.return_value = MockResponsesResponse(
        output_text=None,
        incomplete_reason="content_filter",
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await client.chat_messages([{"role": "user", "content": "request"}])

    assert exc_info.value.outcome.explanation is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    (
        "chat",
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
    """Every non-stream Responses surface retains coexisting output text."""
    client, mock = client_with_mock
    mock.responses.create.return_value = MockResponsesResponse(
        output_text="Partial answer",
        incomplete_reason="content_filter",
    )

    method = getattr(client, method_name)
    with pytest.raises(LLMRefusalError) as exc_info:
        if "tools" in method_name:
            await method(
                "System",
                "User",
                tools=[{"type": "function", "function": {"name": "my_tool"}}],
            )
        elif method_name == "chat":
            await method("System", "User")
        else:
            await method([{"role": "user", "content": "request"}])

    assert exc_info.value.outcome.partial_content == "Partial answer"


@pytest.mark.asyncio
async def test_responses_stream_refusal_preserves_partial_content(client_with_mock) -> None:
    client, mock = client_with_mock
    final_response = MockResponsesResponse(
        output_text=None,
        output=[
            {
                "type": "message",
                "content": [
                    {"type": "refusal", "refusal": "Blocked by policy."}
                ],
            }
        ],
        response_id="resp_stream_refusal",
    )
    final_response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=1,
    )
    mock.responses.stream.return_value = MockAsyncContextManager(
        [
            MockStreamEvent(delta="Partial answer"),
            MockStreamEvent(
                event_type="response.refusal.delta",
                delta="Blocked by ",
            ),
            MockStreamEvent(
                event_type="response.refusal.done",
                refusal="Blocked by policy.",
            ),
            MockStreamEvent(
                event_type="response.completed",
                response=final_response,
            ),
        ]
    )
    chunks: list[str] = []

    with pytest.raises(LLMRefusalError) as exc_info:
        async for chunk in client.stream_chat_messages(
            [{"role": "user", "content": "request"}]
        ):
            chunks.append(chunk)

    assert chunks == ["Partial answer"]
    assert exc_info.value.outcome.partial_content == "Partial answer"
    assert exc_info.value.outcome.explanation == "Blocked by policy."
    assert exc_info.value.outcome.response_id == "resp_stream_refusal"
    assert exc_info.value.outcome.usage is not None
    assert exc_info.value.outcome.usage.prompt_tokens == 10
    assert exc_info.value.outcome.usage.completion_tokens == 5


@pytest.mark.asyncio
async def test_responses_stream_refusal_raises_after_truncated_stream(
    client_with_mock,
) -> None:
    client, mock = client_with_mock
    mock.responses.stream.return_value = MockAsyncContextManager(
        [
            MockStreamEvent(delta="Partial answer"),
            MockStreamEvent(
                event_type="response.refusal.delta",
                delta="Blocked by ",
            ),
            MockStreamEvent(
                event_type="response.refusal.done",
                refusal="Blocked by policy.",
            ),
        ]
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        async for _chunk in client.stream_chat_messages(
            [{"role": "user", "content": "request"}]
        ):
            pass

    assert exc_info.value.outcome.partial_content == "Partial answer"
    assert exc_info.value.outcome.explanation == "Blocked by policy."
    assert exc_info.value.outcome.response_id is None
    assert exc_info.value.outcome.usage is None


# ---------------------------------------------------------------------------
# Initialization Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientInit:
    """Tests for client initialization."""

    def test_init_with_defaults(self) -> None:
        """Test initialization with default model."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="test-key")

            assert client.model == "gpt-5"

    def test_init_with_custom_model(self) -> None:
        """Test initialization with custom model."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.2")

            assert client.model == "gpt-5.2"

    def test_init_default_reasoning_effort(self) -> None:
        """Test default reasoning effort follows canonical contract."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5")

            assert client._reasoning_effort == "minimal"

    def test_init_coerces_minimal_to_none_for_gpt52(self) -> None:
        """Test gpt-5.2 models coerce minimal effort to none."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.2")

            assert client._reasoning_effort == "none"

    def test_init_preserves_legacy_minimal_coercion_for_gpt52_variant(self) -> None:
        """Test gpt-5.2 compatibility variants preserve legacy minimal coercion."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.2-preview")

            assert client._reasoning_effort == "medium"

    def test_init_maps_minimal_to_medium_for_gpt52_pro(self) -> None:
        """Test gpt-5.2-pro maps minimal effort to medium."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.2-pro")

            assert client._reasoning_effort == "medium"

    def test_init_custom_reasoning_effort(self) -> None:
        """Test custom reasoning effort."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(
                api_key="test-key", model="gpt-5-pro", reasoning_effort="medium"
            )

            assert client._reasoning_effort == "medium"

    def test_init_rejects_xhigh_for_model_without_xhigh_profile(self) -> None:
        """Test xhigh is rejected when the selected model profile excludes it."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            with pytest.raises(LLMConfigurationError, match="models that support xhigh"):
                OpenAIResponsesClient(
                    api_key="test-key",
                    model="gpt-5.2",
                    reasoning_effort="xhigh",
                )

    def test_init_allows_xhigh_for_gpt52_pro(self) -> None:
        """Test xhigh is allowed for gpt-5.2-pro."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(
                api_key="test-key",
                model="gpt-5.2-pro",
                reasoning_effort="xhigh",
            )
            assert client._reasoning_effort == "xhigh"

    def test_init_allows_xhigh_for_new_xhigh_models(self) -> None:
        """Test xhigh is allowed by new model profiles that declare support."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            for model in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5"):
                client = OpenAIResponsesClient(
                    api_key="test-key",
                    model=model,
                    reasoning_effort="xhigh",
                )
                assert client._reasoning_effort == "xhigh"

    def test_init_uses_gpt56_profile_default_and_accepts_max(self) -> None:
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            for model in ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
                default_client = OpenAIResponsesClient(api_key="test-key", model=model)
                max_client = OpenAIResponsesClient(
                    api_key="test-key",
                    model=model,
                    reasoning_effort="max",
                )

                assert default_client._reasoning_effort == "medium"
                assert max_client._reasoning_effort == "max"

    def test_init_rejects_minimal_for_gpt56(self) -> None:
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            with pytest.raises(LLMConfigurationError, match="Allowed values"):
                OpenAIResponsesClient(
                    api_key="test-key",
                    model="gpt-5.6-sol",
                    reasoning_effort="minimal",
                )

    def test_init_maps_default_minimal_for_new_standard_models(self) -> None:
        """Test default minimal effort maps to supported effort for new standard models."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            for model in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5"):
                client = OpenAIResponsesClient(api_key="test-key", model=model)
                assert client._reasoning_effort == "medium"

    def test_init_maps_default_minimal_for_new_pro_models(self) -> None:
        """Test default minimal effort maps to each new Pro model's supported default."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()

            gpt54_pro = OpenAIResponsesClient(api_key="test-key", model="gpt-5.4-pro")
            gpt55_pro = OpenAIResponsesClient(api_key="test-key", model="gpt-5.5-pro")

            assert gpt54_pro._reasoning_effort == "medium"
            assert gpt55_pro._reasoning_effort == "high"

    def test_model_property(self) -> None:
        """Test model property returns correct value."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5-mini")

            assert client.model == "gpt-5-mini"


# ---------------------------------------------------------------------------
# Chat Method Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientChat:
    """Tests for the chat() method."""

    @pytest.mark.asyncio
    async def test_chat_returns_string(self, client_with_mock) -> None:
        """Test that chat returns a string response."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse("Hello, I'm GPT-5!")

        result = await client.chat("You are helpful.", "Hello!")

        assert result == "Hello, I'm GPT-5!"
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_chat_passes_instructions(self, client_with_mock) -> None:
        """Test that chat passes system prompt as instructions."""
        client, mock = client_with_mock

        await client.chat("System prompt here", "User message")

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["instructions"] == "System prompt here"

    @pytest.mark.asyncio
    async def test_chat_passes_input_format(self, client_with_mock) -> None:
        """Test that chat passes input in Responses API format."""
        client, mock = client_with_mock

        await client.chat("System", "User message")

        call_kwargs = mock.responses.create.call_args.kwargs
        expected_input = [
            {"role": "user", "content": [{"type": "input_text", "text": "User message"}]}
        ]
        assert call_kwargs["input"] == expected_input

    @pytest.mark.asyncio
    async def test_chat_passes_reasoning_effort(self, client_with_mock) -> None:
        """Test that chat passes reasoning effort (default: minimal)."""
        client, mock = client_with_mock

        await client.chat("System", "User")

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["reasoning"] == {"effort": "minimal"}

    @pytest.mark.asyncio
    async def test_chat_override_reasoning_effort(self, client_with_mock) -> None:
        """Test that chat can override reasoning effort."""
        client, mock = client_with_mock

        await client.chat("System", "User", reasoning_effort="high")

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["reasoning"] == {"effort": "high"}

    @pytest.mark.asyncio
    async def test_chat_coerces_minimal_to_none_for_gpt52(self, mock_openai_client) -> None:
        """Test gpt-5.2 request payload never sends minimal."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_openai_client
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.2")
            client._client = mock_openai_client

            await client.chat("System", "User")

            call_kwargs = mock_openai_client.responses.create.call_args.kwargs
            assert call_kwargs["reasoning"] == {"effort": "none"}

    @pytest.mark.asyncio
    async def test_chat_maps_minimal_to_medium_for_gpt52_pro(self, mock_openai_client) -> None:
        """Test gpt-5.2-pro request payload uses medium when effort maps from minimal."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_openai_client
            client = OpenAIResponsesClient(api_key="test-key", model="gpt-5.2-pro")
            client._client = mock_openai_client

            await client.chat("System", "User")

            call_kwargs = mock_openai_client.responses.create.call_args.kwargs
            assert call_kwargs["reasoning"] == {"effort": "medium"}

    @pytest.mark.asyncio
    async def test_chat_rejects_xhigh_for_model_without_xhigh_profile(self, client_with_mock) -> None:
        """Test that xhigh fails fast when the model profile excludes it."""
        client, mock = client_with_mock

        with pytest.raises(LLMConfigurationError, match="models that support xhigh"):
            await client.chat("System", "User", reasoning_effort="xhigh")
        assert mock.responses.create.call_count == 0

    @pytest.mark.asyncio
    async def test_chat_uses_default_max_tokens(self, client_with_mock) -> None:
        """Test that chat uses default max tokens."""
        client, mock = client_with_mock

        await client.chat("System", "User")

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["max_output_tokens"] == 10000  # High default for testing

    @pytest.mark.asyncio
    async def test_chat_custom_max_tokens(self, client_with_mock) -> None:
        """Test that chat accepts custom max tokens."""
        client, mock = client_with_mock

        await client.chat("System", "User", max_tokens=500)

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["max_output_tokens"] == 500

    @pytest.mark.asyncio
    async def test_chat_request_kwargs_parity(self, client_with_mock) -> None:
        """Single-turn Responses payload keeps current field names and defaults."""
        client, mock = client_with_mock

        await client.chat("System", "User", max_tokens=321, reasoning_effort="high")

        assert mock.responses.create.call_args.kwargs == {
            "model": "gpt-5",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "User"}],
                }
            ],
            "instructions": "System",
            "max_output_tokens": 321,
            "reasoning": {"effort": "high"},
        }


# ---------------------------------------------------------------------------
# Chat Messages Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientChatMessages:
    """Tests for the chat_messages() method."""

    @pytest.mark.asyncio
    async def test_chat_messages_with_history(self, client_with_mock) -> None:
        """Test multi-turn conversation."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse("Response to history")

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]

        result = await client.chat_messages(messages)

        assert result == "Response to history"

    @pytest.mark.asyncio
    async def test_chat_messages_converts_system_to_instructions(
        self, client_with_mock
    ) -> None:
        """Test that system message becomes instructions."""
        client, mock = client_with_mock

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]

        await client.chat_messages(messages)

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["instructions"] == "You are a helpful assistant."

    @pytest.mark.asyncio
    async def test_chat_messages_excludes_system_from_input(
        self, client_with_mock
    ) -> None:
        """Test that system message is not included in input array."""
        client, mock = client_with_mock

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "How are you?"},
        ]

        await client.chat_messages(messages)

        call_kwargs = mock.responses.create.call_args.kwargs
        # Should have 3 items: user, assistant, user (no system)
        assert len(call_kwargs["input"]) == 3

    @pytest.mark.asyncio
    async def test_chat_messages_formats_user_messages(self, client_with_mock) -> None:
        """Test that user messages are formatted correctly."""
        client, mock = client_with_mock

        messages = [{"role": "user", "content": "Hello there"}]

        await client.chat_messages(messages)

        call_kwargs = mock.responses.create.call_args.kwargs
        expected = {"role": "user", "content": [{"type": "input_text", "text": "Hello there"}]}
        assert call_kwargs["input"][0] == expected

    @pytest.mark.asyncio
    async def test_chat_messages_formats_assistant_messages(
        self, client_with_mock
    ) -> None:
        """Test that assistant messages are formatted correctly."""
        client, mock = client_with_mock

        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Bye"},
        ]

        await client.chat_messages(messages)

        call_kwargs = mock.responses.create.call_args.kwargs
        expected_assistant = {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello!"}],
        }
        assert call_kwargs["input"][1] == expected_assistant

    @pytest.mark.asyncio
    async def test_chat_messages_request_kwargs_parity(self, client_with_mock) -> None:
        """History-based Responses payload keeps its current request shape."""
        client, mock = client_with_mock
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Continue"},
        ]

        await client.chat_messages(messages, max_tokens=456, reasoning_effort="medium")

        assert mock.responses.create.call_args.kwargs == {
            "model": "gpt-5",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
                {"role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Continue"}],
                },
            ],
            "instructions": "System prompt",
            "max_output_tokens": 456,
            "reasoning": {"effort": "medium"},
        }

    @pytest.mark.asyncio
    async def test_chat_messages_empty_response_raises(self, client_with_mock) -> None:
        """Test that empty response raises LLMResponseError."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(output_text=None)

        with pytest.raises(LLMResponseError, match="empty content"):
            await client.chat_messages([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_chat_messages_whitespace_only_raises(self, client_with_mock) -> None:
        """Test that whitespace-only response raises LLMResponseError."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(output_text="   ")

        with pytest.raises(LLMResponseError, match="empty content"):
            await client.chat_messages([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_chat_messages_injects_text_format_for_structured_output(
        self, client_with_mock
    ) -> None:
        """Structured output requests should include Responses API text.format payload."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text='{"host":"192.168.1.10","port":443}'
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

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["text"]["format"]["type"] == "json_schema"
        assert call_kwargs["text"]["format"]["name"] == "host_port"
        assert call_kwargs["text"]["format"]["strict"] is True
        assert call_kwargs["text"]["format"]["schema"] == spec.schema

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
        assert mock.responses.create.call_count == 0


class TestOpenAIResponsesClientStructuredUsage:
    """Tests for structured payload extraction in usage-tracking flows."""

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_populates_structured_output(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text='{"host":"192.168.1.10","port":443}'
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
    async def test_chat_messages_with_usage_structured_request_kwargs_parity(
        self, client_with_mock
    ) -> None:
        """Usage-returning structured calls keep the Responses text.format payload."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text='{"host":"192.168.1.10","port":443}'
        )
        messages = [{"role": "user", "content": "parse this"}]
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

        await client.chat_messages_with_usage(
            messages,
            structured_output=spec,
            max_tokens=222,
            reasoning_effort="low",
        )

        assert mock.responses.create.call_args.kwargs == {
            "model": "gpt-5",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "parse this"}],
                }
            ],
            "instructions": "",
            "max_output_tokens": 222,
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "host_port",
                    "schema": spec.schema,
                    "strict": True,
                }
            },
        }

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_accepts_structured_output_without_output_text(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text=None,
            output=[
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "parsed": {"host": "192.168.1.10", "port": 443},
                        }
                    ],
                }
            ],
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

        assert response.content == '{"host":"192.168.1.10","port":443}'
        assert response.structured_output == {"host": "192.168.1.10", "port": 443}

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_passes_tools_with_structured_output(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text='{"host":"192.168.1.10","port":443}'
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
                    "description": "Run nmap",
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

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["tools"] == [
            {
                "type": "function",
                "name": "tool__nmap",
                "description": "Run nmap",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        assert call_kwargs["tool_choice"] == "none"

    @pytest.mark.asyncio
    async def test_chat_messages_with_usage_invalid_structured_content_raises(
        self, client_with_mock
    ) -> None:
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(output_text="not-json")
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
        assert exc_info.value.diagnostics.get("response_id") is None


# ---------------------------------------------------------------------------
# Streaming Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientStreaming:
    """Tests for the stream_chat_messages() method."""

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self, client_with_mock) -> None:
        """Test that streaming yields text chunks."""
        client, mock = client_with_mock

        events = [
            MockStreamEvent("response.output_text.delta", "Hello"),
            MockStreamEvent("response.output_text.delta", ", "),
            MockStreamEvent("response.output_text.delta", "world"),
            MockStreamEvent("response.output_text.delta", "!"),
        ]
        mock.responses.stream.return_value = MockAsyncContextManager(events)

        chunks = []
        async for chunk in client.stream_chat_messages([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)

        assert chunks == ["Hello", ", ", "world", "!"]

    @pytest.mark.asyncio
    async def test_stream_skips_non_delta_events(self, client_with_mock) -> None:
        """Test that streaming skips non-delta events."""
        client, mock = client_with_mock

        events = [
            MockStreamEvent("response.output_text.delta", "Hello"),
            MockStreamEvent("response.created", None),  # Not a delta event
            MockStreamEvent("response.output_text.delta", "World"),
        ]
        mock.responses.stream.return_value = MockAsyncContextManager(events)

        chunks = []
        async for chunk in client.stream_chat_messages([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)

        assert chunks == ["Hello", "World"]

    @pytest.mark.asyncio
    async def test_stream_skips_empty_deltas(self, client_with_mock) -> None:
        """Test that streaming skips empty/None deltas."""
        client, mock = client_with_mock

        events = [
            MockStreamEvent("response.output_text.delta", "Hello"),
            MockStreamEvent("response.output_text.delta", None),
            MockStreamEvent("response.output_text.delta", ""),
            MockStreamEvent("response.output_text.delta", "World"),
        ]
        mock.responses.stream.return_value = MockAsyncContextManager(events)

        chunks = []
        async for chunk in client.stream_chat_messages([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)

        assert chunks == ["Hello", "World"]

    @pytest.mark.asyncio
    async def test_stream_passes_parameters(self, client_with_mock) -> None:
        """Test that streaming passes correct parameters."""
        client, mock = client_with_mock
        mock.responses.stream.return_value = MockAsyncContextManager([])

        messages = [{"role": "user", "content": "test"}]
        async for _ in client.stream_chat_messages(
            messages, max_tokens=500, reasoning_effort="medium"
        ):
            pass

        call_kwargs = mock.responses.stream.call_args.kwargs
        assert call_kwargs["max_output_tokens"] == 500
        assert call_kwargs["reasoning"] == {"effort": "medium"}


# ---------------------------------------------------------------------------
# Tool Calling Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientToolCalling:
    """Tests for the chat_with_tools() method."""

    @pytest.mark.asyncio
    async def test_chat_with_tools_returns_result(self, client_with_mock) -> None:
        """Test that tool calling returns ToolCallResult."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text="I'll search for that.",
            output=[
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search_web",
                    "arguments": '{"query": "test"}',
                }
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

    @pytest.mark.asyncio
    async def test_chat_with_tools_no_tool_calls(self, client_with_mock) -> None:
        """Test result when model doesn't call tools."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(
            output_text="I don't need to search for that.",
            output=[],
        )

        result = await client.chat_with_tools(
            "You can search.",
            "What is 2+2?",
            tools=[{"type": "function", "function": {"name": "search_web"}}],
        )

        assert result.content == "I don't need to search for that."
        assert result.tool_calls is None

    @pytest.mark.asyncio
    async def test_chat_with_tools_converts_tool_format(self, client_with_mock) -> None:
        """Test that tools are converted to Responses API format."""
        client, mock = client_with_mock

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        await client.chat_with_tools("System", "User", tools)

        call_kwargs = mock.responses.create.call_args.kwargs
        expected_tools = [
            {
                "type": "function",
                "name": "search_web",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        assert call_kwargs["tools"] == expected_tools

    @pytest.mark.asyncio
    async def test_chat_with_tools_passes_tool_choice(self, client_with_mock) -> None:
        """Test that tool_choice is passed to API."""
        client, mock = client_with_mock

        await client.chat_with_tools(
            "System",
            "User",
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
            tool_choice="auto",
        )

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_chat_with_tools_translates_neutral_tool_spec_and_choice(
        self, client_with_mock
    ) -> None:
        """Neutral tool contracts should translate to Responses request payloads."""
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

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["tools"] == [
            {
                "type": "function",
                "name": "tool__net_nmap",
                "description": "Run nmap",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        assert call_kwargs["tool_choice"] == {
            "type": "function",
            "name": "tool__net_nmap",
        }

    @pytest.mark.asyncio
    async def test_chat_with_tools_passes_parallel_tool_calls(
        self, client_with_mock
    ) -> None:
        """Test that parallel_tool_calls is passed to API when set."""
        client, mock = client_with_mock

        await client.chat_with_tools(
            "System",
            "User",
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
            parallel_tool_calls=False,
        )

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["parallel_tool_calls"] is False

    @pytest.mark.asyncio
    async def test_chat_with_tools_request_kwargs_parity(self, client_with_mock) -> None:
        """Tool calls keep Responses-native flattened tools and converted choice shape."""
        client, mock = client_with_mock
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        await client.chat_with_tools(
            "System",
            "User",
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "search_web"}},
            max_tokens=333,
            reasoning_effort="high",
            parallel_tool_calls=False,
        )

        assert mock.responses.create.call_args.kwargs == {
            "model": "gpt-5",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "User"}],
                }
            ],
            "instructions": "System",
            "max_output_tokens": 333,
            "reasoning": {"effort": "high"},
            "tools": [
                {
                    "type": "function",
                    "name": "search_web",
                    "description": "Search the web",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "function", "name": "search_web"},
            "parallel_tool_calls": False,
        }

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

        call_kwargs = mock.responses.create.call_args.kwargs
        assert call_kwargs["parallel_tool_calls"] is True

    @pytest.mark.asyncio
    async def test_chat_with_tools_raw_response_preserved(
        self, client_with_mock
    ) -> None:
        """Test that raw response is preserved in result."""
        client, mock = client_with_mock
        mock_response = MockResponsesResponse("content")
        mock.responses.create.return_value = mock_response

        result = await client.chat_with_tools("System", "User", tools=[])

        assert result.raw is mock_response


# ---------------------------------------------------------------------------
# Message Conversion Tests
# ---------------------------------------------------------------------------


class TestMessageConversion:
    """Tests for message format conversion."""

    def test_convert_messages_extracts_system(self) -> None:
        """Test that system message is extracted to instructions."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            messages = [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ]

            system_prompt, input_messages = client._convert_messages_to_input(messages)

            assert system_prompt == "You are helpful."
            assert len(input_messages) == 1

    def test_convert_messages_handles_multimodal_content(self) -> None:
        """Test that multimodal content is handled."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Look at this"},
                        {"type": "image_url", "url": "..."},
                    ],
                }
            ]

            _, input_messages = client._convert_messages_to_input(messages)

            # Should extract text content
            assert input_messages[0]["content"][0]["text"] == "Look at this"

    def test_convert_messages_preserves_legacy_assistant_output_text_parts(self) -> None:
        """OpenAI-compatible assistant output_text lists remain valid history input."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            messages = [
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Previous answer"}],
                }
            ]

            _, input_messages = client._convert_messages_to_input(messages)

            assert input_messages == [
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Previous answer"}],
                }
            ]


# ---------------------------------------------------------------------------
# Tool Conversion Tests
# ---------------------------------------------------------------------------


class TestToolConversion:
    """Tests for tool format conversion."""

    def test_convert_tools_flattens_function(self) -> None:
        """Test that tools are converted from Chat Completions to Responses format."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "test_tool",
                        "description": "A test tool",
                        "parameters": {"type": "object"},
                    },
                }
            ]

            converted = client._convert_tools_for_responses(tools)

            assert len(converted) == 1
            assert converted[0]["type"] == "function"
            assert converted[0]["name"] == "test_tool"
            assert converted[0]["description"] == "A test tool"
            assert "function" not in converted[0]  # Flattened

    def test_convert_tools_passes_through_correct_format(self) -> None:
        """Test that correctly formatted tools pass through."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            # Already in Responses API format
            tools = [
                {
                    "type": "function",
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object"},
                }
            ]

            converted = client._convert_tools_for_responses(tools)

            assert converted == tools

    def test_convert_tools_translates_neutral_function_specs(self) -> None:
        """Test that neutral tool specs convert to Responses function tools."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            converted = client._convert_tools_for_responses(
                [
                    FunctionToolSpec(
                        tool_id="net.nmap",
                        name="tool__net_nmap",
                        description="Run nmap",
                        parameters_schema={"type": "object"},
                    )
                ]
            )

            assert converted == [
                {
                    "type": "function",
                    "name": "tool__net_nmap",
                    "description": "Run nmap",
                    "parameters": {"type": "object"},
                }
            ]


# ---------------------------------------------------------------------------
# Retry Logic Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientRetry:
    """Tests for retry behavior."""

    @pytest.mark.asyncio
    async def test_retries_on_api_error(self, client_with_mock) -> None:
        """Test that transient errors trigger retry."""
        client, mock = client_with_mock

        # Fail twice, then succeed
        mock.responses.create.side_effect = [
            Exception("Temporary error"),
            Exception("Temporary error"),
            MockResponsesResponse("Success after retry"),
        ]

        result = await client.chat_messages(
            [{"role": "user", "content": "test"}],
            _retries=2,
        )

        assert result == "Success after retry"
        assert mock.responses.create.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, client_with_mock) -> None:
        """Test that error is raised after max retries."""
        client, mock = client_with_mock
        mock.responses.create.side_effect = Exception("Persistent error")

        with pytest.raises(LLMAPIError):
            await client.chat_messages(
                [{"role": "user", "content": "test"}],
                _retries=2,
            )

        # 3 attempts total (initial + 2 retries)
        assert mock.responses.create.call_count == 3

    @pytest.mark.asyncio
    async def test_response_parse_error_retries_then_fails_closed(
        self, client_with_mock
    ) -> None:
        """Retry empty provider content but fail closed after three attempts."""
        client, mock = client_with_mock
        mock.responses.create.return_value = MockResponsesResponse(output_text=None)

        with pytest.raises(LLMResponseError):
            await client.chat_messages(
                [{"role": "user", "content": "test"}],
                _retries=5,
            )

        assert mock.responses.create.call_count == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "args"),
        [
            ("chat", ("System", "User")),
            ("chat_messages", ([{"role": "user", "content": "test"}],)),
            ("chat_messages_with_usage", ([{"role": "user", "content": "test"}],)),
            ("chat_with_tools", ("System", "User", [])),
            ("chat_with_tools_with_usage", ("System", "User", [])),
        ],
    )
    async def test_retry_count_and_exception_chaining_are_preserved(
        self,
        client_with_mock,
        method_name: str,
        args: tuple[Any, ...],
    ) -> None:
        """Each retrying public method should preserve attempts and exception chaining."""
        client, mock = client_with_mock
        mock.responses.create.side_effect = Exception("Persistent error")

        with pytest.raises(LLMAPIError) as exc_info:
            await getattr(client, method_name)(*args, _retries=2)

        assert mock.responses.create.call_count == 3
        assert exc_info.value.__cause__ is not None
        assert str(exc_info.value.__cause__) == "Persistent error"

    @pytest.mark.asyncio
    async def test_backoff_sleep_keeps_exponential_formula_and_jitter(
        self, client_with_mock
    ) -> None:
        """Backoff helper should preserve delay and jitter calculation."""
        client, _ = client_with_mock

        with patch("agent.providers.llm.adapters.openai.responses.retry.random.random", return_value=0.4), patch(
            "agent.providers.llm.adapters.openai.responses.retry.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:
            await client._backoff_sleep(3)

        assert mock_sleep.await_count == 1
        assert mock_sleep.await_args.args[0] == pytest.approx(2.2)

    @pytest.mark.asyncio
    async def test_configuration_error_still_fails_fast_without_retry(self, client_with_mock) -> None:
        """Invalid per-request reasoning settings should fail before any API call."""
        client, mock = client_with_mock

        with pytest.raises(LLMConfigurationError, match="models that support xhigh"):
            await client.chat_with_tools(
                "System",
                "User",
                [],
                reasoning_effort="xhigh",
            )

        assert mock.responses.create.call_count == 0

    @pytest.mark.asyncio
    async def test_structured_parse_failure_keeps_metrics_diagnostics_and_logger_provenance(
        self,
        client_with_mock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Structured parse failures should retain metrics, diagnostics, and facade logger name."""
        client, mock = client_with_mock
        response = MockResponsesResponse(output_text="not-json")
        response.id = "resp_123"
        response.status = "completed"
        response.incomplete_details = {"reason": "schema_mismatch"}
        mock.responses.create.return_value = response
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

        caplog.set_level("WARNING", logger="agent.providers.llm.adapters.openai.responses.client")
        with patch("agent.providers.llm.adapters.openai.responses.structured.safe_inc") as mock_safe_inc:
            with pytest.raises(LLMStructuredOutputParseError) as exc_info:
                await client.chat_messages_with_usage(
                    [{"role": "user", "content": "parse this"}],
                    structured_output=spec,
                )

        assert exc_info.value.diagnostics == {
            "response_id": "resp_123",
            "status": "completed",
            "incomplete_details": {"reason": "schema_mismatch"},
        }
        mock_safe_inc.assert_any_call("llm_structured_parse_failure_openai_responses_host_port")
        warning_records = [r for r in caplog.records if "Structured output parse failed" in r.message]
        assert warning_records
        assert all(
            record.name == "agent.providers.llm.adapters.openai.responses.client"
            for record in warning_records
        )


class TestOpenAIResponsesClientStreamingUsage:
    """Tests for streaming usage capture and done-event handling."""

    @pytest.mark.asyncio
    async def test_stream_with_usage_captures_usage_only_from_done_event_without_duplicate_text(
        self,
        client_with_mock,
    ) -> None:
        """Usage capture should read the completed response and ignore done-text duplication."""
        client, mock = client_with_mock

        final_response = MagicMock()
        final_response.usage = MagicMock()
        final_response.usage.input_tokens = 10
        final_response.usage.output_tokens = 5
        final_response.usage.reasoning_tokens = 1

        delta_event = MockStreamEvent("response.output_text.delta", "Hello")
        text_done_event = MockStreamEvent("response.output_text.done", "Hello")
        response_done_event = MagicMock()
        response_done_event.type = "response.done"
        response_done_event.response = final_response

        mock.responses.stream.return_value = MockAsyncContextManager(
            [delta_event, text_done_event, response_done_event]
        )

        stream = await client.stream_chat_messages_with_usage(
            [{"role": "user", "content": "Hi"}]
        )
        chunks = []
        async for chunk in stream.content_iterator:
            chunks.append(chunk)

        usage = stream.get_final_usage()

        assert chunks == ["Hello"]
        assert usage is not None
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 5
        assert usage.reasoning_tokens == 1

    @pytest.mark.asyncio
    async def test_stream_with_usage_refusal_waits_for_completed_response(
        self,
        client_with_mock,
    ) -> None:
        client, mock = client_with_mock
        final_response = MockResponsesResponse(
            output_text=None,
            output=[
                {
                    "type": "message",
                    "content": [
                        {"type": "refusal", "refusal": "Blocked by policy."}
                    ],
                }
            ],
            response_id="resp_stream_usage_refusal",
        )
        final_response.usage = MagicMock(
            input_tokens=12,
            output_tokens=3,
            reasoning_tokens=2,
        )
        mock.responses.stream.return_value = MockAsyncContextManager(
            [
                MockStreamEvent(delta="Partial answer"),
                MockStreamEvent(
                    event_type="response.refusal.delta",
                    delta="Blocked by ",
                ),
                MockStreamEvent(
                    event_type="response.refusal.done",
                    refusal="Blocked by policy.",
                ),
                MockStreamEvent(
                    event_type="response.completed",
                    response=final_response,
                ),
            ]
        )

        stream = await client.stream_chat_messages_with_usage(
            [{"role": "user", "content": "request"}]
        )
        chunks: list[str] = []

        with pytest.raises(LLMRefusalError) as exc_info:
            async for chunk in stream.content_iterator:
                chunks.append(chunk)

        assert chunks == ["Partial answer"]
        assert exc_info.value.outcome.partial_content == "Partial answer"
        assert exc_info.value.outcome.explanation == "Blocked by policy."
        assert exc_info.value.outcome.response_id == "resp_stream_usage_refusal"
        assert exc_info.value.outcome.usage is not None
        assert exc_info.value.outcome.usage.prompt_tokens == 12
        assert exc_info.value.outcome.usage.completion_tokens == 3
        assert stream.get_final_usage() == exc_info.value.outcome.usage


# ---------------------------------------------------------------------------
# Factory Integration Tests
# ---------------------------------------------------------------------------


class TestOpenAIResponsesClientFactoryIntegration:
    """Tests for factory integration."""

    def test_factory_returns_responses_client_for_gpt5(self) -> None:
        """Test that factory creates OpenAIResponsesClient for GPT-5 models."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory

        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()

            client = LLMClientFactory.get_client(model="gpt-5", api_key="test-key")

            assert isinstance(client, OpenAIResponsesClient)
            assert client.model == "gpt-5"

    def test_factory_handles_gpt5_variants(self) -> None:
        """Test that factory handles GPT-5 model variants."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory

        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()

            variants = [
                "gpt-5",
                "gpt-5-mini",
                "gpt-5-nano",
                "gpt-5-pro",
                "gpt-5.1",
                "gpt-5.2",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.4-nano",
                "gpt-5.5",
                "gpt-5.6",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
            ]
            for variant in variants:
                client = LLMClientFactory.get_client(model=variant, api_key="key")
                assert isinstance(client, OpenAIResponsesClient), f"Failed for {variant}"

    def test_factory_handles_gpt52_pro(self) -> None:
        """Test that factory handles gpt-5.2-pro specifically."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory

        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()

            client = LLMClientFactory.get_client(model="gpt-5.2-pro", api_key="key")
            assert isinstance(client, OpenAIResponsesClient)

    def test_factory_handles_new_non_listable_pro_models(self) -> None:
        """Test that exact hidden Pro models still resolve through Responses."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory

        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()

            for model in ("gpt-5.4-pro", "gpt-5.5-pro", "gpt-5.6"):
                client = LLMClientFactory.get_client(model=model, api_key="key")
                assert isinstance(client, OpenAIResponsesClient)

    def test_gpt5_registered_in_providers(self) -> None:
        """Test that GPT-5 prefixes are registered for legacy model-only fallback."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory

        providers = LLMClientFactory.list_prefix_registrations()

        expected_prefixes = [
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5-pro",
            "gpt-5.1",
            "gpt-5.2",
            "gpt-5.2-pro",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.4-pro",
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.6",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        ]
        for prefix in expected_prefixes:
            assert prefix in providers, f"Missing prefix: {prefix}"
            assert providers[prefix] == "OpenAIResponsesClient"

    def test_gpt4_is_routed_to_chat_provider(self) -> None:
        """Test that GPT-4 models route to Chat Completions provider."""
        from agent.providers.llm.factory.client_factory import LLMClientFactory

        with patch("agent.providers.llm.adapters.openai.chat.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()

            client = LLMClientFactory.get_client(model="gpt-4o-mini", api_key="key")
            assert isinstance(client, OpenAIChatClient)


# ---------------------------------------------------------------------------
# Output Extraction Tests
# ---------------------------------------------------------------------------


class TestOutputExtraction:
    """Tests for output text extraction."""

    def test_extract_output_text_from_attribute(self) -> None:
        """Test extraction from output_text attribute."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            response = MagicMock()
            response.output_text = "Direct output"

            result = client._extract_output_text(response)

            assert result == "Direct output"

    def test_extract_output_text_from_output_array(self) -> None:
        """Test extraction from output array."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            response = MagicMock()
            response.output_text = None
            response.output = [
                {
                    "content": [
                        {"type": "output_text", "text": "From array"}
                    ]
                }
            ]

            result = client._extract_output_text(response)

            assert result == "From array"

    def test_extract_output_text_returns_none_for_empty(self) -> None:
        """Test that empty response returns None."""
        with patch("agent.providers.llm.adapters.openai.responses.client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = MagicMock()
            client = OpenAIResponsesClient(api_key="key", model="gpt-5")

            response = MagicMock()
            response.output_text = None
            response.output = []

            result = client._extract_output_text(response)

            assert result is None
