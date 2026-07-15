"""Tests for the Anthropic Messages provider adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.providers.llm.adapters.anthropic.client import AnthropicMessagesClient
from agent.providers.llm.core.base import StructuredOutputSpec
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
    LLMStructuredOutputParseError,
)
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec, ToolChoice


class _FakeMessages:
    """Fake Anthropic messages resource used by adapter unit tests."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.stream_response: Any = None
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response

    def stream(self, **kwargs: Any) -> Any:
        self.stream_calls.append(kwargs)
        return self.stream_response


class _FakeStream:
    """Minimal async Anthropic stream with text chunks and final message."""

    def __init__(self, chunks: list[str], final_message: Any) -> None:
        self._chunks = chunks
        self._final_message = final_message
        self.text_stream = self._iter_chunks()

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def _iter_chunks(self):
        for chunk in self._chunks:
            yield chunk

    def get_final_message(self) -> Any:
        return self._final_message


def _text_response(text: str = "hello") -> SimpleNamespace:
    """Return a minimal Anthropic-style text response."""
    return SimpleNamespace(
        id="msg_test",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=10,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=3,
            output_tokens=4,
        ),
    )


def _refusal_response() -> SimpleNamespace:
    """Return a documented Anthropic refusal with usage and details."""
    return SimpleNamespace(
        id="msg_refusal",
        stop_reason="refusal",
        stop_details=SimpleNamespace(
            type="refusal",
            category="cyber",
            explanation="The request triggered a safety policy.",
        ),
        content=[],
        usage=SimpleNamespace(input_tokens=11, output_tokens=3),
    )


def _tool_response() -> SimpleNamespace:
    """Return a minimal Anthropic-style tool-use response."""
    return SimpleNamespace(
        id="msg_tool",
        content=[
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="tool__lookup",
                input={"query": "drow"},
            )
        ],
        usage=SimpleNamespace(input_tokens=8, output_tokens=2),
    )


