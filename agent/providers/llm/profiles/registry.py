"""Registry contracts and lookup helpers for provider-neutral LLM metadata.

This module owns the immutable profile registry surface and composes
provider-owned profile data without constructing clients, reading credentials,
importing backend routers, or accessing persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Iterable, Mapping

from core.llm.role_requirements import get_role_requirements

from ..contracts.structured_output_strategy import freeze_structured_output_strategies
from ..contracts.tool_contracts import freeze_tool_choice_modes
from ..core.capabilities import CapabilityInput, LLMCapability, freeze_capabilities, normalize_capability
from ..core.exceptions import LLMCapabilityNotSupportedError, LLMProfileNotFoundError
from ..core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    get_openai_legacy_compatibility_family,
    normalize_model_id,
    normalize_provider_id,
)

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Provider metadata and provider-wide capabilities."""

    id: str
    display_name: str
    capabilities: frozenset[LLMCapability] = field(default_factory=frozenset)
    internal_role_models: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_provider_id(self.id))
        object.__setattr__(self, "capabilities", freeze_capabilities(self.capabilities))
        object.__setattr__(
            self,
            "internal_role_models",
            MappingProxyType(_normalize_internal_role_models(self.internal_role_models)),
        )

    def supports(self, capability: CapabilityInput) -> bool:
        """Return True when the provider exposes a provider-wide capability."""
        return normalize_capability(capability) in self.capabilities

    def require_capability(self, capability: CapabilityInput) -> None:
        """Raise when the provider does not expose a provider-wide capability."""
        normalized = normalize_capability(capability)
        if normalized not in self.capabilities:
            raise LLMCapabilityNotSupportedError(
                f"Provider '{self.id}' does not support capability '{normalized.value}'",
                provider=self.id,
                capability=normalized.value,
            )


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Provider-owned model metadata used for validation and runtime limits."""

    ref: ProviderModelRef
    display_name: str
    api_surface: str
    capabilities: frozenset[LLMCapability]
    context_window_tokens: int
    max_output_tokens: int
    listable: bool
    compatibility_family: str | None = None
    reasoning_efforts: frozenset[str] = field(default_factory=frozenset)
    default_reasoning_effort: str | None = None
    tool_choice_modes: frozenset[str] = field(default_factory=frozenset)
    structured_output_strategies: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", self.ref.normalized())
        object.__setattr__(self, "capabilities", freeze_capabilities(self.capabilities))
        object.__setattr__(
            self,
            "reasoning_efforts",
            frozenset(str(effort).strip().lower() for effort in self.reasoning_efforts),
        )
        object.__setattr__(
            self,
            "tool_choice_modes",
            freeze_tool_choice_modes(self.tool_choice_modes),
        )
        object.__setattr__(
            self,
            "structured_output_strategies",
            freeze_structured_output_strategies(self.structured_output_strategies),
        )
        if self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not isinstance(self.listable, bool):
            raise TypeError("listable must be declared as a bool")

    def supports(self, capability: CapabilityInput) -> bool:
        """Return True when the concrete model/API surface supports a capability."""
        return normalize_capability(capability) in self.capabilities

    def require_capability(self, capability: CapabilityInput) -> None:
        """Raise when the concrete model/API surface lacks a capability."""
        normalized = normalize_capability(capability)
        if normalized not in self.capabilities:
            raise LLMCapabilityNotSupportedError(
                f"Model '{self.ref}' does not support capability '{normalized.value}'",
                provider=self.ref.provider,
                capability=normalized.value,
            )


@dataclass(frozen=True, slots=True)
class _CompatibilityRule:
    """Family-level profile template for approved legacy model compatibility."""

    provider: str
    family_prefix: str
    template: ModelProfile

    def matches(self, ref: ProviderModelRef) -> bool:
        return ref.provider == self.provider and ref.model.startswith(self.family_prefix)


class ModelProfileRegistry:
    """Immutable lookup registry for provider and model profiles."""

    def __init__(
        self,
        *,
        providers: Iterable[ProviderProfile],
        models: Iterable[ModelProfile],
        compatibility_rules: Iterable[_CompatibilityRule],
    ) -> None:
        self._providers = {profile.id: profile for profile in providers}
        self._models = {profile.ref: profile for profile in models}
        self._compatibility_rules = tuple(
            sorted(compatibility_rules, key=lambda rule: len(rule.family_prefix), reverse=True)
        )

    def require_provider_profile(self, provider_id: str) -> ProviderProfile:
        """Return a provider profile or raise an explicit profile error."""
        normalized_provider = normalize_provider_id(provider_id)
        try:
            return self._providers[normalized_provider]
        except KeyError as exc:
            raise LLMProfileNotFoundError(
                f"No provider profile registered for provider '{provider_id}'",
                provider=normalized_provider,
            ) from exc

    def require_model_profile(self, ref: ProviderModelRef) -> ModelProfile:
        """Return an exact or approved compatibility model profile."""
        normalized_ref = ref.normalized()
        exact = self._models.get(normalized_ref)
        if exact is not None:
            return exact
        compatibility = self._resolve_compatibility_profile(normalized_ref)
        if compatibility is not None:
            return compatibility
        raise LLMProfileNotFoundError(
            f"No model profile registered for '{normalized_ref}'",
            provider=normalized_ref.provider,
            model=normalized_ref.model,
        )

    def supports_provider(self, provider_id: str, capability: CapabilityInput) -> bool:
        """Return True when a provider-wide capability is supported."""
        return self.require_provider_profile(provider_id).supports(capability)

    def supports_model(self, ref: ProviderModelRef, capability: CapabilityInput) -> bool:
        """Return True when a concrete model/API surface supports a capability."""
        return self.require_model_profile(ref).supports(capability)

    def require_provider_capability(
        self,
        provider_id: str,
        capability: CapabilityInput,
    ) -> ProviderProfile:
        """Return the provider profile after requiring a provider-wide capability."""
        profile = self.require_provider_profile(provider_id)
        profile.require_capability(capability)
        return profile

    def require_model_capability(
        self,
        ref: ProviderModelRef,
        capability: CapabilityInput,
    ) -> ModelProfile:
        """Return the model profile after requiring a model-specific capability."""
        profile = self.require_model_profile(ref)
        profile.require_capability(capability)
        return profile

    def resolve_provider_internal_role_model(
        self,
        provider_id: str,
        role: str,
    ) -> ProviderModelRef:
        """Return and validate the provider-owned model for an internal role."""
        provider_profile = self.require_provider_profile(provider_id)
        role_key = str(role).strip()
        try:
            model = provider_profile.internal_role_models[role_key]
        except KeyError as exc:
            raise LLMProfileNotFoundError(
                f"No internal model configured for provider '{provider_profile.id}' "
                f"and role '{role_key}'",
                provider=provider_profile.id,
            ) from exc

        ref = ProviderModelRef(provider_profile.id, model)
        model_profile = self.require_model_profile(ref)
        _validate_internal_role_model(role_key, model_profile)
        return model_profile.ref

    def resolve_context_window_tokens(self, ref: ProviderModelRef) -> int:
        """Return the model profile's declared context-window ceiling."""
        return self.require_model_profile(ref).context_window_tokens

    def resolve_max_output_tokens(self, ref: ProviderModelRef) -> int:
        """Return the model profile's declared max-output token limit."""
        return self.require_model_profile(ref).max_output_tokens

    def list_model_profiles(
        self,
        *,
        provider_id: str | None = None,
        listable: bool | None = None,
    ) -> tuple[ModelProfile, ...]:
        """Return exact registered model profiles, optionally filtered."""
        normalized_provider = normalize_provider_id(provider_id) if provider_id is not None else None
        profiles = self._models.values()
        if normalized_provider is not None:
            profiles = (profile for profile in profiles if profile.ref.provider == normalized_provider)
        if listable is not None:
            profiles = (profile for profile in profiles if profile.listable is listable)
        return tuple(sorted(profiles, key=lambda profile: (profile.ref.provider, profile.ref.model)))

    def list_catalog_model_profiles(self, provider_id: str = OPENAI_PROVIDER_ID) -> tuple[ModelProfile, ...]:
        """Return public catalog profiles for a provider."""
        return self.list_model_profiles(provider_id=provider_id, listable=True)

    def _resolve_compatibility_profile(self, ref: ProviderModelRef) -> ModelProfile | None:
        family = get_openai_legacy_compatibility_family(ref.model)
        if ref.provider != OPENAI_PROVIDER_ID or family is None:
            return None
        for rule in self._compatibility_rules:
            if rule.matches(ref):
                return replace(rule.template, ref=ref, compatibility_family=rule.family_prefix)
        return None


