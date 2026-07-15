"""Construct embedding provider adapters from runtime selections."""

from __future__ import annotations

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, normalize_provider_id

from .base import EmbeddingProvider
from .selection_service import EmbeddingRuntimeSelection
from .providers.openai import OpenAIEmbeddingProvider


class EmbeddingProviderFactory:
    """Factory for concrete embedding providers."""

    def create(
        self,
        selection: EmbeddingRuntimeSelection,
        *,
        api_key: str,
    ) -> EmbeddingProvider:
        """Create an embedding provider for the selected provider/model."""

        provider = normalize_provider_id(selection.provider)
        if provider == OPENAI_PROVIDER_ID:
            return OpenAIEmbeddingProvider(
                api_key=api_key,
                model=selection.model,
                dimensions=selection.dimensions,
            )
        raise ValueError(f"Unsupported embedding provider: {provider}")


__all__ = ["EmbeddingProviderFactory"]
