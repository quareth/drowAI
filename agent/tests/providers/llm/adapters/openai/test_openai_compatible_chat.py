"""Tests for the unregistered conservative OpenAI-compatible Chat adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.openai.compatible_chat import (
    CompatibleChatAuth,
    OpenAICompatibleChatClient,
)
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
        client = OpenAICompatibleChatClient(
            endpoint=endpoint,
            auth=auth or CompatibleChatAuth.bearer("test-key"),
            wire_model_id=wire_model_id,
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

    with pytest.raises(LLMConfigurationError, match="endpoint"):
        _client(endpoint=endpoint)


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
