"""Reusable fake LLM provider adapter and profiles for contract tests.

The fake provider intentionally records provider-neutral inputs without
translating them to OpenAI request payloads. Tests use it to prove the core
contracts can host another adapter without adding provider branches.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from agent.providers.llm.core.base import LLMClient, LLMResponse, ToolCall, ToolCallResult
from agent.providers.llm.core.capabilities import LLMCapability, freeze_capabilities
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ModelProfile,
    ModelProfileRegistry,
    ProviderProfile,
)
from agent.providers.llm.contracts.tool_contracts import FunctionToolSpec, ToolChoice

FAKE_PROVIDER_ID = "fake"
FAKE_MODEL_ID = "fake-chat"


class FakeProviderClient(LLMClient):
    """Minimal provider adapter that records neutral LLM contract inputs."""

    instances: list["FakeProviderClient"] = []

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self._model = model
        self.init_kwargs = dict(kwargs)
        self.calls: list[dict[str, Any]] = []
        self.__class__.instances.append(self)

    @classmethod
    def reset_instances(cls) -> None:
        """Clear constructed fake clients between tests."""
        cls.instances.clear()

    @property
    def model(self) -> str:
        """Return the provider request model supplied by the factory."""
        return self._model

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {
                "method": "chat",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": dict(kwargs),
            }
        )
        return "fake chat response"

    async def chat_messages(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {
                "method": "chat_messages",
                "messages": list(messages),
                "kwargs": dict(kwargs),
            }
        )
        return "fake messages response"

    async def stream_chat_messages(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "method": "stream_chat_messages",
                "messages": list(messages),
                "kwargs": dict(kwargs),
            }
        )
        yield "fake "
        yield "stream"

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "method": "chat_with_usage",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": dict(kwargs),
            }
        )
        structured_output = None
        if kwargs.get("structured_output") is not None:
            structured_output = {"answer": "fake"}
        return LLMResponse(
            content='{"answer":"fake"}' if structured_output else "fake usage response",
            structured_output=structured_output,
        )

    async def chat_messages_with_usage(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "method": "chat_messages_with_usage",
                "messages": list(messages),
                "kwargs": dict(kwargs),
            }
        )
        structured_output = None
        if kwargs.get("structured_output") is not None:
            structured_output = {"answer": "fake"}
        return LLMResponse(
            content='{"answer":"fake"}' if structured_output else "fake messages usage response",
            structured_output=structured_output,
        )

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        self.calls.append(
            {
                "method": "chat_with_tools",
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": list(tools),
                "tool_choice": tool_choice,
                "kwargs": dict(kwargs),
            }
        )
        function_name = _first_function_name(tools)
        tool_calls = (
            [ToolCall(id="fake-call-1", name=function_name, arguments='{"ok":true}')]
            if function_name
            else None
        )
        return ToolCallResult(
            content=None if tool_calls else "fake tool-free response",
            tool_calls=tool_calls,
            raw={"provider": FAKE_PROVIDER_ID, "tool_count": len(tools)},
        )

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> ToolCallResult:
        result = await self.chat_with_tools(
            system_prompt,
            user_prompt,
            tools,
            tool_choice,
            **kwargs,
        )
        result.usage = None
        return result


def _first_function_name(tools: list[Any]) -> str | None:
    """Return the first neutral function name from a tool list."""
    for tool in tools:
        if isinstance(tool, FunctionToolSpec):
            return tool.name
    return None


def fake_provider_profile() -> ProviderProfile:
    """Build provider-wide fake provider metadata."""
    return ProviderProfile(
        id=FAKE_PROVIDER_ID,
        display_name="Fake Provider",
        capabilities=freeze_capabilities((LLMCapability.CHAT,)),
    )


def fake_model_profile() -> ModelProfile:
    """Build model-level fake provider metadata."""
    return ModelProfile(
        ref=ProviderModelRef(FAKE_PROVIDER_ID, FAKE_MODEL_ID),
        display_name="Fake Chat",
        api_surface="fake_chat",
        capabilities=freeze_capabilities(
            (
                LLMCapability.CHAT,
                LLMCapability.STREAMING,
                LLMCapability.TOOLS,
                LLMCapability.STRUCTURED_OUTPUT_NATIVE,
                LLMCapability.USAGE_REPORTING,
                LLMCapability.CONTEXT_WINDOW,
                LLMCapability.MAX_OUTPUT_TOKENS,
            )
        ),
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        listable=True,
        tool_choice_modes=frozenset(("auto", "none", "required", "specific")),
        structured_output_strategies=frozenset(("native_schema",)),
    )


def build_fake_profile_registry() -> ModelProfileRegistry:
    """Build an isolated profile registry containing only the fake provider."""
    return ModelProfileRegistry(
        providers=(fake_provider_profile(),),
        models=(fake_model_profile(),),
        compatibility_rules=(),
    )


def fake_tool_spec() -> FunctionToolSpec:
    """Return a neutral fake function tool spec."""
    return FunctionToolSpec(
        tool_id="fake.lookup",
        name="tool__fake_lookup",
        description="Look up fake data",
        parameters_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def fake_specific_tool_choice() -> ToolChoice:
    """Return a neutral choice targeting the fake tool."""
    return ToolChoice("specific", function_name="tool__fake_lookup")