def _multi_tool_response() -> SimpleNamespace:
    """Return multiple Anthropic tool-use blocks in provider order."""
    return SimpleNamespace(
        id="msg_tools",
        content=[
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="tool__first",
                input={"value": 1},
            ),
            SimpleNamespace(
                type="tool_use",
                id="toolu_2",
                name="tool__second",
                input={"value": 2},
            ),
        ],
        usage=SimpleNamespace(input_tokens=8, output_tokens=2),
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    *,
    model: str = "claude-sonnet-4-6",
    reasoning_effort: str | None = None,
) -> tuple[AnthropicMessagesClient, _FakeMessages]:
    """Create an Anthropic client with a fake SDK messages resource."""
    fake_messages = _FakeMessages(response)
    fake_sdk_client = SimpleNamespace(messages=fake_messages)
    mock_sdk = MagicMock(return_value=fake_sdk_client)
    monkeypatch.setattr("agent.providers.llm.adapters.anthropic.client.anthropic.AsyncAnthropic", mock_sdk)

    client = AnthropicMessagesClient(
        api_key="sk-anthropic",
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return client, fake_messages


@pytest.mark.asyncio
async def test_refusal_outcome_captures_model_response_id_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, messages = _client(monkeypatch, _refusal_response(), model="claude-fable-5")

    with pytest.raises(LLMRefusalError) as exc_info:
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "request"}],
            _retries=2,
        )

    outcome = exc_info.value.outcome
    assert outcome.provider == "anthropic"
    assert outcome.model == "claude-fable-5"
    assert outcome.category == "cyber"
    assert outcome.explanation == "The request triggered a safety policy."
    assert outcome.response_id == "msg_refusal"
    assert outcome.usage is not None
    assert len(messages.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ("chat_messages_with_usage", "chat_with_tools_with_usage"),
)
async def test_non_stream_refusal_preserves_partial_content_across_call_paths(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Anthropic chat and tool refusal paths retain visible text blocks."""
    response = _refusal_response()
    response.content = [SimpleNamespace(type="text", text="Partial answer")]
    client, _messages = _client(monkeypatch, response, model="claude-fable-5")

    method = getattr(client, method_name)
    with pytest.raises(LLMRefusalError) as exc_info:
        if "tools" in method_name:
            await method(
                "System",
                "User",
                tools=[
                    FunctionToolSpec(
                        tool_id="lookup",
                        name="tool__lookup",
                        description="Look up data",
                        parameters_schema={"type": "object", "properties": {}},
                    )
                ],
            )
        else:
            await method([{"role": "user", "content": "request"}])

    assert exc_info.value.outcome.partial_content == "Partial answer"


@pytest.mark.asyncio
async def test_streaming_refusal_preserves_partial_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, messages = _client(monkeypatch, _text_response(), model="claude-fable-5")
    messages.stream_response = _FakeStream(["Partial ", "answer"], _refusal_response())

    chunks: list[str] = []
    with pytest.raises(LLMRefusalError) as exc_info:
        async for chunk in client.stream_chat_messages(
            [{"role": "user", "content": "request"}]
        ):
            chunks.append(chunk)

    assert chunks == ["Partial ", "answer"]
    assert exc_info.value.outcome.partial_content == "Partial answer"
    assert exc_info.value.outcome.usage is not None


@pytest.mark.asyncio
async def test_chat_messages_with_usage_maps_request_response_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _text_response("hi"))

    response = await client.chat_messages_with_usage(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
    )

    assert response.content == "hi"
    assert response.usage is not None
    assert response.usage.provider == "anthropic"
    assert response.usage.api_surface == "messages"
    assert response.usage.prompt_tokens == 15
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 19
    assert response.usage.cached_tokens == 0
    assert fake_messages.calls[0]["system"] == "system"
    assert fake_messages.calls[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert fake_messages.calls[0]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_anthropic_effort_is_model_scoped_and_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(
        monkeypatch,
        _text_response("hi"),
        model="claude-sonnet-5",
        reasoning_effort="xhigh",
    )

    await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        reasoning_effort="max",
    )

    assert client._reasoning_effort == "xhigh"
    assert fake_messages.calls[0]["output_config"] == {"effort": "max"}


def test_anthropic_effort_rejects_unsupported_model_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(LLMConfigurationError, match="support xhigh"):
        _client(
            monkeypatch,
            _text_response("unused"),
            model="claude-sonnet-4-6",
            reasoning_effort="xhigh",
        )


@pytest.mark.asyncio
async def test_stream_chat_messages_with_usage_streams_text_and_final_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_message = _text_response("hello world")
    client, fake_messages = _client(monkeypatch, final_message)
    fake_messages.stream_response = _FakeStream(["hello", " ", "world"], final_message)

    response = await client.stream_chat_messages_with_usage(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.2,
        max_tokens=128,
    )

    chunks: list[str] = []
    async for chunk in response.content_iterator:
        chunks.append(chunk)

    usage = response.get_final_usage()

    assert chunks == ["hello", " ", "world"]
    assert usage is not None
    assert usage.provider == "anthropic"
    assert usage.api_surface == "messages"
    assert usage.prompt_tokens == 15
    assert usage.completion_tokens == 4
    assert usage.provider_usage_components is not None
    assert usage.provider_usage_components.components == {
        "input_tokens": 10,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "output_tokens": 4,
    }
    assert fake_messages.stream_calls[0]["system"] == "system"
    assert fake_messages.stream_calls[0]["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_stream_chat_messages_with_usage_wraps_sdk_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _text_response("unused"))

    class _BrokenStream:
        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *_args: Any) -> None:
            return None

    fake_messages.stream_response = _BrokenStream()

    response = await client.stream_chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
    )

    with pytest.raises(Exception, match="Anthropic usage-aware streaming"):
        async for _chunk in response.content_iterator:
            pass


@pytest.mark.asyncio
async def test_streaming_fable_refusal_raises_after_partial_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_message = SimpleNamespace(
        stop_reason="refusal",
        stop_details={
            "type": "refusal",
            "category": "cyber",
            "explanation": "Request declined.",
        },
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
    )
    client, fake_messages = _client(
        monkeypatch,
        final_message,
        model="claude-fable-5",
    )
    fake_messages.stream_response = _FakeStream(["partial"], final_message)
    response = await client.stream_chat_messages_with_usage(
        [{"role": "user", "content": "hello"}]
    )

    chunks: list[str] = []
    with pytest.raises(LLMRefusalError, match="safety classifier") as exc_info:
        async for chunk in response.content_iterator:
            chunks.append(chunk)

    assert chunks == ["partial"]
    assert response.get_final_usage() == exc_info.value.outcome.usage


@pytest.mark.asyncio
async def test_structured_output_uses_prompt_instruction_and_shared_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _text_response('{"answer":"ok"}'))
    spec = StructuredOutputSpec(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    response = await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        structured_output=spec,
    )

    assert response.structured_output == {"answer": "ok"}
    assert fake_messages.calls[0]["output_config"] == {"effort": "high"}
    assert "Structured output requirement" in fake_messages.calls[0]["messages"][0]["content"]
    assert '"answer":{"type":"string"}' in fake_messages.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_structured_output_accepts_fenced_json_from_prompt_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _fake_messages = _client(monkeypatch, _text_response('```json\n{"answer":"ok"}\n```'))
    spec = StructuredOutputSpec(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    response = await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        structured_output=spec,
    )

    assert response.structured_output == {"answer": "ok"}


@pytest.mark.asyncio
async def test_structured_output_prompt_instruction_preserves_local_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _text_response('{"unexpected":"ok"}'))
    spec = StructuredOutputSpec(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(LLMStructuredOutputParseError):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            structured_output=spec,
        )

    assert fake_messages.calls[0]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_chat_messages_with_usage_ignores_adaptive_thinking_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        id="msg_unsupported",
        content=[
            SimpleNamespace(type="thinking", thinking="hidden reasoning"),
            SimpleNamespace(type="text", text="visible"),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    client, _fake_messages = _client(monkeypatch, response)

    result = await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}]
    )

    assert result.content == "visible"


@pytest.mark.asyncio
async def test_fable_refusal_is_a_typed_successful_response_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        id="msg_refusal",
        stop_reason="refusal",
        stop_details=SimpleNamespace(
            type="refusal",
            category="cyber",
            explanation="Request declined.",
        ),
        content=[],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
    )
    client, _fake_messages = _client(
        monkeypatch,
        response,
        model="claude-fable-5",
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}]
        )

    assert exc_info.value.category == "cyber"
    assert exc_info.value.explanation == "Request declined."
    assert exc_info.value.stop_details["type"] == "refusal"


@pytest.mark.asyncio
async def test_fable_uses_adaptive_thinking_and_omits_sampling_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(
        monkeypatch,
        _text_response("hi"),
        model="claude-fable-5",
    )

    await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        thinking={"type": "adaptive", "display": "summarized"},
        temperature=0.2,
    )

    request = fake_messages.calls[0]
    assert request["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request["output_config"] == {"effort": "high"}
    assert "temperature" not in request


def test_fable_rejects_disabled_adaptive_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _fake_messages = _client(
        monkeypatch,
        _text_response("unused"),
        model="claude-fable-5",
    )

    with pytest.raises(LLMConfigurationError, match="requires adaptive thinking"):
        client._build_request_kwargs(
            [{"role": "user", "content": "hello"}],
            {"thinking": {"type": "disabled"}},
        )


@pytest.mark.asyncio
async def test_chat_messages_with_usage_rejects_non_text_request_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _text_response("unused"))

    with pytest.raises(
        LLMConfigurationError,
        match="only accepts text message content",
    ):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": {"type": "image", "source": "x"}}]
        )

    assert fake_messages.calls == []


@pytest.mark.asyncio
async def test_chat_messages_with_usage_preserves_history_tool_call_capability_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _text_response("unused"))

    with pytest.raises(
        LLMCapabilityNotSupportedError,
        match="historical assistant tool_calls",
    ):
        await client.chat_messages_with_usage(
            [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1"}],
                },
            ]
        )

    assert fake_messages.calls == []


@pytest.mark.asyncio
async def test_chat_with_tools_normalizes_tool_request_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _tool_response())
    tool = FunctionToolSpec(
        tool_id="lookup",
        name="tool__lookup",
        description="Look up data",
        parameters_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    response = await client.chat_with_tools_with_usage(
        "system",
        "lookup drow",
        [tool],
        ToolChoice("specific", function_name="tool__lookup"),
    )

    assert response.tool_calls is not None
    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].name == "tool__lookup"
    assert response.tool_calls[0].arguments == '{"query":"drow"}'
    assert fake_messages.calls[0]["tools"] == [
        {
            "name": "tool__lookup",
            "description": "Look up data",
            "input_schema": tool.parameters_schema,
        }
    ]
    assert fake_messages.calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "tool__lookup",
    }


@pytest.mark.asyncio
async def test_chat_with_tools_preserves_multiple_tool_call_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _fake_messages = _client(monkeypatch, _multi_tool_response())
    tools = [
        FunctionToolSpec(
            tool_id="first",
            name="tool__first",
            description="First",
            parameters_schema={"type": "object", "properties": {}},
        ),
        FunctionToolSpec(
            tool_id="second",
            name="tool__second",
            description="Second",
            parameters_schema={"type": "object", "properties": {}},
        ),
    ]

    response = await client.chat_with_tools_with_usage(
        "system",
        "call both",
        tools,
        ToolChoice("required"),
    )

    assert response.tool_calls is not None
    assert [call.id for call in response.tool_calls] == ["toolu_1", "toolu_2"]
    assert [call.name for call in response.tool_calls] == ["tool__first", "tool__second"]
    assert [call.arguments for call in response.tool_calls] == [
        '{"value":1}',
        '{"value":2}',
    ]


@pytest.mark.asyncio
async def test_chat_with_tools_rejects_missing_tool_capability_before_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _tool_response())

    class _NoToolsProfile:
        tool_choice_modes = frozenset({"auto"})

        def require_capability(self, capability):
            raise LLMCapabilityNotSupportedError(
                "no tools",
                provider="anthropic",
                capability=str(capability),
            )

    monkeypatch.setattr(
        "agent.providers.llm.adapters.anthropic.client.require_model_profile",
        lambda _ref: _NoToolsProfile(),
    )

    with pytest.raises(LLMCapabilityNotSupportedError):
        await client.chat_with_tools_with_usage(
            "system",
            "lookup",
            [
                FunctionToolSpec(
                    tool_id="lookup",
                    name="tool__lookup",
                    description="Look up",
                    parameters_schema={"type": "object", "properties": {}},
                )
            ],
            ToolChoice("auto"),
        )

    assert fake_messages.calls == []


@pytest.mark.asyncio
async def test_chat_with_tools_rejects_forced_choice_without_tools_before_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _tool_response())

    with pytest.raises(LLMCapabilityNotSupportedError, match="requires at least one tool"):
        await client.chat_with_tools_with_usage(
            "system",
            "lookup",
            [],
            ToolChoice("required"),
        )

    assert fake_messages.calls == []


@pytest.mark.asyncio
async def test_chat_with_tools_rejects_unsupported_tool_choice_before_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake_messages = _client(monkeypatch, _tool_response())

    class _AutoOnlyProfile:
        tool_choice_modes = frozenset({"auto"})

        def require_capability(self, _capability):
            return None

    monkeypatch.setattr(
        "agent.providers.llm.adapters.anthropic.client.require_model_profile",
        lambda _ref: _AutoOnlyProfile(),
    )

    with pytest.raises(LLMCapabilityNotSupportedError):
        await client.chat_with_tools_with_usage(
            "system",
            "lookup",
            [
                FunctionToolSpec(
                    tool_id="lookup",
                    name="tool__lookup",
                    description="Look up",
                    parameters_schema={"type": "object", "properties": {}},
                )
            ],
            ToolChoice("required"),
        )

    assert fake_messages.calls == []
