"""Tests for fail-closed and agent-capable OpenAI-compatible Chat dialects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.openai.compatible_chat import (
    AGENT_OPENAI_COMPATIBLE_DIALECT,
    CompatibleChatAuth,
    OpenAICompatibleChatClient,
)
from agent.providers.llm.core.base import StructuredOutputSpec
from agent.providers.llm.contracts.compat import LLMDialectPolicy
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.factory.client_factory import LLMClientFactory


def _response(content: str = "ok", usage: object | None = None) -> object:
    """Build a minimal successful Chat Completions response."""

    message = SimpleNamespace(content=content, refusal=None, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(id="compatible-response", choices=[choice], usage=usage)


def _client(
    *,
    endpoint: str = "https://inference.example/v1",
    wire_model_id: str = "Vendor/Model.Name-20B",
    auth: CompatibleChatAuth | None = None,
    guarded_executor: MagicMock | None = None,
    dialect_policy: LLMDialectPolicy | None = None,
) -> tuple[OpenAICompatibleChatClient, MagicMock, MagicMock]:
    """Create the compatible adapter with a mocked SDK constructor."""

    sdk_client = MagicMock()
    sdk_client.close = AsyncMock()
    sdk_client.chat.completions.create = AsyncMock(return_value=_response())
    constructor = MagicMock(return_value=sdk_client)
    with patch(
        "agent.providers.llm.adapters.openai.compatible_chat.openai.AsyncOpenAI",
        constructor,
    ):
        client_kwargs = {
            "base_url": endpoint,
            "auth": auth or CompatibleChatAuth.bearer("test-key"),
            "wire_model_id": wire_model_id,
            "guarded_executor": guarded_executor,
        }
        if dialect_policy is not None:
            client_kwargs["dialect_policy"] = dialect_policy
        client = OpenAICompatibleChatClient(
            **client_kwargs,
        )
    return client, sdk_client, constructor


@pytest.mark.asyncio
async def test_compatible_request_preserves_exact_wire_model_id() -> None:
    """Wire model IDs are forwarded without lowercasing or punctuation changes."""

    client, sdk, constructor = _client()
    messages = [{"role": "user", "content": "hello"}]

    await client.chat_messages(messages, temperature=0.4, max_tokens=64)

    assert client.model == "Vendor/Model.Name-20B"
    assert sdk.chat.completions.create.await_args.kwargs == {
        "model": "Vendor/Model.Name-20B",
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 64,
    }
    assert constructor.call_args.kwargs["base_url"] == "https://inference.example/v1"


@pytest.mark.asyncio
async def test_compatible_adapter_rejects_unknown_request_parameters() -> None:
    """Unapproved SDK parameters fail before any outbound request."""

    client, sdk, _constructor = _client()

    with pytest.raises(LLMConfigurationError, match="frequency_penalty"):
        await client.chat_messages(
            [{"role": "user", "content": "hello"}],
            frequency_penalty=0.5,
        )

    sdk.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatible_adapter_preserves_provider_reported_usage() -> None:
    """Non-streaming usage proof uses provider-reported token counts only."""

    client, sdk, _constructor = _client()
    sdk.chat.completions.create.return_value = _response(
        usage=SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=7,
        )
    )

    response = await client.chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        max_tokens=8,
    )

    assert response.content == "ok"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 5
    assert response.usage.completion_tokens == 2
    assert response.usage.total_tokens == 7
    assert sdk.chat.completions.create.await_args.kwargs["model"] == (
        "Vendor/Model.Name-20B"
    )


@pytest.mark.asyncio
async def test_compatible_adapter_rejects_streaming_usage_without_policy() -> None:
    """Streaming usage remains disabled until the endpoint contract admits it."""

    client, sdk, _constructor = _client()

    with pytest.raises(LLMConfigurationError, match="streaming usage"):
        await client.stream_chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=8,
        )

    sdk.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatible_adapter_rejects_tools_without_dialect_policy() -> None:
    """Tool behavior stays disabled until a code-owned dialect policy enables it."""

    client, sdk, _constructor = _client()

    with pytest.raises(LLMConfigurationError, match="dialect policy"):
        await client.chat_with_tools(
            "system",
            "user",
            tools=[{"type": "function", "function": {"name": "tool__lookup"}}],
        )

    sdk.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_dialect_sends_and_parses_native_structured_output() -> None:
    """Agent classification uses the reviewed JSON-schema request contract."""

    client, sdk, _constructor = _client(
        dialect_policy=AGENT_OPENAI_COMPATIBLE_DIALECT
    )
    sdk.chat.completions.create.return_value = _response('{"route":"simple_tool"}')
    spec = StructuredOutputSpec(
        name="intent_route",
        schema={
            "type": "object",
            "properties": {"route": {"type": "string"}},
            "required": ["route"],
            "additionalProperties": False,
        },
    )

    response = await client.chat_with_usage(
        "system",
        "user",
        max_tokens=64,
        reasoning_effort=None,
        structured_output=spec,
    )

    assert response.structured_output == {"route": "simple_tool"}
    assert sdk.chat.completions.create.await_args.kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "intent_route",
            "strict": True,
            "schema": spec.schema,
        },
    }


@pytest.mark.asyncio
async def test_agent_dialect_sends_and_normalizes_usage_tracked_tool_calls() -> None:
    """Function-call parameter resolution stays provider-neutral at the boundary."""

    client, sdk, _constructor = _client(
        dialect_policy=AGENT_OPENAI_COMPATIBLE_DIALECT
    )
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="tool__net_nmap", arguments='{"target":"127.0.0.1"}'),
    )
    sdk.chat.completions.create.return_value = SimpleNamespace(
        id="tool-response",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    refusal=None,
                    tool_calls=[tool_call],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
    )

    result = await client.chat_with_tools_with_usage(
        "system",
        "user",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "tool__net_nmap",
                    "description": "Run nmap",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="required",
        max_tokens=128,
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].name == "tool__net_nmap"
    assert result.tool_calls[0].arguments == '{"target":"127.0.0.1"}'
    assert result.usage is not None
    assert result.usage.total_tokens == 15
    assert sdk.chat.completions.create.await_args.kwargs["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_agent_dialect_normalizes_valid_content_encoded_tool_call() -> None:
    """Compatible models may encode a required call in content instead of tool_calls."""

    client, sdk, _constructor = _client(
        dialect_policy=AGENT_OPENAI_COMPATIBLE_DIALECT
    )
    sdk.chat.completions.create.return_value = _response(
        '{"name":"functions.tool__net_nmap",'
        '"arguments":{"target":"127.0.0.1"}}',
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
    )

    result = await client.chat_with_tools_with_usage(
        "system",
        "user",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "tool__net_nmap",
                    "description": "Run nmap",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice="required",
        max_tokens=128,
    )

    assert result.content is None
    assert result.tool_calls is not None
    assert result.tool_calls[0].name == "tool__net_nmap"
    assert result.tool_calls[0].arguments == '{"target":"127.0.0.1"}'
    assert result.usage is not None
    assert result.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_guarded_compatible_transport_normalizes_content_encoded_tool_call() -> None:
    """The guarded runtime path normalizes the exact wire response seen from proxies."""

    guarded_executor = MagicMock(
        return_value=(
            b'{"id":"tool-response","choices":[{"message":{'
            b'"role":"assistant","content":"{\\"name\\":'
            b'\\"functions.tool__net_nmap\\",\\"arguments\\":{'
            b'\\"target\\":\\"127.0.0.1\\"}}","tool_calls":null},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":11,'
            b'"completion_tokens":4,"total_tokens":15}}'
        )
    )
    client, _sdk, constructor = _client(
        guarded_executor=guarded_executor,
        dialect_policy=AGENT_OPENAI_COMPATIBLE_DIALECT,
    )

    result = await client.chat_with_tools_with_usage(
        "system",
        "user",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "tool__net_nmap",
                    "description": "Run nmap",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice="required",
        max_tokens=128,
    )

    assert constructor.call_count == 0
    assert result.content is None
    assert result.tool_calls is not None
    assert result.tool_calls[0].name == "tool__net_nmap"
    assert result.tool_calls[0].arguments == '{"target":"127.0.0.1"}'
    assert result.usage is not None
    assert result.usage.total_tokens == 15
    assert guarded_executor.call_args.args[0]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_agent_dialect_decodes_guarded_sse_and_final_usage() -> None:
    """Guarded compatible streaming preserves content and provider usage events."""

    guarded_executor = MagicMock(
        return_value=(
            b'data: {"id":"stream-1","choices":[{"delta":{"content":"hello"}}]}\n\n'
            b'data: {"id":"stream-1","choices":[],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":1,"total_tokens":4}}\n\n'
            b'data: [DONE]\n\n'
        )
    )
    client, _sdk, _constructor = _client(
        guarded_executor=guarded_executor,
        dialect_policy=AGENT_OPENAI_COMPATIBLE_DIALECT,
    )

    response = await client.stream_chat_messages_with_usage(
        [{"role": "user", "content": "hello"}],
        max_tokens=8,
        reasoning_effort=None,
    )
    chunks = [chunk async for chunk in response.content_iterator]

    assert chunks == ["hello"]
    assert response.get_final_usage() is not None
    assert response.get_final_usage().total_tokens == 4
    request = guarded_executor.call_args.args[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://inference.example/v1",
        "https://user:password@inference.example/v1",
        "https://inference.example/v1?token=secret",
        "https://inference.example/v1#fragment",
        "https://inference.example/v 1",
    ],
)
def test_compatible_adapter_rejects_unsafe_endpoint_forms(endpoint: str) -> None:
    """Endpoint syntax is validated before constructing an SDK transport."""

    with pytest.raises(LLMConfigurationError, match="base URL"):
        _client(endpoint=endpoint)


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://127.0.0.1:4000/v1",
        "http://localhost:4000/v1",
        "http://[::1]:4000/v1",
    ),
)
def test_compatible_adapter_accepts_explicit_loopback_endpoint(endpoint: str) -> None:
    """Guarded compatible construction accepts its authorized local endpoint."""

    _client_instance, _sdk, constructor = _client(
        endpoint=endpoint,
        guarded_executor=MagicMock(),
    )

    constructor.assert_not_called()


def test_compatible_adapter_supports_typed_no_auth() -> None:
    """No-auth endpoints do not require or invent a bearer credential."""

    _client_instance, _sdk, constructor = _client(auth=CompatibleChatAuth.none())

    assert constructor.call_args.kwargs["api_key"] is None
    assert constructor.call_args.kwargs["_enforce_credentials"] is False


def test_compatible_adapter_is_not_product_registered() -> None:
    """Phase 1 does not expose the compatible adapter through the factory."""

    registrations = LLMClientFactory.list_providers()
    prefix_registrations = LLMClientFactory.list_prefix_registrations()

    assert all("OpenAICompatibleChatClient" not in value for value in registrations.values())
    assert all(
        value != "OpenAICompatibleChatClient"
        for value in prefix_registrations.values()
    )