def _normalize_internal_role_models(
    internal_role_models: Mapping[str, str],
) -> dict[str, str]:
    """Return normalized role -> model mapping for a provider profile."""
    normalized: dict[str, str] = {}
    for role, model in dict(internal_role_models).items():
        role_key = str(role).strip()
        if not role_key:
            raise ValueError("internal role key cannot be empty")
        normalized[role_key] = normalize_model_id(str(model))
    return normalized


def _validate_internal_role_model(role: str, profile: ModelProfile) -> None:
    """Validate that a model profile can satisfy one internal role."""
    requirements = get_role_requirements(role)
    for capability in requirements.required_capabilities:
        profile.require_capability(capability)
    if requirements.structured_output_required and not profile.structured_output_strategies:
        raise ValueError(
            f"Internal role '{role}' target '{profile.ref}' must support a "
            "structured output strategy"
        )


from .anthropic import (  # noqa: E402
    ANTHROPIC_API_SURFACE_MESSAGES,
    ANTHROPIC_DEFAULT_MODEL_ID,
    ANTHROPIC_EXACT_MODEL_IDS,
    ANTHROPIC_INTERNAL_ROLE_MODELS,
    ANTHROPIC_LISTABLE_MODEL_IDS,
    ANTHROPIC_NON_LISTABLE_MODEL_IDS,
    build_anthropic_model_profiles,
    build_anthropic_provider_profile,
)
from .openai import (  # noqa: E402
    OPENAI_API_SURFACE_CHAT_COMPLETIONS,
    OPENAI_API_SURFACE_RESPONSES,
    OPENAI_DEFAULT_MODEL_ID,
    OPENAI_EXACT_MODEL_IDS,
    OPENAI_GPT_OSS_20B_MODEL_ID,
    OPENAI_INTERNAL_ROLE_MODELS,
    OPENAI_LEGACY_CHAT_MODEL_IDS,
    OPENAI_LISTABLE_MODEL_IDS,
    OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS,
    OPENAI_RESPONSES_MAX_OUTPUT_TOKENS,
    build_openai_compatibility_rules,
    build_openai_model_profiles,
    build_openai_provider_profile,
)


