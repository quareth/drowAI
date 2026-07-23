"""Characterize provider-neutral budget wrapper behavior.

Scope: lock `BudgetEnforcingLLMClient` forwarding, budget decisions, capability
exposure, tool-payload accounting, and current lifecycle behavior at its
canonical agent LLM core boundary.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

from agent.providers.llm.core import budget_enforcing_client as budget_module
from agent.providers.llm.core.base import (
    ChatMessage,
    LLMClient,
    LLMResponse,
    ToolCallResult,
)
from agent.providers.llm.core.budget_enforcing_client import BudgetEnforcingLLMClient
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile


class _RecordingClient(LLMClient):
    """Fake wrapped client that records every delegated method call."""

    model = "wrapped-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.extra_capability = "delegated"
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1

    def _record(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.calls.append({"method": method, "args": args, "kwargs": dict(kwargs)})

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        self._record("chat", (system_prompt, user_prompt), kwargs)
        return "chat-result"

    async def chat_messages(self, messages: list[ChatMessage], **kwargs: Any) -> str:
        self._record("chat_messages", (messages,), kwargs)
        return "chat-messages-result"

    async def stream_chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self._record("stream_chat_messages", (messages,), kwargs)
        yield "chunk-1"
        yield "chunk-2"

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        self._record("chat_with_usage", (system_prompt, user_prompt), kwargs)
        return LLMResponse(content="usage-result")

    async def chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        self._record("chat_messages_with_usage", (messages,), kwargs)
        return LLMResponse(content="messages-usage-result")

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        self._record("chat_with_tools", (system_prompt, user_prompt, tools, tool_choice), kwargs)
        return ToolCallResult(content="tools-result", tool_calls=None, raw=None)

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        self._record(
            "chat_with_tools_with_usage",
            (system_prompt, user_prompt, tools, tool_choice),
            kwargs,
        )
        return ToolCallResult(content="tools-usage-result", tool_calls=None, raw=None)


class _RecordingClientWithStreamingUsage(_RecordingClient):
    """Fake wrapped client that exposes the optional streaming-usage method."""

    async def stream_chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        self._record("stream_chat_messages_with_usage", (messages,), kwargs)
        return LLMResponse(content="stream-usage-result")


def _wrapper(
    wrapped: LLMClient | None = None,
    *,
    provider: str = "openai",
    model: str = "gpt-5.2",
    role: str = "conversation_main",
) -> BudgetEnforcingLLMClient:
    return BudgetEnforcingLLMClient(
        wrapped or _RecordingClient(),
        provider_model=ProviderModelRef(provider, model),
        role=role,
        model_profile=require_model_profile(ProviderModelRef(provider, model)),
    )


def _stub_token_estimators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history_tokens: int = 1,
    json_tokens: int = 0,
) -> list[Any]:
    payloads: list[Any] = []
    monkeypatch.setattr(
        budget_module,
        "estimate_chat_history_tokens",
        lambda **_kwargs: SimpleNamespace(tokens=history_tokens),
    )

    def fake_estimate_json_tokens(payload: Any, **_kwargs: Any) -> SimpleNamespace:
        payloads.append(payload)
        return SimpleNamespace(tokens=json_tokens)

    monkeypatch.setattr(budget_module, "estimate_json_tokens", fake_estimate_json_tokens)
    return payloads


def test_model_attribute_and_optional_streaming_usage_capability_are_delegated() -> None:
    """Wrapper exposes the wrapped model, unknown attributes, and optional usage stream."""

    wrapped = _RecordingClient()
    client = _wrapper(wrapped)

    assert client.model == "wrapped-model"
    assert client.extra_capability == "delegated"
    assert hasattr(client, "stream_chat_messages_with_usage") is False

    wrapped_with_usage = _RecordingClientWithStreamingUsage()
    client_with_usage = _wrapper(wrapped_with_usage)

    assert hasattr(client_with_usage, "stream_chat_messages_with_usage") is True


@pytest.mark.asyncio
async def test_all_overridden_methods_forward_with_enforced_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every wrapper override delegates to the wrapped client with budgeted kwargs."""

    _stub_token_estimators(monkeypatch)
    wrapped = _RecordingClientWithStreamingUsage()
    client = _wrapper(wrapped)
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"name": "lookup", "parameters": {"type": "object"}}]

    assert await client.chat("system", "user", max_tokens=123) == "chat-result"
    assert await client.chat_messages(messages, max_tokens=123) == "chat-messages-result"
    assert [
        chunk async for chunk in client.stream_chat_messages(messages, max_tokens=123)
    ] == ["chunk-1", "chunk-2"]
    assert (await client.chat_with_usage("system", "user", max_tokens=123)).content == "usage-result"
    assert (
        await client.chat_messages_with_usage(messages, max_tokens=123)
    ).content == "messages-usage-result"
    assert (
        await client.stream_chat_messages_with_usage(messages, max_tokens=123)
    ).content == "stream-usage-result"
    assert (
        await client.chat_with_tools("system", "user", tools, tool_choice="required", max_tokens=123)
    ).content == "tools-result"
    assert (
        await client.chat_with_tools_with_usage(
            "system",
            "user",
            tools,
            tool_choice={"type": "function", "name": "lookup"},
            max_tokens=123,
        )
    ).content == "tools-usage-result"

    assert [call["method"] for call in wrapped.calls] == [
        "chat",
        "chat_messages",
        "stream_chat_messages",
        "chat_with_usage",
        "chat_messages_with_usage",
        "stream_chat_messages_with_usage",
        "chat_with_tools",
        "chat_with_tools_with_usage",
    ]
    assert all(call["kwargs"]["max_tokens"] == 123 for call in wrapped.calls)


