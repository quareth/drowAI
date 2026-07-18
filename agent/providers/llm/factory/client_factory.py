"""Factory for creating provider-neutral LLMClient instances.

This module owns adapter construction at the LLM provider boundary. Explicit
``provider + model`` resolution is the primary path; legacy model-prefix
matching remains only as a compatibility fallback for existing model-only
callers and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Any, Callable, Dict, Type

from ..core.base import LLMClient
from ..core.exceptions import (
    LLMConfigurationError,
    LLMProfileNotFoundError,
    LLMProviderNotFoundError,
)
from ..core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    ProviderModelResolution,
    normalize_provider_id,
    resolve_legacy_openai_model_ref,
)
from ..profiles import (
    ANTHROPIC_API_SURFACE_MESSAGES,
    OPENAI_API_SURFACE_CHAT_COMPLETIONS,
    OPENAI_API_SURFACE_RESPONSES,
    ModelProfile,
    list_model_profiles,
    require_model_profile,
)

logger = logging.getLogger(__name__)

ProviderAdapterResolver = Callable[[ModelProfile], Type[LLMClient]]


@dataclass(frozen=True, slots=True)
class _ProviderRegistration:
    """Adapter resolver registered for one provider id."""

    resolver: ProviderAdapterResolver
    adapter_names: tuple[str, ...]


class LLMClientFactory:
    """Factory for creating LLMClient instances.

    Providers register by provider id. Adapter resolvers receive the selected
    model profile and return the concrete LLMClient implementation for that
    model/API surface.

    Legacy prefix registration remains available for model-only compatibility
    and isolated tests. It is not used when provider identity is supplied.
    
    Thread Safety:
        All methods are thread-safe. Registration uses a lock to prevent
        race conditions during provider registration.
    
    Example:
        from .identity import ProviderModelRef

        client = LLMClientFactory.get_client(
            provider_model=ProviderModelRef("openai", "gpt-5.2"),
            api_key="...",
        )
        response = await client.chat("System prompt", "User message")
    """
    
    _provider_registry: Dict[str, _ProviderRegistration] = {}
    _registry: Dict[str, Type[LLMClient]] = {}
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def register_provider(
        cls,
        provider_id: str,
        adapter_resolver: ProviderAdapterResolver | Type[LLMClient],
        *,
        adapter_names: tuple[str, ...] | None = None,
    ) -> None:
        """Register an adapter resolver for a provider id.

        Args:
            provider_id: Stable provider id, for example ``"openai"``.
            adapter_resolver: Callable that maps a ModelProfile to a concrete
                LLMClient class. A single LLMClient class may be supplied for
                providers with one adapter.
            adapter_names: Optional names exposed by introspection.
        """
        normalized_provider = normalize_provider_id(provider_id)
        resolver = cls._normalize_adapter_resolver(adapter_resolver)
        names = adapter_names or cls._adapter_names_for(adapter_resolver)
        if not names:
            raise ValueError("adapter_names cannot be empty")

        with cls._lock:
            cls._provider_registry[normalized_provider] = _ProviderRegistration(
                resolver=resolver,
                adapter_names=tuple(names),
            )
            logger.debug(
                "Registered LLM provider '%s' with adapters %s",
                normalized_provider,
                ", ".join(names),
            )

    @classmethod
    def _normalize_adapter_resolver(
        cls,
        adapter_resolver: ProviderAdapterResolver | Type[LLMClient],
    ) -> ProviderAdapterResolver:
        """Return a resolver callable after validating the registration input."""
        if isinstance(adapter_resolver, type):
            if not issubclass(adapter_resolver, LLMClient):
                raise TypeError(
                    "adapter_resolver class must be a subclass of LLMClient"
                )

            def _single_adapter_resolver(_profile: ModelProfile) -> Type[LLMClient]:
                return adapter_resolver

            return _single_adapter_resolver

        if not callable(adapter_resolver):
            raise TypeError("adapter_resolver must be callable or an LLMClient subclass")
        return adapter_resolver

    @staticmethod
    def _adapter_names_for(
        adapter_resolver: ProviderAdapterResolver | Type[LLMClient],
    ) -> tuple[str, ...]:
        """Return stable introspection names for a registered resolver."""
        if isinstance(adapter_resolver, type):
            return (adapter_resolver.__name__,)
        name = getattr(adapter_resolver, "__name__", adapter_resolver.__class__.__name__)
        return (str(name),)
    
    @classmethod
    def register(cls, prefix: str, provider_class: Type[LLMClient]) -> None:
        """Register a legacy provider class for a model prefix.
        
        The prefix is matched against the start of model identifiers.
        More specific prefixes take precedence (longest match wins).
        This path exists for compatibility with model-only callers. New provider
        integrations should use register_provider().
        
        Args:
            prefix: Model prefix to match (e.g., "gpt-5", "gpt-5-mini", "claude-3")
            provider_class: LLMClient subclass to instantiate for matching models
            
        Raises:
            TypeError: If provider_class is not a subclass of LLMClient
            
        Example:
            # Legacy compatibility registration only.
            LLMClientFactory.register("gpt-5", OpenAIResponsesClient)
            # Now "gpt-5", "gpt-5-mini" can resolve via OpenAIResponsesClient
        """
        if not isinstance(provider_class, type) or not issubclass(provider_class, LLMClient):
            raise TypeError(
                f"provider_class must be a subclass of LLMClient, got {type(provider_class)}"
            )
        
        prefix_lower = prefix.lower().strip()
        if not prefix_lower:
            raise ValueError("prefix cannot be empty")
        
        with cls._lock:
            cls._registry[prefix_lower] = provider_class
            logger.debug(
                f"Registered provider {provider_class.__name__} for prefix '{prefix_lower}'"
            )
    
    @classmethod
    def unregister(cls, prefix: str) -> bool:
        """Remove a provider registration.
        
        Args:
            prefix: The prefix to unregister
            
        Returns:
            True if a provider was unregistered, False if prefix was not found
        """
        prefix_lower = prefix.lower().strip()
        with cls._lock:
            if prefix_lower in cls._registry:
                del cls._registry[prefix_lower]
                logger.debug(f"Unregistered provider for prefix '{prefix_lower}'")
                return True
            return False
    
    @classmethod
    def get_client(
        cls, 
        *, 
        model: str | None = None,
        api_key: str,
        provider: str | None = None,
        provider_model: ProviderModelRef | None = None,
        model_profile: ModelProfile | None = None,
        **kwargs: Any,
    ) -> LLMClient:
        """Create a client for the requested provider/model.
        
        Explicit provider/model resolution is preferred. Model-only calls are
        retained as a legacy OpenAI compatibility path, then as longest-prefix
        fallback for existing isolated registrations.
        
        Args:
            model: Model identifier when provider_model is not supplied.
            api_key: API key for the provider
            provider: Optional provider id convenience argument.
            provider_model: Canonical provider/model identity.
            **kwargs: Additional arguments passed to the provider constructor
            
        Returns:
            Configured LLMClient instance
            
        Raises:
            LLMConfigurationError: If api_key is empty or None
            LLMProviderNotFoundError: If no provider matches the model
            
        Example:
            client = LLMClientFactory.get_client(
                provider_model=ProviderModelRef("openai", "gpt-5-mini"),
                api_key="sk-...",
            )
        """
        if not api_key or not isinstance(api_key, str) or not api_key.strip():
            raise LLMConfigurationError(
                "API key is required and cannot be empty",
                provider=None,
            )

        if provider_model is not None or provider is not None:
            resolution = cls._resolve_explicit_request(
                provider_model=provider_model,
                provider=provider,
                model=model,
            )
            return cls._create_provider_client(
                resolution=resolution,
                api_key=api_key,
                model_profile=model_profile,
                **kwargs,
            )

        if not model or not isinstance(model, str) or not model.strip():
            raise LLMConfigurationError(
                "Model identifier is required and cannot be empty",
                provider=None,
            )

        openai_resolution = cls._resolve_legacy_openai_request_if_available(model)
        if openai_resolution is not None:
            return cls._create_provider_client(
                resolution=openai_resolution,
                api_key=api_key,
                **kwargs,
            )

        provider_class = cls._find_provider(model.lower().strip())
        if provider_class is None:
            raise LLMProviderNotFoundError(
                f"No provider registered for model '{model}'",
                model=model,
                available_prefixes=cls._list_prefixes_unlocked(),
            )

        logger.debug("Creating %s for model='%s'", provider_class.__name__, model)
        return provider_class(api_key=api_key, model=model, **kwargs)

    @classmethod
    def _resolve_explicit_request(
        cls,
        *,
        provider_model: ProviderModelRef | None,
        provider: str | None,
        model: str | None,
    ) -> ProviderModelResolution:
        """Resolve an explicit provider/model request for profile lookup."""
        if provider_model is not None:
            if not isinstance(provider_model, ProviderModelRef):
                raise LLMConfigurationError(
                    "provider_model must be a ProviderModelRef",
                    provider=None,
                )
            normalized_ref = provider_model.normalized()
            if provider is not None and normalize_provider_id(provider) != normalized_ref.provider:
                raise LLMConfigurationError(
                    "provider must match provider_model.provider when both are supplied",
                    provider=provider_model.provider,
                )
            if model is not None and model != provider_model.model:
                raise LLMConfigurationError(
                    "model must match provider_model.model when both are supplied",
                    provider=provider_model.provider,
                )
            return ProviderModelResolution(
                lookup_ref=normalized_ref,
                provider_request_model=provider_model.model,
            )

        if not model or not isinstance(model, str) or not model.strip():
            raise LLMConfigurationError(
                "Model identifier is required and cannot be empty",
                provider=None,
            )
        if provider is None:
            raise LLMProviderNotFoundError(
                "Provider id is required when provider_model is not supplied",
                model=model,
                available_prefixes=cls._list_provider_ids_unlocked(),
            )

        ref = ProviderModelRef(provider=provider, model=model)
        return ProviderModelResolution(
            lookup_ref=ref.normalized(),
            provider_request_model=model,
        )

    @classmethod
    def _resolve_legacy_openai_request_if_available(
        cls,
        model: str,
    ) -> ProviderModelResolution | None:
        """Resolve model-only OpenAI calls when the OpenAI provider is registered."""
        with cls._lock:
            has_openai_provider = OPENAI_PROVIDER_ID in cls._provider_registry
        if not has_openai_provider:
            return None

        try:
            return resolve_legacy_openai_model_ref(model)
        except LLMProviderNotFoundError:
            return None

    @classmethod
    def _create_provider_client(
        cls,
        *,
        resolution: ProviderModelResolution,
        api_key: str,
        model_profile: ModelProfile | None = None,
        **kwargs: Any,
    ) -> LLMClient:
        """Instantiate the provider adapter for a resolved provider/model."""
        lookup_ref = resolution.lookup_ref.normalized()
        registration = cls._get_provider_registration(lookup_ref.provider)
        profile = model_profile or require_model_profile(lookup_ref)
        if profile.ref.provider != lookup_ref.provider:
            raise LLMConfigurationError(
                "model_profile provider must match the requested provider",
                provider=lookup_ref.provider,
            )
        provider_class = registration.resolver(profile)
        if not isinstance(provider_class, type) or not issubclass(provider_class, LLMClient):
            raise LLMConfigurationError(
                "Provider adapter resolver must return an LLMClient subclass",
                provider=lookup_ref.provider,
            )

        logger.debug(
            "Creating %s for provider='%s' model='%s' profile='%s'",
            provider_class.__name__,
            lookup_ref.provider,
            resolution.provider_request_model,
            profile.ref,
        )
        return provider_class(
            api_key=api_key,
            model=resolution.provider_request_model,
            **kwargs,
        )

    @classmethod
    def _get_provider_registration(cls, provider_id: str) -> _ProviderRegistration:
        """Return a provider registration or raise provider-not-found."""
        normalized_provider = normalize_provider_id(provider_id)
        with cls._lock:
            registration = cls._provider_registry.get(normalized_provider)
            available = cls._list_provider_ids_unlocked()
        if registration is None:
            raise LLMProviderNotFoundError(
                f"No provider registered for provider '{provider_id}'",
                available_prefixes=available,
            )
        return registration
    
    @classmethod
    def _find_provider(cls, model_lower: str) -> Type[LLMClient] | None:
        """Find the legacy provider with the longest matching prefix.
        
        Args:
            model_lower: Lowercase model identifier
            
        Returns:
            Provider class or None if no match found
        """
        with cls._lock:
            matching_prefixes = [
                prefix for prefix in cls._registry.keys()
                if model_lower.startswith(prefix)
            ]
        
        if not matching_prefixes:
            return None
        
        # Select longest matching prefix
        longest_prefix = max(matching_prefixes, key=len)
        return cls._registry[longest_prefix]
    
    @classmethod
    def list_providers(cls) -> Dict[str, str]:
        """Return registered provider id -> adapter name mapping.
        
        Useful for debugging and introspection.
        
        Returns:
            Dict mapping provider ids to adapter names
            
        Example:
            >>> LLMClientFactory.list_providers()
            {'openai': 'OpenAIChatClient, OpenAIResponsesClient'}
        """
        with cls._lock:
            return {
                provider_id: ", ".join(registration.adapter_names)
                for provider_id, registration in cls._provider_registry.items()
            }

    @classmethod
    def list_prefix_registrations(cls) -> Dict[str, str]:
        """Return legacy prefix -> provider class name mappings."""
        with cls._lock:
            return {
                prefix: provider_class.__name__
                for prefix, provider_class in cls._registry.items()
            }

    @classmethod
    def list_models(
        cls,
        provider_id: str,
        *,
        listable: bool | None = None,
    ) -> tuple[str, ...]:
        """Return registered profile model ids for a provider."""
        cls._get_provider_registration(provider_id)
        return tuple(
            profile.ref.model
            for profile in list_model_profiles(provider_id=provider_id, listable=listable)
        )
    
    @classmethod
    def is_registered(cls, prefix: str) -> bool:
        """Check if a legacy prefix has a registered provider.
        
        Args:
            prefix: The prefix to check
            
        Returns:
            True if a provider is registered for this prefix
        """
        with cls._lock:
            return prefix.lower().strip() in cls._registry

    @classmethod
    def is_provider_registered(cls, provider_id: str) -> bool:
        """Check if a provider id has a registered adapter resolver."""
        normalized_provider = normalize_provider_id(provider_id)
        with cls._lock:
            return normalized_provider in cls._provider_registry
    
    @classmethod
    def clear_registry(cls) -> None:
        """Clear all registered providers and legacy prefixes.
        
        WARNING: This is primarily for testing. Do not use in production code.
        """
        with cls._lock:
            cls._provider_registry.clear()
            cls._registry.clear()
            logger.warning("LLMClientFactory registry cleared")

    @classmethod
    def _list_prefixes_unlocked(cls) -> list[str]:
        """Return legacy prefix keys while the class lock is held or not contended."""
        return list(cls._registry.keys())

    @classmethod
    def _list_provider_ids_unlocked(cls) -> list[str]:
        """Return provider ids while the class lock is held or not contended."""
        return list(cls._provider_registry.keys())


def _register_default_providers() -> None:
    """Register built-in providers at module load time.
    
    This function is called automatically when the module is imported.
    It registers OpenAI under the provider-aware primary path and preserves
    legacy prefix mappings for model-only compatibility.
    """
    from ..adapters.anthropic.client import AnthropicMessagesClient
    from ..adapters.openai.chat import OpenAIChatClient
    from ..adapters.openai.responses.client import OpenAIResponsesClient

    def _resolve_openai_adapter(profile: ModelProfile) -> Type[LLMClient]:
        if profile.api_surface == OPENAI_API_SURFACE_RESPONSES:
            return OpenAIResponsesClient
        if profile.api_surface == OPENAI_API_SURFACE_CHAT_COMPLETIONS:
            return OpenAIChatClient
        raise LLMProfileNotFoundError(
            f"No OpenAI adapter registered for API surface '{profile.api_surface}'",
            provider=OPENAI_PROVIDER_ID,
            model=profile.ref.model,
        )

    def _resolve_anthropic_adapter(profile: ModelProfile) -> Type[LLMClient]:
        if profile.api_surface != ANTHROPIC_API_SURFACE_MESSAGES:
            raise LLMProfileNotFoundError(
                f"No Anthropic adapter registered for API surface '{profile.api_surface}'",
                provider=ANTHROPIC_PROVIDER_ID,
                model=profile.ref.model,
            )
        return AnthropicMessagesClient

    LLMClientFactory.register_provider(
        OPENAI_PROVIDER_ID,
        _resolve_openai_adapter,
        adapter_names=(
            OpenAIChatClient.__name__,
            OpenAIResponsesClient.__name__,
        ),
    )
    LLMClientFactory.register_provider(
        ANTHROPIC_PROVIDER_ID,
        _resolve_anthropic_adapter,
        adapter_names=(AnthropicMessagesClient.__name__,),
    )
        
    # -----------------------------------------------------------------------
    # Legacy model-only fallback prefixes. Provider-aware calls never use
    # these mappings; derive them from exact OpenAI profiles so factory
    # routing and catalog/profile metadata cannot drift silently.
    # -----------------------------------------------------------------------
    for profile in list_model_profiles(provider_id=OPENAI_PROVIDER_ID):
        LLMClientFactory.register(
            profile.ref.model,
            _resolve_openai_adapter(profile),
        )
        
    logger.debug("Default LLM providers registered successfully")


# Auto-register default providers when module is loaded
_register_default_providers()


__all__ = ["LLMClientFactory"]
