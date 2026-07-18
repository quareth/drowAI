"""Conformance tests locking reusable OpenAI Chat Completions behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec, ToolChoice
from agent.providers.llm.core.base import StructuredOutputSpec


def _response(content: str | None = "ok", *, tool_calls: list[object] | None = None) -> object:
    """Build a minimal Chat Completions response."""

    message = SimpleNamespace(content=content, refusal=None, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(id="chatcmpl-conformance", choices=[choice], usage=None)


@pytest.fixture
def client_and_sdk() -> tuple[OpenAIChatClient, MagicMock]:
    """Create the native OpenAI adapter with a mocked SDK transport."""

    sdk_client = MagicMock()
    sdk_client.close = AsyncMock()
    sdk_client.chat.completions.create = AsyncMock(return_value=_response())
    with patch(
        "agent.providers.llm.adapters.openai.chat.openai.AsyncOpenAI",
        MagicMock(return_value=sdk_client),
    ):
        client = OpenAIChatClient(api_key="test-key", model="gpt-4")
    return client, sdk_client


@pytest.mark.asyncio
async def test_native_plain_request_shape_is_stable(client_and_sdk) -> None:
    """Native chat keeps the current exact Chat Completions payload."""

    client, sdk = client_and_sdk
    messages = [{"role": "user", "content": "hello"}]

    await client.chat_messages(messages, temperature=0.7, max_tokens=321)

    assert sdk.chat.completions.create.await_args.kwargs == {
        "model": "gpt-4",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 321,
    }


@pytest.mark.asyncio
async def test_native_tools_and_structured_output_shape_is_stable(client_and_sdk) -> None:
    """Native tool and strict-schema translation remains unchanged."""

    client, sdk = client_and_sdk
    sdk.chat.completions.create.return_value = _response('{"answer":"ok"}')
    tool = FunctionToolSpec(
        tool_id="lookup",
        name="tool__lookup",
        description="Look up a value",
        parameters_schema={"type": "object", "properties": {}},
    )
    spec = StructuredOutputSpec(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    await client.chat_messages(
        [{"role": "user", "content": "answer"}],
        tools=[tool],
        tool_choice=ToolChoice("specific", function_name="tool__lookup"),
        structured_output=spec,
    )

    request = sdk.chat.completions.create.await_args.kwargs
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "tool__lookup",
                "description": "Look up a value",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert request["tool_choice"] == {
        "type": "function",
        "function": {"name": "tool__lookup"},
    }
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": spec.schema,
        },
    }


@pytest.mark.asyncio
async def test_native_tool_call_parsing_is_stable(client_and_sdk) -> None:
    """Native SDK tool calls retain provider-neutral IDs, names, and arguments."""

    client, sdk = client_and_sdk
    sdk_tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="tool__lookup", arguments='{"q":"x"}'),
    )
    sdk.chat.completions.create.return_value = _response(None, tool_calls=[sdk_tool_call])

    result = await client.chat_with_tools("system", "user", tools=[])

    assert result.tool_calls is not None
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call-1", "tool__lookup", '{"q":"x"}')
    ]


@pytest.mark.asyncio
async def test_native_stream_request_and_usage_capture_are_stable(client_and_sdk) -> None:
    """Native streaming still requests final usage and exposes it after consumption."""

    client, sdk = client_and_sdk
    content_chunk = SimpleNamespace(
        id="chatcmpl-stream",
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="hello", refusal=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    usage_marker = object()
    usage_chunk = SimpleNamespace(id=None, choices=[], usage=SimpleNamespace(total_tokens=3))

    async def stream():
        yield content_chunk
        yield usage_chunk

    sdk.chat.completions.create.return_value = stream()
    client._extract_usage_from_response = MagicMock(return_value=usage_marker)

    response = await client.stream_chat_messages_with_usage(
        [{"role": "user", "content": "hello"}]
    )
    chunks = [chunk async for chunk in response.content_iterator]

    assert chunks == ["hello"]
    assert response.get_final_usage() is usage_marker
    assert sdk.chat.completions.create.await_args.kwargs["stream_options"] == {
        "include_usage": True
    }