def _build_default_registry() -> ModelProfileRegistry:
    return ModelProfileRegistry(
        providers=(build_openai_provider_profile(), build_anthropic_provider_profile()),
        models=(*build_openai_model_profiles(), *build_anthropic_model_profiles()),
        compatibility_rules=build_openai_compatibility_rules(),
    )


MODEL_PROFILE_REGISTRY = _build_default_registry()


def get_default_model_ref() -> ProviderModelRef:
    """Return the current default provider/model lookup identity."""
    return ProviderModelRef(OPENAI_PROVIDER_ID, OPENAI_DEFAULT_MODEL_ID)


def get_provider_default_model_ref(provider_id: str) -> ProviderModelRef:
    """Return the provider-scoped default model reference."""
    normalized_provider = normalize_provider_id(provider_id)
    provider_defaults = {
        OPENAI_PROVIDER_ID: OPENAI_DEFAULT_MODEL_ID,
        ANTHROPIC_PROVIDER_ID: ANTHROPIC_DEFAULT_MODEL_ID,
    }
    try:
        default_model = provider_defaults[normalized_provider]
    except KeyError as exc:
        raise LLMProfileNotFoundError(
            f"No default model registered for provider '{provider_id}'",
            provider=normalized_provider,
        ) from exc
    ref = ProviderModelRef(normalized_provider, default_model)
    require_model_profile(ref)
    return ref


