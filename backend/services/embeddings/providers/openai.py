"""OpenAI embedding adapter for provider-neutral memory embeddings."""

from __future__ import annotations

from openai import AsyncOpenAI

from backend.services.embeddings.base import EmbeddingProfile
from backend.services.embeddings.profiles import build_openai_embedding_profile


class OpenAIEmbeddingProvider:
    """Async OpenAI embedding provider implementation."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None = None,
        dimensions: int | None = None,
        profile: EmbeddingProfile | None = None,
    ) -> None:
        self._profile = profile or build_openai_embedding_profile(
            model=model,
            dimensions=dimensions,
        )
        self.model = self._profile.ref.model
        self.dimensions = self._profile.dimensions
        self.api_key = (api_key or "").strip() or None
        self._client: AsyncOpenAI | None = None

    @property
    def profile(self) -> EmbeddingProfile:
        """Return the OpenAI embedding profile for this provider instance."""
        return self._profile

    def _get_client(self) -> AsyncOpenAI:
        """Build and cache the OpenAI async client on first use."""
        if self._client is None:
            resolved_key = self.api_key
            if not resolved_key:
                raise ValueError("OpenAI API key is required for memory embedding requests")
            self._client = AsyncOpenAI(api_key=resolved_key)
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Embed one text string into a float vector."""
        if not text or not text.strip():
            raise ValueError("text must not be empty")

        response = await self._get_client().embeddings.create(
            model=self.model,
            input=text,
            dimensions=self.dimensions,
        )
        return list(response.data[0].embedding)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple text strings, preserving input order."""
        if not texts:
            raise ValueError("texts must not be empty")

        response = await self._get_client().embeddings.create(
            model=self.model,
            input=texts,
            dimensions=self.dimensions,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]


__all__ = ["AsyncOpenAI", "OpenAIEmbeddingProvider"]
