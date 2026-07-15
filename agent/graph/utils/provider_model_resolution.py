"""Resolve provider/model pairs from graph runtime metadata.

This module keeps provider/model precedence paired so a model selected from one
runtime source is never combined with a provider selected from another source.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef


def resolve_graph_provider_model_ref(
    metadata: Mapping[str, Any],
    context: Optional[Any] = None,
    *,
    default_model: Optional[str] = None,
) -> Optional[ProviderModelRef]:
    """Resolve the conversation provider/model pair from metadata or context."""
    metadata_ref = _pair_from_mapping(metadata, model_key="model", provider_key="provider")
    if metadata_ref is not None:
        return metadata_ref

    runtime_ref = _pair_from_mapping(
        metadata,
        model_key="runtime_model",
        provider_key="runtime_provider",
    )
    if runtime_ref is not None:
        return runtime_ref

    context_ref = _pair_from_context(context, model_attr="model", provider_attr="provider")
    if context_ref is not None:
        return context_ref

    if _is_valid_string(default_model):
        return ProviderModelRef(OPENAI_PROVIDER_ID, str(default_model).strip())
    return None


def resolve_graph_reasoning_provider_model_ref(
    metadata: Mapping[str, Any],
    context: Optional[Any] = None,
) -> Optional[ProviderModelRef]:
    """Resolve the optional reasoning provider/model pair from metadata/context."""
    metadata_ref = _pair_from_mapping(
        metadata,
        model_key="reasoning_model",
        provider_key="reasoning_provider",
    )
    if metadata_ref is not None:
        return metadata_ref

    runtime_ref = _pair_from_mapping(
        metadata,
        model_key="runtime_reasoning_model",
        provider_key="runtime_reasoning_provider",
    )
    if runtime_ref is not None:
        return runtime_ref

    return _pair_from_context(
        context,
        model_attr="reasoning_model",
        provider_attr="reasoning_provider",
    )


def _pair_from_mapping(
    source: Mapping[str, Any],
    *,
    model_key: str,
    provider_key: str,
) -> Optional[ProviderModelRef]:
    model = source.get(model_key)
    if not _is_valid_string(model):
        return None
    provider = source.get(provider_key)
    return ProviderModelRef(
        _normalize_provider(provider),
        str(model).strip(),
    )


def _pair_from_context(
    context: Optional[Any],
    *,
    model_attr: str,
    provider_attr: str,
) -> Optional[ProviderModelRef]:
    if context is None:
        return None
    model = getattr(context, model_attr, None)
    if not _is_valid_string(model):
        return None
    provider = getattr(context, provider_attr, None)
    return ProviderModelRef(
        _normalize_provider(provider),
        str(model).strip(),
    )


def _normalize_provider(provider: Any) -> str:
    if _is_valid_string(provider):
        return str(provider).strip()
    return OPENAI_PROVIDER_ID


def _is_valid_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "resolve_graph_provider_model_ref",
    "resolve_graph_reasoning_provider_model_ref",
]