@pytest.mark.asyncio
async def test_default_and_clamped_openai_output_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted OpenAI budgets use the legacy default and oversized budgets clamp."""

    _stub_token_estimators(monkeypatch, history_tokens=1)
    wrapped = _RecordingClient()
    client = _wrapper(wrapped)

    await client.chat_with_usage("system", "user")
    await client.chat_with_usage("system", "user", max_tokens=64_000)

    assert wrapped.calls == [
        {
            "method": "chat_with_usage",
            "args": ("system", "user"),
            "kwargs": {"max_tokens": 10_000},
        },
        {
            "method": "chat_with_usage",
            "args": ("system", "user"),
            "kwargs": {"max_tokens": 32_000},
        },
    ]


@pytest.mark.asyncio
async def test_omitted_nonlegacy_output_budget_remains_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model capability ceiling is not an implicit per-call output request."""

    _stub_token_estimators(monkeypatch, history_tokens=3_916)
    wrapped = _RecordingClient()
    provider_model = ProviderModelRef("openai", "gpt-5.2")
    client = BudgetEnforcingLLMClient(
        wrapped,
        provider_model=provider_model,
        role="conversation_main",
        model_profile=replace(
            require_model_profile(provider_model),
            api_surface="compatible_chat",
        ),
    )

    await client.chat_with_usage("system", "user")

    assert wrapped.calls == [
        {
            "method": "chat_with_usage",
            "args": ("system", "user"),
            "kwargs": {},
        }
    ]


@pytest.mark.asyncio
async def test_hard_budget_failure_prevents_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-clamping providers fail before the wrapped client is invoked."""

    _stub_token_estimators(monkeypatch, history_tokens=1)
    wrapped = _RecordingClient()
    client = _wrapper(
        wrapped,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
    )

    with pytest.raises(LLMConfigurationError, match="max_output_tokens=64000"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=999_999,
        )

    assert wrapped.calls == []


@pytest.mark.asyncio
async def test_context_fit_failure_prevents_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context-window overflow is raised before delegating to the wrapped client."""

    _stub_token_estimators(monkeypatch, history_tokens=199_500)
    wrapped = _RecordingClient()
    client = _wrapper(
        wrapped,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
    )

    with pytest.raises(LLMConfigurationError, match="context_window_tokens=200000"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=1_000,
        )

    assert wrapped.calls == []


@pytest.mark.asyncio
async def test_estimator_failure_prevents_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Estimator exceptions become configuration errors before provider I/O."""

    def fail_estimate(**_kwargs: Any) -> SimpleNamespace:
        raise ValueError("token estimator failed")

    monkeypatch.setattr(budget_module, "estimate_chat_history_tokens", fail_estimate)
    wrapped = _RecordingClient()
    client = _wrapper(
        wrapped,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
    )

    with pytest.raises(LLMConfigurationError, match="Unable to estimate context tokens"):
        await client.chat_messages_with_usage(
            [{"role": "user", "content": "hello"}],
            max_tokens=1_000,
        )

    assert wrapped.calls == []


@pytest.mark.asyncio
async def test_tool_payload_accounting_participates_in_context_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool specs and tool_choice are added to the context estimate payload."""

    payloads = _stub_token_estimators(
        monkeypatch,
        history_tokens=198_500,
        json_tokens=700,
    )
    wrapped = _RecordingClient()
    client = _wrapper(
        wrapped,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
    )
    tools = [{"name": "large_tool", "parameters": {"type": "object"}}]
    tool_choice = {"type": "function", "name": "large_tool"}

    with pytest.raises(LLMConfigurationError, match="context_window_tokens=200000"):
        await client.chat_with_tools_with_usage(
            "system",
            "user",
            tools,
            tool_choice=tool_choice,
            max_tokens=1_000,
        )

    assert payloads == [{"tools": tools, "tool_choice": tool_choice}]
    assert wrapped.calls == []


@pytest.mark.asyncio
async def test_current_lifecycle_uses_wrapper_base_class_not_wrapped_client() -> None:
    """Current aclose/context-manager behavior does not delegate to the wrapped client."""

    wrapped = _RecordingClient()
    client = _wrapper(wrapped)

    await client.aclose()
    assert wrapped.close_calls == 0

    async with client as entered:
        assert entered is client

    assert wrapped.close_calls == 0
