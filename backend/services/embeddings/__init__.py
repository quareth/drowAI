"""Provider-neutral embedding service contracts and factories.

This package owns embedding profiles, runtime selection contracts, and
provider adapter construction for semantic-memory embedding calls.
"""

from .base import EmbeddingModelRef, EmbeddingProfile, EmbeddingProvider
from .factory import EmbeddingProviderFactory
from .profiles import DEFAULT_OPENAI_EMBEDDING_MODEL, require_embedding_profile
from .selection_service import (
    DEFAULT_MEMORY_EXTRACTION_MODEL,
    DEFAULT_MEMORY_GATE_MODEL,
    EmbeddingRuntimeSelection,
    EmbeddingRuntimeSelectionService,
    MemoryLLMRuntimeSelection,
)

__all__ = [
    "DEFAULT_OPENAI_EMBEDDING_MODEL",
    "DEFAULT_MEMORY_EXTRACTION_MODEL",
    "DEFAULT_MEMORY_GATE_MODEL",
    "EmbeddingModelRef",
    "EmbeddingProfile",
    "EmbeddingProvider",
    "EmbeddingProviderFactory",
    "EmbeddingRuntimeSelection",
    "EmbeddingRuntimeSelectionService",
    "MemoryLLMRuntimeSelection",
    "require_embedding_profile",
]