def require_provider_profile(provider_id: str) -> ProviderProfile:
    """Return a provider profile from the default registry."""
    return MODEL_PROFILE_REGISTRY.require_provider_profile(provider_id)


def require_model_profile(ref: ProviderModelRef) -> ModelProfile:
    """Return an exact or approved compatibility model profile."""
    return MODEL_PROFILE_REGISTRY.require_model_profile(ref)


def supports_provider(provider_id: str, capability: CapabilityInput) -> bool:
    """Return True when a provider-wide capability is supported."""
    return MODEL_PROFILE_REGISTRY.supports_provider(provider_id, capability)


def supports_model(ref: ProviderModelRef, capability: CapabilityInput) -> bool:
    """Return True when a concrete model/API surface supports a capability."""
    return MODEL_PROFILE_REGISTRY.supports_model(ref, capability)


def require_provider_capability(provider_id: str, capability: CapabilityInput) -> ProviderProfile:
    """Require a provider-wide capability and return the provider profile."""
    return MODEL_PROFILE_REGISTRY.require_provider_capability(provider_id, capability)


def require_model_capability(ref: ProviderModelRef, capability: CapabilityInput) -> ModelProfile:
    """Require a model/API-surface capability and return the model profile."""
    return MODEL_PROFILE_REGISTRY.require_model_capability(ref, capability)


def resolve_provider_internal_role_model(provider_id: str, role: str) -> ProviderModelRef:
    """Return the provider-owned model reference for an internal role."""
    return MODEL_PROFILE_REGISTRY.resolve_provider_internal_role_model(provider_id, role)


def resolve_context_window_tokens(ref: ProviderModelRef) -> int:
    """Return the selected model's declared context-window ceiling."""
    return MODEL_PROFILE_REGISTRY.resolve_context_window_tokens(ref)


def resolve_max_output_tokens(ref: ProviderModelRef) -> int:
    """Return the selected model's declared max-output token limit."""
    return MODEL_PROFILE_REGISTRY.resolve_max_output_tokens(ref)


def list_model_profiles(
    *,
    provider_id: str | None = None,
    listable: bool | None = None,
) -> tuple[ModelProfile, ...]:
    """Return exact registered model profiles from the default registry."""
    return MODEL_PROFILE_REGISTRY.list_model_profiles(provider_id=provider_id, listable=listable)


def list_catalog_model_profiles(provider_id: str = OPENAI_PROVIDER_ID) -> tuple[ModelProfile, ...]:
    """Return public catalog profiles from the default registry."""
    return MODEL_PROFILE_REGISTRY.list_catalog_model_profiles(provider_id)


__all__ = [
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MODEL_PROFILE_REGISTRY",
    "ANTHROPIC_API_SURFACE_MESSAGES",
    "ANTHROPIC_DEFAULT_MODEL_ID",
    "ANTHROPIC_EXACT_MODEL_IDS",
    "ANTHROPIC_INTERNAL_ROLE_MODELS",
    "ANTHROPIC_LISTABLE_MODEL_IDS",
    "ANTHROPIC_NON_LISTABLE_MODEL_IDS",
    "OPENAI_API_SURFACE_CHAT_COMPLETIONS",
    "OPENAI_API_SURFACE_RESPONSES",
    "OPENAI_DEFAULT_MODEL_ID",
    "OPENAI_EXACT_MODEL_IDS",
    "OPENAI_GPT_OSS_20B_MODEL_ID",
    "OPENAI_INTERNAL_ROLE_MODELS",
    "OPENAI_LEGACY_CHAT_MODEL_IDS",
    "OPENAI_LISTABLE_MODEL_IDS",
    "OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS",
    "OPENAI_RESPONSES_MAX_OUTPUT_TOKENS",
    "ModelProfile",
    "ModelProfileRegistry",
    "ProviderProfile",
    "get_default_model_ref",
    "get_provider_default_model_ref",
    "list_catalog_model_profiles",
    "list_model_profiles",
    "require_model_capability",
    "require_model_profile",
    "require_provider_capability",
    "require_provider_profile",
    "resolve_provider_internal_role_model",
    "resolve_context_window_tokens",
    "resolve_max_output_tokens",
    "supports_model",
    "supports_provider",
]
