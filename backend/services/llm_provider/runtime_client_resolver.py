"""Turn-local LLMClient resolver for provider-neutral runtime selection.

This service is the narrow adapter-construction boundary. It resolves a
credential ref to a short-lived secret, then delegates provider/model adapter
construction to the tenant baseline `LLMClientFactory`.
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
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.budget_policy import OutputBudgetDecision, decide_output_budget
from agent.providers.llm.core.exceptions import (
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMProfileNotFoundError,
)
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import ModelProfile, require_model_profile
from core.llm.role_policy import RoleCallSettings

from .credential_service import LLMCredentialService
from .types import LLMCredentialRef, LLMCallTarget, LLMRuntimeSelection, ProviderSecret

_UNSET = object()
_LEGACY_RUNTIME_DEFAULT_MAX_TOKENS_BY_SURFACE: dict[tuple[str, str], int] = {
    ("openai", "responses"): 10_000,
    ("openai", "chat_completions"): 10_000,
    ("anthropic", "messages"): 4_096,
}


class LLMRuntimeClientResolver:
    """Resolve runtime selections into concrete provider clients."""

    def __init__(self, credential_service: LLMCredentialService) -> None:
        self._credential_service = credential_service

    def get_client(
        self,
        selection: LLMRuntimeSelection | dict[str, Any],
        *,
        target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
        **client_kwargs: Any,
    ) -> LLMClient:
        """Create an LLMClient for the selected credential context."""

        runtime_selection = LLMRuntimeSelection.from_mapping(selection)
        call_ref = resolve_call_target(runtime_selection, target)
        reasoning_effort_kwarg = client_kwargs.get("reasoning_effort", _UNSET)
        reasoning_effort = (
            reasoning_effort_kwarg
            if reasoning_effort_kwarg is not _UNSET
            else resolve_call_reasoning_effort(runtime_selection, target)
        )
        supported_reasoning_effort = _resolve_supported_reasoning_effort(
            call_ref,
            reasoning_effort,
        )
        if supported_reasoning_effort is not None:
            client_kwargs["reasoning_effort"] = supported_reasoning_effort
        elif reasoning_effort_kwarg is not _UNSET:
            client_kwargs.pop("reasoning_effort", None)
        credential_selection = self._selection_for_call_provider(
            runtime_selection,
            call_ref=call_ref,
            runtime_user_id=runtime_user_id,
        )
        secret = self.resolve_secret(
            credential_selection,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose=purpose,
        )
        client = LLMClientFactory.get_client(
            provider_model=call_ref,
            api_key=secret.value,
            **client_kwargs,
        )
        try:
            model_profile = require_model_profile(call_ref)
        except LLMProfileNotFoundError:
            return client
        return BudgetEnforcingLLMClient(
            client,
            provider_model=call_ref,
            role=_resolve_budget_role(target=target, client_kwargs=client_kwargs),
            model_profile=model_profile,
        )

    def _selection_for_call_provider(
        self,
        runtime_selection: LLMRuntimeSelection,
        *,
        call_ref: ProviderModelRef,
        runtime_user_id: int,
    ) -> LLMRuntimeSelection:
        """Return a selection whose credential provider matches the call target."""

        selected_provider = str(runtime_selection.credential_ref.provider)
        if call_ref.provider == selected_provider:
            return runtime_selection

        credential_ref = self._credential_service.get_credential_ref(
            runtime_user_id,
            call_ref.provider,
        )
        return LLMRuntimeSelection(
            provider=call_ref.provider,
            model=call_ref.model,
            credential_ref=credential_ref,
            reasoning_effort=runtime_selection.reasoning_effort,
        )

    def resolve_secret(
        self,
        selection: LLMRuntimeSelection | dict[str, Any],
        *,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
    ) -> ProviderSecret:
        """Resolve the selected credential context to a short-lived secret."""

        runtime_selection = LLMRuntimeSelection.from_mapping(selection)
        return self._credential_service.resolve_secret(
            runtime_selection.credential_ref,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose=purpose,
        )

    def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        """Return an enabled credential ref for explicit non-chat dependencies."""

        return self._credential_service.get_credential_ref(user_id, provider)


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

    def _default_max_tokens(self) -> int:
        return _LEGACY_RUNTIME_DEFAULT_MAX_TOKENS_BY_SURFACE.get(
            (self._provider_model.provider, self._model_profile.api_surface),
            self._model_profile.max_output_tokens,
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


def _resolve_budget_role(
    *,
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None,
    client_kwargs: dict[str, Any],
) -> str:
    if isinstance(target, LLMCallTarget) and target.role:
        return target.role
    role = client_kwargs.get("resolution_role")
    if role is not None:
        return str(role)
    return "unspecified"


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


def resolve_call_target(
    selection: LLMRuntimeSelection | dict[str, Any],
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
) -> ProviderModelRef:
    """Resolve the provider/model for a concrete LLM call."""

    runtime_selection = LLMRuntimeSelection.from_mapping(selection)
    if target is None:
        return ProviderModelRef(runtime_selection.provider, runtime_selection.model)
    if isinstance(target, ProviderModelRef):
        return target.normalized()
    if isinstance(target, RoleCallSettings):
        return ProviderModelRef(target.provider, target.model).normalized()
    if isinstance(target, LLMCallTarget):
        return ProviderModelRef(target.provider, target.model).normalized()
    raise TypeError(f"Unsupported LLM call target type: {type(target)!r}")


def resolve_call_reasoning_effort(
    selection: LLMRuntimeSelection | dict[str, Any],
    target: ProviderModelRef | RoleCallSettings | LLMCallTarget | None = None,
) -> str | None:
    """Resolve the reasoning effort for a concrete LLM call."""

    runtime_selection = LLMRuntimeSelection.from_mapping(selection)
    if isinstance(target, (RoleCallSettings, LLMCallTarget)):
        return target.reasoning_effort
    return runtime_selection.reasoning_effort


def _resolve_supported_reasoning_effort(
    call_ref: ProviderModelRef,
    reasoning_effort: Any,
) -> str | None:
    """Return a reasoning effort only when the target model supports that option."""

    if reasoning_effort is None:
        return None

    profile = require_model_profile(call_ref)
    if profile.supports(LLMCapability.REASONING_EFFORT):
        normalized_effort = str(reasoning_effort).strip().lower()
        if profile.reasoning_efforts and normalized_effort not in profile.reasoning_efforts:
            allowed = "|".join(sorted(profile.reasoning_efforts))
            raise LLMCapabilityNotSupportedError(
                (
                    f"Model '{call_ref}' does not support reasoning_effort "
                    f"'{reasoning_effort}'. Allowed values: {allowed}."
                ),
                provider=call_ref.provider,
                capability=LLMCapability.REASONING_EFFORT.value,
            )
        return normalized_effort

    raise LLMCapabilityNotSupportedError(
        f"Model '{call_ref}' does not support reasoning_effort",
        provider=call_ref.provider,
        capability=LLMCapability.REASONING_EFFORT.value,
    )


__all__ = [
    "BudgetEnforcingLLMClient",
    "LLMRuntimeClientResolver",
    "resolve_call_reasoning_effort",
    "resolve_call_target",
]
