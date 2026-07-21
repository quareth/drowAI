"""Shared runtime-client test doubles for builder and facade boundary tests."""

from __future__ import annotations

from typing import Any, AsyncIterator

from agent.providers.llm.core.base import ChatMessage, LLMClient, LLMResponse, ToolCallResult
from backend.services.llm_provider.types import (
    LLMConnectionOperation,
    RegisteredLLMOperationTarget,
)


class MinimalRuntimeClient(LLMClient):
    """Concrete fake client returned by runtime client factory boundaries."""

    model = "factory-model"

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        return "chat"

    async def chat_messages(self, messages: list[ChatMessage], **kwargs: Any) -> str:
        return "chat_messages"

    async def stream_chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        yield "stream"

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content="usage")

    async def chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content="messages_usage")

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(content="tools", tool_calls=None, raw=None)

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        return ToolCallResult(content="tools_usage", tool_calls=None, raw=None)


def operation_target(provider: str = "openai") -> RegisteredLLMOperationTarget:
    """Build one registered inference target for runtime-client tests."""

    return RegisteredLLMOperationTarget(
        operation=LLMConnectionOperation.INFERENCE,
        provider=provider,
        method="POST",
        url=f"https://{provider}.example.test/v1/chat/completions",
        client_base_url=f"https://{provider}.example.test/v1",
        expected_host=f"{provider}.example.test",
        allowed_ports=frozenset({443}),
        allowed_path_prefixes=("/v1",),
    )


__all__ = ["MinimalRuntimeClient", "operation_target"]
