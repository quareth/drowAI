"""Provider/model identity contracts for the LLM provider boundary.

This module owns normalized provider/model lookup identities and legacy
OpenAI model-only compatibility resolution. It deliberately contains no
client construction, credential access, router imports, or settings imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from .exceptions import LLMProviderNotFoundError

OPENAI_PROVIDER_ID = "openai"
ANTHROPIC_PROVIDER_ID = "anthropic"

OPENAI_GPT5_FAMILY = "gpt-5"
OPENAI_GPT4_FAMILY = "gpt-4"
OPENAI_GPT35_FAMILY = "gpt-3.5"

OPENAI_LEGACY_COMPATIBILITY_FAMILIES: tuple[str, ...] = (
    OPENAI_GPT35_FAMILY,
    OPENAI_GPT5_FAMILY,
    OPENAI_GPT4_FAMILY,
)


def _require_non_empty(value: str, *, label: str) -> str:
    """Return a string value after validating that it contains non-space text."""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} cannot be empty")
    return value


def normalize_provider_id(provider: str) -> str:
    """Normalize a provider id for lookup without changing request payload data."""
    return _require_non_empty(provider, label="provider").strip().lower()


def normalize_model_id(model: str) -> str:
    """Normalize a model id for lookup without changing request payload data."""
    return _require_non_empty(model, label="model").strip().lower()


@dataclass(frozen=True, slots=True)
class ProviderModelRef:
    """Canonical provider/model lookup identity used at the provider boundary."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        _require_non_empty(self.provider, label="provider")
        _require_non_empty(self.model, label="model")

    def normalized(self) -> "ProviderModelRef":
        """Return the normalized lookup identity for this provider/model pair."""
        return ProviderModelRef(
            provider=normalize_provider_id(self.provider),
            model=normalize_model_id(self.model),
        )

    def __str__(self) -> str:
        normalized = self.normalized()
        return f"{normalized.provider}/{normalized.model}"


@dataclass(frozen=True, slots=True)
class ProviderModelResolution:
    """Resolved lookup identity plus raw provider request model.

    ``lookup_ref`` is safe to normalize and use for profile lookup. The
    ``provider_request_model`` field preserves the original user/request model
    string for legacy model-only adapter construction.
    """

    lookup_ref: ProviderModelRef
    provider_request_model: str
    compatibility_family: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.provider_request_model, label="provider_request_model")


def get_openai_legacy_compatibility_family(model: str) -> str | None:
    """Return the approved OpenAI compatibility family for a model id, if any."""
    normalized_model = normalize_model_id(model)
    for family in sorted(OPENAI_LEGACY_COMPATIBILITY_FAMILIES, key=len, reverse=True):
        if normalized_model.startswith(family):
            return family
    return None


def is_openai_legacy_compatible_model(model: str) -> bool:
    """Return True when a model belongs to an approved legacy OpenAI family."""
    return get_openai_legacy_compatibility_family(model) is not None


def resolve_legacy_openai_model_ref(model: str) -> ProviderModelResolution:
    """Resolve a legacy model-only OpenAI request without rewriting the request model."""
    raw_model = _require_non_empty(model, label="model")
    normalized_model = normalize_model_id(raw_model)
    compatibility_family = get_openai_legacy_compatibility_family(normalized_model)
    if compatibility_family is None:
        raise LLMProviderNotFoundError(
            f"Model '{model}' is not in an approved legacy OpenAI model family",
            model=model,
            available_prefixes=list(OPENAI_LEGACY_COMPATIBILITY_FAMILIES),
        )
    return ProviderModelResolution(
        lookup_ref=ProviderModelRef(OPENAI_PROVIDER_ID, normalized_model),
        provider_request_model=raw_model,
        compatibility_family=compatibility_family,
    )


__all__ = [
    "ANTHROPIC_PROVIDER_ID",
    "OPENAI_PROVIDER_ID",
    "OPENAI_GPT5_FAMILY",
    "OPENAI_GPT4_FAMILY",
    "OPENAI_GPT35_FAMILY",
    "OPENAI_LEGACY_COMPATIBILITY_FAMILIES",
    "ProviderModelRef",
    "ProviderModelResolution",
    "get_openai_legacy_compatibility_family",
    "is_openai_legacy_compatible_model",
    "normalize_model_id",
    "normalize_provider_id",
    "resolve_legacy_openai_model_ref",
]
