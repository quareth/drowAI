"""Tests for provider-neutral and SDK-backed LLM client cleanup contracts."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.providers.llm.adapters.anthropic.client import AnthropicMessagesClient
from agent.providers.llm.adapters.openai.chat import OpenAIChatClient
from agent.providers.llm.adapters.openai.responses.client import OpenAIResponsesClient
from agent.tests.providers.llm.fakes import FakeProviderClient


@pytest.mark.asyncio
async def test_client_inherits_safe_noop_cleanup_and_async_context_manager() -> None:
    """Clients without owned transports remain valid and need no caller migration."""

    client = FakeProviderClient(api_key="test-key", model="fake-chat")

    async with client as entered:
        assert entered is client

    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constructor_path", "client_type", "model"),
    [
        (
            "agent.providers.llm.adapters.openai.chat.openai.AsyncOpenAI",
            OpenAIChatClient,
            "gpt-4o-mini",
        ),
        (
            "agent.providers.llm.adapters.openai.responses.client.openai.AsyncOpenAI",
            OpenAIResponsesClient,
            "gpt-5",
        ),
        (
            "agent.providers.llm.adapters.anthropic.client.anthropic.AsyncAnthropic",
            AnthropicMessagesClient,
            "claude-sonnet-4-6",
        ),
    ],
)
async def test_sdk_clients_close_owned_transport_once(
    constructor_path: str,
    client_type: type[Any],
    model: str,
) -> None:
    """SDK-backed clients deterministically close their transport only once."""

    sdk_client = SimpleNamespace(close=AsyncMock())
    with patch(constructor_path, MagicMock(return_value=sdk_client)):
        client = client_type(api_key="test-key", model=model)

    await asyncio.gather(client.aclose(), client.aclose())
    await client.aclose()

    sdk_client.close.assert_awaited_once_with()


class _BlockingOpenAIStream:
    """OpenAI-style stream that blocks after yielding one content chunk."""

    def __init__(self) -> None:
        self.blocked = asyncio.Event()
        self._yielded = False

    def __aiter__(self) -> "_BlockingOpenAIStream":
        return self

    async def __anext__(self) -> Any:
        if not self._yielded:
            self._yielded = True
            choice = SimpleNamespace(
                delta=SimpleNamespace(content="partial", refusal=None),
                finish_reason=None,
            )
            return SimpleNamespace(id="chunk-1", choices=[choice], usage=None)

        self.blocked.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_streaming_cancellation_closes_client_without_double_close() -> None:
    """Cancelling a context-managed stream releases the SDK transport once."""

    stream = _BlockingOpenAIStream()
    sdk_client = MagicMock()
    sdk_client.close = AsyncMock()
    sdk_client.chat.completions.create = AsyncMock(return_value=stream)

    with patch(
        "agent.providers.llm.adapters.openai.chat.openai.AsyncOpenAI",
        MagicMock(return_value=sdk_client),
    ):
        client = OpenAIChatClient(api_key="test-key", model="gpt-4o-mini")

    async def consume() -> None:
        async with client:
            async for _chunk in client.stream_chat_messages(
                [{"role": "user", "content": "hello"}]
            ):
                pass

    task = asyncio.create_task(consume())
    await stream.blocked.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    await client.aclose()
    sdk_client.close.assert_awaited_once_with()
