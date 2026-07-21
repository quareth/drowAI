"""
Shared role-based model and reasoning-effort policy authority.

This module resolves role-owned provider/model/effort targets used by both
backend LangGraph services and agent graph/runtime callsites. Role contracts
and role requirements live in sibling modules; model capabilities live in
provider profile builders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, cast

from .role_contracts import (
    CANONICAL_REASONING_EFFORT_VALUES,
    DEFAULT_CONVERSATION_MAIN_MODEL,
    DEFAULT_INTERNAL_REASONING_EFFORT,
    DEFAULT_PROVIDER_ID,
    DEFAULT_USER_SELECTED_REASONING_EFFORT,
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
)
from .role_requirements import (
    CONVERSATION_INHERITED_ROLE_KEYS,
    INTERNAL_ROLE_KEYS,
    USER_SELECTED_ROLE_KEYS,
    get_role_requirements,
)


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
    """Resolve one selected model with deterministic per-role call policy."""

    conversation_main_default: str = DEFAULT_CONVERSATION_MAIN_MODEL
    user_selected_reasoning_default: ReasoningEffort = (
        DEFAULT_USER_SELECTED_REASONING_EFFORT
    )
    internal_reasoning_default: ReasoningEffort = DEFAULT_INTERNAL_REASONING_EFFORT

    def resolve_call_settings(
        self,
        role: RoleKey,
        *,
        conversation_model: Optional[str] = None,
        conversation_provider: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> RoleCallSettings:
        """Resolve the selected target and provider-neutral effort for one role.

        Model identity is deliberately owned only by the submitted conversation
        selection; roles may vary effort, not deployment or model.
        """

        allowed_roles = (
            USER_SELECTED_ROLE_KEYS
            | CONVERSATION_INHERITED_ROLE_KEYS
            | INTERNAL_ROLE_KEYS
        )
        if role not in allowed_roles:
            raise ValueError(f"Unknown model role: {role}")

        provider = _normalize_provider_id(conversation_provider) or DEFAULT_PROVIDER_ID
        model = (
            _normalize_model_name(conversation_model)
            or _normalize_model_name(self.conversation_main_default)
        )
        if model is None:
            raise ValueError(f"Role '{role}' requires a selected model")

        binding = ProviderModelBinding(provider=provider, model=model)
        if role in (CONVERSATION_INHERITED_ROLE_KEYS | INTERNAL_ROLE_KEYS):
            _validate_role_binding(
                role=role,
                binding=binding,
                binding_kind="selected-model",
            )

        if role in INTERNAL_ROLE_KEYS:
            resolved_effort = self._resolve_internal_reasoning_effort(
                provider=provider,
                model=model,
            )
        elif role in CONVERSATION_INHERITED_ROLE_KEYS:
            resolved_effort = None
        else:
            resolved_effort = self._resolve_reasoning_effort(
                provider=provider,
                model=model,
                requested_effort=reasoning_effort,
                default_effort=self.user_selected_reasoning_default,
            )

        return RoleCallSettings(
            provider=provider,
            model=model,
            reasoning_effort=resolved_effort,
            source="user_selected",
        )

    def resolve(
        self,
        role: RoleKey,
        *,
        conversation_model: Optional[str] = None,
        conversation_provider: Optional[str] = None,
    ) -> str:
        """Resolve model for `role` with explicit role-specific precedence."""

        return self.resolve_call_settings(
            role,
            conversation_model=conversation_model,
            conversation_provider=conversation_provider,
        ).model

    def _resolve_internal_reasoning_effort(
        self,
        *,
        provider: str,
        model: str,
    ) -> Optional[ReasoningEffort]:
        """Return low reasoning, or the closest supported lower-cost effort."""

        from agent.providers.llm.core.capabilities import LLMCapability
        from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
        from agent.providers.llm.core.identity import ProviderModelRef
        from agent.providers.llm.profiles.registry import require_model_profile

        try:
            profile = require_model_profile(ProviderModelRef(provider, model))
        except LLMProfileNotFoundError:
            return None
        if not profile.supports(LLMCapability.REASONING_EFFORT):
            return None

        preferred = self.internal_reasoning_default
        supported = profile.reasoning_efforts
        if not supported:
            return None
        if preferred in supported:
            selected = preferred
        else:
            ranks = {
                effort: index
                for index, effort in enumerate(CANONICAL_REASONING_EFFORT_VALUES)
            }
            preferred_rank = ranks[preferred]
            ranked_supported = [
                effort for effort in supported if effort in ranks
            ]
            if not ranked_supported:
                return None
            selected = min(
                ranked_supported,
                key=lambda effort: (
                    ranks[effort] > preferred_rank,
                    abs(ranks[effort] - preferred_rank),
                ),
            )
        return validate_reasoning_effort_for_model(
            effort=selected,
            provider=provider,
            model=model,
        )

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
    "ModelRoleRegistry",
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
    "validate_reasoning_effort_for_model",
]
