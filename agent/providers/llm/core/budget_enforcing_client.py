"""Provider-neutral output-budget enforcement for LLM clients.

Purpose: wrap an existing ``LLMClient`` and validate or adjust output-token
budgets before provider calls. Scope boundary: this module may depend on agent
LLM core, profiles, and context-estimation utilities only; it must not import
backend services, credentials, target resolution, or adapter construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, AsyncIterator

from agent.context.context_window_policy import estimate_chat_history_tokens
from agent.context.token_counter_registry import estimate_json_tokens
from agent.providers.llm.core.base import (
    ChatMessage,
    LLMClient,
    ToolChoiceInput,
    ToolSpecInput,
)
from agent.providers.llm.core.budget_policy import OutputBudgetDecision, decide_output_budget
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import ModelProfile

_LEGACY_RUNTIME_DEFAULT_MAX_TOKENS_BY_SURFACE: dict[tuple[str, str], int] = {
    ("openai", "responses"): 10_000,
    ("openai", "chat_completions"): 10_000,
    ("anthropic", "messages"): 4_096,
}


class BudgetEnforcingLLMClient(LLMClient):
    """LLMClient wrapper that validates max_tokens before provider calls."""

    def __init__(
        self,
        wrapped: LLMClient,
        *,
        provider_model: ProviderModelRef,
        role: str,
        model_profile: ModelProfile,
    ) -> None:
        self._wrapped = wrapped
        self._provider_model = provider_model.normalized()
        self._role = role
        self._model_profile = model_profile

    @property
    def model(self) -> str:
        """Return the provider request model exposed by the wrapped client."""
        return getattr(self._wrapped, "model", self._provider_model.model)

    def __getattribute__(self, name: str) -> Any:
        if name == "stream_chat_messages_with_usage":
            wrapped = object.__getattribute__(self, "_wrapped")
            if not hasattr(wrapped, name):
                raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        messages = _single_turn_messages(system_prompt, user_prompt)
        return await self._wrapped.chat(
            system_prompt,
            user_prompt,
            **self._enforce_output_budget(kwargs, messages=messages),
        )

    async def chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> str:
        return await self._wrapped.chat_messages(
            messages,
            **self._enforce_output_budget(kwargs, messages=messages),
        )

    async def stream_chat_messages(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        async for chunk in self._wrapped.stream_chat_messages(
            messages,
            **self._enforce_output_budget(kwargs, messages=messages),
        ):
            yield chunk

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> Any:
        messages = _single_turn_messages(system_prompt, user_prompt)
        return await self._wrapped.chat_with_usage(
            system_prompt,
            user_prompt,
            **self._enforce_output_budget(kwargs, messages=messages),
        )

    async def chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> Any:
        return await self._wrapped.chat_messages_with_usage(
            messages,
            **self._enforce_output_budget(kwargs, messages=messages),
        )

    async def stream_chat_messages_with_usage(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> Any:
        return await self._wrapped.stream_chat_messages_with_usage(
            messages,
            **self._enforce_output_budget(kwargs, messages=messages),
        )

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> Any:
        messages = _single_turn_messages(system_prompt, user_prompt)
        return await self._wrapped.chat_with_tools(
            system_prompt,
            user_prompt,
            tools,
            tool_choice=tool_choice,
            **self._enforce_output_budget(
                kwargs,
                messages=messages,
                extra_context_payloads=[{"tools": tools, "tool_choice": tool_choice}],
            ),
        )

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpecInput],
        tool_choice: ToolChoiceInput = "auto",
        **kwargs: Any,
    ) -> Any:
        messages = _single_turn_messages(system_prompt, user_prompt)
        return await self._wrapped.chat_with_tools_with_usage(
            system_prompt,
            user_prompt,
            tools,
            tool_choice=tool_choice,
            **self._enforce_output_budget(
                kwargs,
                messages=messages,
                extra_context_payloads=[{"tools": tools, "tool_choice": tool_choice}],
            ),
        )

    def _enforce_output_budget(
        self,
        kwargs: dict[str, Any],
        *,
        messages: list[ChatMessage],
        extra_context_payloads: list[Any] | None = None,
    ) -> dict[str, Any]:
        requested_max_tokens = kwargs.get("max_tokens")
        should_write_budget = "max_tokens" not in kwargs or requested_max_tokens is None
        if should_write_budget:
            requested_max_tokens = self._default_max_tokens()

        decision = decide_output_budget(
            provider=self._provider_model.provider,
            model=self._provider_model.model,
            role=self._role,
            requested_max_output_tokens=requested_max_tokens,
            context_estimate_tokens=self._estimate_context_tokens(
                messages,
                extra_context_payloads=extra_context_payloads,
            ),
            model_profile=self._model_profile,
        )
        if decision.should_fail:
            raise _budget_configuration_error(decision)
        if (should_write_budget or decision.clamped) and decision.accepted_max_tokens is not None:
            adjusted = dict(kwargs)
            adjusted["max_tokens"] = decision.accepted_max_tokens
            return adjusted
        return kwargs

    def _default_max_tokens(self) -> int | None:
        return _LEGACY_RUNTIME_DEFAULT_MAX_TOKENS_BY_SURFACE.get(
            (self._provider_model.provider, self._model_profile.api_surface),
        )

    def _estimate_context_tokens(
        self,
        messages: list[ChatMessage],
        *,
        extra_context_payloads: list[Any] | None = None,
    ) -> int:
        try:
            estimate = estimate_chat_history_tokens(
                provider=self._provider_model.provider,
                model=self._provider_model.model,
                history=[dict(message) for message in messages],
            )
            extra_tokens = sum(
                estimate_json_tokens(
                    _budget_payload_to_jsonable(payload),
                    provider=self._provider_model.provider,
                    model=self._provider_model.model,
                ).tokens
                for payload in (extra_context_payloads or [])
            )
        except Exception as exc:
            raise LLMConfigurationError(
                (
                    "Unable to estimate context tokens for "
                    f"{self._provider_model.provider}/{self._provider_model.model}; "
                    "refusing LLM call before provider API."
                ),
                provider=self._provider_model.provider,
            ) from exc
        return estimate.tokens + extra_tokens


def _budget_configuration_error(decision: OutputBudgetDecision) -> LLMConfigurationError:
    if decision.reason == "exceeds_model_max_output":
        message = (
            f"Requested max_tokens={decision.requested_max_tokens} for role "
            f"'{decision.role}' exceeds {decision.provider}/{decision.model} "
            f"max_output_tokens={decision.model_max_output_tokens}."
        )
    elif decision.reason == "context_window_exceeded" and decision.context_fit is not None:
        message = (
            f"Requested context plus output budget exceeds "
            f"{decision.provider}/{decision.model} context_window_tokens="
            f"{decision.context_window_tokens} by "
            f"{decision.context_fit.overflow_tokens} tokens."
        )
    else:
        message = (
            f"Invalid max_tokens={decision.requested_max_tokens} for role "
            f"'{decision.role}' and model {decision.provider}/{decision.model}."
        )
    return LLMConfigurationError(message, provider=decision.provider)


def _single_turn_messages(system_prompt: str, user_prompt: str) -> list[ChatMessage]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _budget_payload_to_jsonable(value: Any) -> Any:
    """Return a stable JSON-like representation for budget estimation."""
    if is_dataclass(value) and not isinstance(value, type):
        return _budget_payload_to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _budget_payload_to_jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_budget_payload_to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
