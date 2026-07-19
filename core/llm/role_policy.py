"""
Shared role-based model and reasoning-effort policy authority.

This module resolves role-owned provider/model/effort targets used by both
backend LangGraph services and agent graph/runtime callsites. Role contracts
and role requirements live in sibling modules; provider-specific model defaults
live in provider profile builders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional, cast

from .role_contracts import (
    CANONICAL_REASONING_EFFORT_VALUES,
    DEFAULT_CONVERSATION_MAIN_MODEL,
    DEFAULT_INTERNAL_REASONING_EFFORT,
    DEFAULT_PROVIDER_ID,
    DEFAULT_USER_SELECTED_REASONING_EFFORT,
    InternalRoleModelBinding,
    POST_TOOL_ARTICULATOR_MODEL_REF_ENV,
    ProviderModelBinding,
    ROLE_CONTEXT_COMPRESSOR,
    ROLE_CONVERSATION_MAIN,
    ROLE_INTENT_CLASSIFIER,
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_POST_TOOL_OBSERVATION,
    ROLE_REASONING_MAIN,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
    ReasoningEffort,
    RoleCallSettings,
    RoleKey,
    TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV,
    TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV,
    internal_role_model_ref_env,
)
from .role_requirements import (
    CONVERSATION_INHERITED_ROLE_KEYS,
    INTERNAL_ROLE_KEYS,
    USER_SELECTED_ROLE_KEYS,
    get_role_requirements,
)

InternalModelResolver = Callable[[str, RoleKey], ProviderModelBinding]


def _normalize_model_name(model: Optional[str]) -> Optional[str]:
    if model is None:
        return None
    normalized = model.strip().lower()
    return normalized or None


def _normalize_provider_id(provider: Optional[str]) -> Optional[str]:
    if provider is None:
        return None
    normalized = provider.strip().lower()
    return normalized or None


def validate_reasoning_effort_for_model(
    *,
    effort: Optional[str],
    model: Optional[str],
    provider: Optional[str] = None,
) -> Optional[ReasoningEffort]:
    """Validate and normalize optional reasoning effort for a concrete model."""
    from agent.providers.llm.core.reasoning_policy import (
        validate_reasoning_effort_for_provider_model,
    )

    resolved = validate_reasoning_effort_for_provider_model(
        effort=effort,
        provider=provider,
        model=model,
    )
    return cast(Optional[ReasoningEffort], resolved)


@dataclass(slots=True)
class ModelRoleRegistry:
    """Resolve model names by role using deterministic precedence rules."""

    conversation_main_default: str = DEFAULT_CONVERSATION_MAIN_MODEL
    internal_model_resolver: InternalModelResolver | None = None
    user_selected_reasoning_default: ReasoningEffort = (
        DEFAULT_USER_SELECTED_REASONING_EFFORT
    )
    internal_reasoning_default: ReasoningEffort = DEFAULT_INTERNAL_REASONING_EFFORT
    env_getter: Callable[[str], Optional[str]] = os.getenv

    def resolve_call_settings(
        self,
        role: RoleKey,
        *,
        conversation_model: Optional[str] = None,
        conversation_provider: Optional[str] = None,
        reasoning_model: Optional[str] = None,
        reasoning_provider: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> RoleCallSettings:
        """Resolve model + effort + source for a role-owned call."""

        if _selected_model_owns_all_roles(
            provider=conversation_provider,
            model=conversation_model,
        ):
            return self._resolve_selected_model_settings(
                role=role,
                conversation_provider=conversation_provider,
                conversation_model=conversation_model,
                reasoning_effort=reasoning_effort,
            )

        if role in CONVERSATION_INHERITED_ROLE_KEYS:
            return self._resolve_conversation_inherited_settings(
                role=role,
                conversation_model=conversation_model,
                conversation_provider=conversation_provider,
            )

        if role in USER_SELECTED_ROLE_KEYS:
            resolved_model = self._resolve_user_selected_model(
                role=role,
                conversation_model=conversation_model,
                reasoning_model=reasoning_model,
            )
            resolved_provider = self._resolve_user_selected_provider(
                role=role,
                conversation_model=conversation_model,
                conversation_provider=conversation_provider,
                reasoning_model=reasoning_model,
                reasoning_provider=reasoning_provider,
            )
            resolved_effort = self._resolve_reasoning_effort(
                provider=resolved_provider,
                model=resolved_model,
                requested_effort=reasoning_effort,
                default_effort=self.user_selected_reasoning_default,
            )
            return RoleCallSettings(
                provider=resolved_provider,
                model=resolved_model,
                reasoning_effort=resolved_effort,
                source="user_selected",
            )

        resolved_internal = self._resolve_internal_model_binding(
            role,
            conversation_provider=conversation_provider,
        )
        return RoleCallSettings(
            provider=resolved_internal.provider,
            model=resolved_internal.model,
            reasoning_effort=self._resolve_reasoning_effort(
                provider=resolved_internal.provider,
                model=resolved_internal.model,
                requested_effort=None,
                default_effort=self.internal_reasoning_default,
            ),
            source="internal_fixed",
        )

    def _resolve_selected_model_settings(
        self,
        *,
        role: RoleKey,
        conversation_provider: Optional[str],
        conversation_model: Optional[str],
        reasoning_effort: Optional[str],
    ) -> RoleCallSettings:
        """Resolve a model profile that owns every role to one selected target."""

        provider = _normalize_provider_id(conversation_provider)
        model = _normalize_model_name(conversation_model)
        if provider is None or model is None:
            raise ValueError(
                f"Role '{role}' requires an explicit selected provider and model"
            )
        binding = ProviderModelBinding(provider=provider, model=model)
        _validate_role_binding(
            role=role,
            binding=binding,
            binding_kind="selected-model",
        )
        return RoleCallSettings(
            provider=provider,
            model=model,
            reasoning_effort=self._resolve_reasoning_effort(
                provider=provider,
                model=model,
                requested_effort=reasoning_effort,
                default_effort=self.user_selected_reasoning_default,
            ),
            source="user_selected",
        )

    def _resolve_conversation_inherited_settings(
        self,
        *,
        role: RoleKey,
        conversation_model: Optional[str],
        conversation_provider: Optional[str],
    ) -> RoleCallSettings:
        """Resolve one role that explicitly inherits the conversation target."""

        provider = _normalize_provider_id(conversation_provider)
        model = _normalize_model_name(conversation_model)
        if provider is None or model is None:
            raise ValueError(
                f"Role '{role}' requires an explicit conversation provider and model"
            )
        binding = ProviderModelBinding(provider=provider, model=model)
        _validate_role_binding(
            role=role,
            binding=binding,
            binding_kind="conversation-inherited",
        )
        return RoleCallSettings(
            provider=binding.provider,
            model=binding.model,
            reasoning_effort=None,
            source="user_selected",
        )

    def resolve(
        self,
        role: RoleKey,
        *,
        conversation_model: Optional[str] = None,
        conversation_provider: Optional[str] = None,
        reasoning_model: Optional[str] = None,
        reasoning_provider: Optional[str] = None,
    ) -> str:
        """Resolve model for `role` with explicit role-specific precedence."""

        return self.resolve_call_settings(
            role,
            conversation_model=conversation_model,
            conversation_provider=conversation_provider,
            reasoning_model=reasoning_model,
            reasoning_provider=reasoning_provider,
        ).model

    def _resolve_user_selected_model(
        self,
        *,
        role: RoleKey,
        conversation_model: Optional[str],
        reasoning_model: Optional[str],
    ) -> str:
        normalized_conversation = _normalize_model_name(conversation_model)
        normalized_reasoning = _normalize_model_name(reasoning_model)

        if role == ROLE_CONVERSATION_MAIN:
            return normalized_conversation or self.conversation_main_default

        if role in {ROLE_REASONING_MAIN, ROLE_POST_TOOL_OBSERVATION}:
            return (
                normalized_reasoning
                or normalized_conversation
                or self.conversation_main_default
            )

        if role == ROLE_INTENT_CLASSIFIER:
            return normalized_conversation or self.conversation_main_default

        raise ValueError(f"Unknown user-selected model role: {role}")

    def _resolve_user_selected_provider(
        self,
        *,
        role: RoleKey,
        conversation_model: Optional[str],
        conversation_provider: Optional[str],
        reasoning_model: Optional[str],
        reasoning_provider: Optional[str],
    ) -> str:
        normalized_conversation = _normalize_model_name(conversation_model)
        normalized_reasoning = _normalize_model_name(reasoning_model)
        normalized_conversation_provider = _normalize_provider_id(conversation_provider)
        normalized_reasoning_provider = _normalize_provider_id(reasoning_provider)

        if role == ROLE_CONVERSATION_MAIN:
            return normalized_conversation_provider or DEFAULT_PROVIDER_ID

        if role in {ROLE_REASONING_MAIN, ROLE_POST_TOOL_OBSERVATION}:
            if normalized_reasoning:
                return normalized_reasoning_provider or DEFAULT_PROVIDER_ID
            if normalized_conversation:
                return normalized_conversation_provider or DEFAULT_PROVIDER_ID
            return DEFAULT_PROVIDER_ID

        if role == ROLE_INTENT_CLASSIFIER:
            if normalized_conversation:
                return normalized_conversation_provider or DEFAULT_PROVIDER_ID
            return DEFAULT_PROVIDER_ID

        raise ValueError(f"Unknown user-selected model role: {role}")

    def _resolve_internal_model_binding(
        self,
        role: RoleKey,
        *,
        conversation_provider: Optional[str],
    ) -> ProviderModelBinding:
        env_ref = _parse_provider_model_ref_env(
            self.env_getter(internal_role_model_ref_env(role)),
            env_name=internal_role_model_ref_env(role),
        )
        if env_ref is not None:
            _validate_role_binding(
                role=role,
                binding=env_ref,
                binding_kind="internal",
            )
            return env_ref

        provider = _normalize_provider_id(conversation_provider) or DEFAULT_PROVIDER_ID
        resolver = (
            self.internal_model_resolver
            or _resolve_profile_internal_model_binding
        )
        if role in INTERNAL_ROLE_KEYS:
            return resolver(provider, role)
        raise ValueError(f"Unknown model role: {role}")

    def _resolve_reasoning_effort(
        self,
        *,
        provider: str,
        model: str,
        requested_effort: Optional[str],
        default_effort: ReasoningEffort,
    ) -> Optional[ReasoningEffort]:
        """Resolve role reasoning without applying OpenAI defaults elsewhere."""

        if requested_effort is not None:
            return validate_reasoning_effort_for_model(
                effort=requested_effort,
                provider=provider,
                model=model,
            )
        supports_reasoning, profile_default = _model_reasoning_default(
            provider=provider,
            model=model,
        )
        if not supports_reasoning:
            return None
        resolved_default = (
            default_effort
            if profile_default in {None, "none", "minimal"}
            else profile_default
        )
        return validate_reasoning_effort_for_model(
            effort=resolved_default,
            provider=provider,
            model=model,
        ) or resolved_default


def _resolve_profile_internal_model_binding(
    provider: str,
    role: RoleKey,
) -> ProviderModelBinding:
    """Resolve provider-owned internal role defaults through model profiles."""
    from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
    from agent.providers.llm.profiles.registry import (
        resolve_provider_internal_role_model,
    )

    try:
        ref = resolve_provider_internal_role_model(provider, role)
    except LLMProfileNotFoundError as exc:
        normalized_provider = _normalize_provider_id(provider) or DEFAULT_PROVIDER_ID
        if role in INTERNAL_ROLE_KEYS:
            raise ValueError(
                f"No internal model configured for provider '{normalized_provider}' "
                f"and role '{role}'"
            ) from exc
        raise
    return ProviderModelBinding(provider=ref.provider, model=ref.model)


def _model_reasoning_default(
    *,
    provider: str,
    model: str,
) -> tuple[bool, Optional[ReasoningEffort]]:
    """Return reasoning support and the exact model profile default."""

    from agent.providers.llm.core.capabilities import LLMCapability
    from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
    from agent.providers.llm.core.identity import ProviderModelRef
    from agent.providers.llm.profiles.registry import require_model_profile

    try:
        profile = require_model_profile(ProviderModelRef(provider, model))
    except LLMProfileNotFoundError:
        return False, None
    if not profile.supports(LLMCapability.REASONING_EFFORT):
        return False, None
    return True, cast(Optional[ReasoningEffort], profile.default_reasoning_effort)


def _selected_model_owns_all_roles(
    *,
    provider: Optional[str],
    model: Optional[str],
) -> bool:
    """Return whether the selected model profile replaces internal role models."""

    normalized_provider = _normalize_provider_id(provider)
    normalized_model = _normalize_model_name(model)
    if normalized_provider is None or normalized_model is None:
        return False

    from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
    from agent.providers.llm.core.identity import ProviderModelRef
    from agent.providers.llm.profiles.registry import require_model_profile

    try:
        profile = require_model_profile(
            ProviderModelRef(normalized_provider, normalized_model)
        )
    except LLMProfileNotFoundError:
        return False
    return profile.role_model_policy == "selected_model"


def _parse_provider_model_ref_env(
    value: Optional[str],
    *,
    env_name: str,
) -> Optional[ProviderModelBinding]:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    provider, separator, model = raw.partition("/")
    normalized_provider = _normalize_provider_id(provider)
    normalized_model = _normalize_model_name(model)
    if separator != "/" or normalized_provider is None or normalized_model is None:
        raise ValueError(
            f"{env_name} must be a provider/model reference, for example "
            "'openai/gpt-5-mini'"
        )
    return ProviderModelBinding(provider=normalized_provider, model=normalized_model)


def _validate_role_binding(
    *,
    role: RoleKey,
    binding: ProviderModelBinding,
    binding_kind: str,
) -> None:
    from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
    from agent.providers.llm.core.identity import ProviderModelRef
    from agent.providers.llm.profiles.registry import require_model_profile

    try:
        profile = require_model_profile(
            ProviderModelRef(binding.provider, binding.model)
        )
    except LLMProfileNotFoundError as exc:
        raise ValueError(
            f"No model profile registered for {binding_kind} role '{role}' target "
            f"'{binding.provider}/{binding.model}'"
        ) from exc
    requirements = get_role_requirements(role)
    for capability in requirements.required_capabilities:
        if not profile.supports(capability):
            raise ValueError(
                f"{binding_kind.capitalize()} role '{role}' target "
                f"'{profile.ref}' must support "
                f"{capability}"
            )
    if (
        requirements.structured_output_required
        and not profile.structured_output_strategies
    ):
        raise ValueError(
            f"{binding_kind.capitalize()} role '{role}' target "
            f"'{profile.ref}' must support a "
            "structured output strategy"
        )


__all__ = [
    "CANONICAL_REASONING_EFFORT_VALUES",
    "DEFAULT_CONVERSATION_MAIN_MODEL",
    "DEFAULT_INTERNAL_REASONING_EFFORT",
    "DEFAULT_USER_SELECTED_REASONING_EFFORT",
    "InternalRoleModelBinding",
    "ModelRoleRegistry",
    "POST_TOOL_ARTICULATOR_MODEL_REF_ENV",
    "ProviderModelBinding",
    "ROLE_CONTEXT_COMPRESSOR",
    "ROLE_CONVERSATION_MAIN",
    "ROLE_INTENT_CLASSIFIER",
    "ROLE_POST_TOOL_OBSERVATION",
    "ROLE_POST_TOOL_ARTICULATOR",
    "ROLE_REASONING_MAIN",
    "ROLE_TOOL_CATEGORY_SELECTOR",
    "ROLE_TOOL_OUTPUT_COMPRESSOR",
    "RoleCallSettings",
    "RoleKey",
    "ReasoningEffort",
    "TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV",
    "TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV",
    "validate_reasoning_effort_for_model",
]
