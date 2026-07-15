"""Define provider-neutral embedding contracts for memory services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmbeddingModelRef:
    """Canonical embedding provider/model identity."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Embedding model metadata needed by storage and retrieval policy."""

    ref: EmbeddingModelRef
    dimensions: int
    vector_family: str
    listable: bool = True


class EmbeddingProvider(Protocol):
    """Protocol implemented by concrete embedding providers."""

    @property
    def profile(self) -> EmbeddingProfile:
        """Return embedding identity and vector shape metadata."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Embed one text string into a vector."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple text strings, preserving input order."""
        ...


__all__ = ["EmbeddingModelRef", "EmbeddingProfile", "EmbeddingProvider"]
