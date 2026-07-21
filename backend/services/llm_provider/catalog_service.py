"""Provider catalog facade backed by the tenant baseline LLM profile registry.

This service exposes provider/model metadata and validation for backend
routers and runtime services. It does not own provider SDK clients or
credential storage.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.factory.client_factory import LLMClientFactory
from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    normalize_model_id,
    normalize_provider_id,
)
from agent.providers.llm.profiles.registry import (
    OPENAI_API_SURFACE_RESPONSES,
    ModelProfile,
    ProviderProfile,
    get_default_model_ref,
    get_provider_default_model_ref,
    list_catalog_model_profiles,
    list_model_profiles,
    require_model_profile,
    require_provider_profile,
)
from backend.services.usage_tracking.pricing_registry import get_pricing_quote

from .types import ProviderConfigurationError


@dataclass(frozen=True, slots=True)
class CatalogModelSummary:
    """Listable model metadata exposed to API routes."""

    id: str
    canonical_model_id: str
    exact_wire_model_id: str | None
    label: str
    api_surface: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    max_output_tokens: int
    reasoning_efforts: tuple[str, ...]
    visible_reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    default_visible_reasoning_effort: str | None
    tool_choice_modes: tuple[str, ...]
    structured_output_strategies: tuple[str, ...]
    pricing_status: str


@dataclass(frozen=True, slots=True)
class CatalogProviderSummary:
    """Provider metadata with public model summaries."""

    id: str
    label: str
    capabilities: tuple[str, ...]
    available: bool
    selectable: bool
    models: tuple[CatalogModelSummary, ...]
    default_model: str


PROVIDER_DISPLAY_ORDER: tuple[str, ...] = (
    OPENAI_PROVIDER_ID,
    ANTHROPIC_PROVIDER_ID,
)


class LLMProviderCatalogService:
    """Profile-registry backed provider/model catalog service."""

    def list_providers(self) -> tuple[CatalogProviderSummary, ...]:
        """Return supported providers with public catalog models."""

        providers = []
        provider_ids = _sort_provider_ids(
            {
                profile.ref.provider
                for profile in list_model_profiles(listable=True)
            }
        )
        for provider_id in provider_ids:
            provider_profile = self.require_provider(provider_id)
            default_ref = get_provider_default_model_ref(provider_profile.id)
            adapter_available = self._provider_adapter_available(provider_profile.id)
            models = tuple(
                self._model_summary(profile)
                for profile in list_catalog_model_profiles(provider_profile.id)
            )
            providers.append(
                CatalogProviderSummary(
                    id=provider_profile.id,
                    label=provider_profile.display_name,
                    capabilities=_capability_values(provider_profile.capabilities),
                    available=adapter_available,
                    selectable=adapter_available,
                    models=models,
                    default_model=default_ref.model,
                )
            )
        return tuple(providers)

    def list_provider_models(self, provider: str) -> tuple[ModelProfile, ...]:
        """Return public catalog model profiles for a provider."""

        normalized_provider = normalize_provider_id(provider)
        self.require_provider(normalized_provider)
        if not self._provider_adapter_available(normalized_provider):
            return ()
        return list_catalog_model_profiles(normalized_provider)

    def default_model_ref(self) -> ProviderModelRef:
        """Return the default provider/model reference."""

        return get_default_model_ref()

    def require_provider(self, provider: str) -> ProviderProfile:
        """Validate and return provider metadata."""

        try:
            return require_provider_profile(normalize_provider_id(provider))
        except (LLMProfileNotFoundError, ValueError, TypeError) as exc:
            raise ProviderConfigurationError(f"Unknown LLM provider: {provider}") from exc

    def require_model(self, provider: str, model: str) -> ModelProfile:
        """Validate and return a provider/model profile."""

        ref = ProviderModelRef(
            provider=normalize_provider_id(provider),
            model=normalize_model_id(model),
        )
        try:
            return require_model_profile(ref)
        except (LLMProfileNotFoundError, ValueError, TypeError) as exc:
            raise ProviderConfigurationError(f"Unknown or unsupported model: {ref}") from exc

    def require_selectable_model(self, provider: str, model: str) -> ModelProfile:
        """Validate a user-selectable conversation model.

        Public catalog exposure and route-compatible selectability are related
        but not identical. OpenAI keeps its approved compatibility-family
        route behavior, while new providers must have exact listable profiles
        and a registered adapter before selection is accepted.
        """

        profile = self.require_model(provider, model)
        if not profile.supports(LLMCapability.CHAT):
            raise ProviderConfigurationError(
                f"Model '{profile.ref}' is not selectable because it does not support chat"
            )
        if not self._provider_adapter_available(profile.ref.provider):
            raise ProviderConfigurationError(
                f"LLM provider adapter is not registered: {profile.ref.provider}"
            )
        if profile.ref.provider == OPENAI_PROVIDER_ID and profile.api_surface != OPENAI_API_SURFACE_RESPONSES:
            raise ProviderConfigurationError("Only OpenAI GPT-5 Responses models are selectable")
        if profile.ref.provider != OPENAI_PROVIDER_ID and not profile.listable:
            raise ProviderConfigurationError(f"Model '{profile.ref}' is not selectable")
        return profile

    def is_provider_adapter_available(self, provider: str) -> bool:
        """Return True when the provider has a registered client adapter."""

        return self._provider_adapter_available(normalize_provider_id(provider))

    def normalize_provider_model(self, provider: str, model: str) -> ProviderModelRef:
        """Return a normalized provider/model reference after validation."""

        profile = self.require_model(provider, model)
        return profile.ref

    @staticmethod
    def _provider_adapter_available(provider: str) -> bool:
        """Return True when the provider has a registered client adapter."""
        return LLMClientFactory.is_provider_registered(provider)

    @staticmethod
    def _model_summary(profile: ModelProfile) -> CatalogModelSummary:
        """Build public catalog metadata for one model profile."""

        return CatalogModelSummary(
            id=profile.ref.model,
            canonical_model_id=profile.canonical_model_id or str(profile.ref),
            exact_wire_model_id=None,
            label=profile.display_name,
            api_surface=profile.api_surface,
            capabilities=_capability_values(profile.capabilities),
            context_window_tokens=profile.context_window_tokens,
            max_output_tokens=profile.max_output_tokens,
            reasoning_efforts=_ordered_values(profile.reasoning_efforts, _REASONING_EFFORT_ORDER),
            visible_reasoning_efforts=_visible_reasoning_efforts(profile),
            default_reasoning_effort=profile.default_reasoning_effort,
            default_visible_reasoning_effort=_default_visible_reasoning_effort(profile),
            tool_choice_modes=_ordered_values(profile.tool_choice_modes, _TOOL_CHOICE_MODE_ORDER),
            structured_output_strategies=tuple(sorted(profile.structured_output_strategies)),
            pricing_status=get_pricing_quote(
                profile.ref,
                api_surface=profile.api_surface,
            ).status,
        )


_REASONING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_VISIBLE_REASONING_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
_TOOL_CHOICE_MODE_ORDER = ("auto", "none", "required", "specific")


def _capability_values(capabilities) -> tuple[str, ...]:
    """Return stable capability values for API responses."""

    return tuple(sorted(capability.value for capability in capabilities))


def _sort_provider_ids(provider_ids: set[str] | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Sort providers in backend-owned product display order."""

    normalized = {normalize_provider_id(provider) for provider in provider_ids}
    ordered = [provider for provider in PROVIDER_DISPLAY_ORDER if provider in normalized]
    ordered.extend(sorted(normalized.difference(PROVIDER_DISPLAY_ORDER)))
    return tuple(ordered)


def _ordered_values(values, preferred_order: tuple[str, ...]) -> tuple[str, ...]:
    """Return stable string values with known values in product order."""

    normalized = {str(value) for value in values}
    ordered = [value for value in preferred_order if value in normalized]
    ordered.extend(sorted(normalized.difference(preferred_order)))
    return tuple(ordered)


def _visible_reasoning_efforts(profile: ModelProfile) -> tuple[str, ...]:
    """Return backend-owned reasoning efforts shown in the primary menu."""

    if not profile.supports(LLMCapability.REASONING_EFFORT):
        return ()
    visible = set(profile.reasoning_efforts).intersection(_VISIBLE_REASONING_EFFORT_ORDER)
    return _ordered_values(visible, _VISIBLE_REASONING_EFFORT_ORDER)


def _default_visible_reasoning_effort(profile: ModelProfile) -> str | None:
    """Return the primary menu's default visible reasoning effort."""

    visible = _visible_reasoning_efforts(profile)
    if not visible:
        return None
    if profile.default_reasoning_effort in visible:
        return profile.default_reasoning_effort
    return "medium" if "medium" in visible else visible[0]


__all__ = [
    "CatalogModelSummary",
    "CatalogProviderSummary",
    "LLMProviderCatalogService",
]
