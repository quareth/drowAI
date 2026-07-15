"""Resolve supported embedding model profiles for semantic memory."""

from __future__ import annotations

import logging
import os

from agent.providers.llm.core.identity import (
    OPENAI_PROVIDER_ID,
    normalize_model_id,
    normalize_provider_id,
)

from backend.models.semantic_memory import SemanticMemory

from .base import EmbeddingModelRef, EmbeddingProfile

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
SUPPORTED_OPENAI_EMBEDDING_MODEL_IDS: tuple[str, ...] = (
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    "text-embedding-3-large",
)


def _read_db_embedding_dimensions(default: int = 1536) -> int:
    """Read semantic memory vector dimensions from ORM metadata."""
    try:
        vector_type = SemanticMemory.__table__.c.embedding.type
        dimensions = int(getattr(vector_type, "dim", default))
    except Exception:
        return default
    return dimensions if dimensions > 0 else default


DB_EMBEDDING_DIMENSIONS = _read_db_embedding_dimensions()


def read_dimensions_env(default: int = DB_EMBEDDING_DIMENSIONS) -> int:
    """Return embedding dimension from env, falling back to the DB vector shape."""
    raw = os.getenv("MEMORY_EMBEDDING_DIMENSIONS")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if value != default:
        logger.warning(
            "[MEMORY_EMBEDDING] MEMORY_EMBEDDING_DIMENSIONS=%s ignored; semantic_memories.embedding expects %s",
            value,
            default,
        )
        return default
    return value


def default_embedding_model() -> str:
    """Return the configured OpenAI memory embedding model."""
    return os.getenv("MEMORY_EMBEDDING_MODEL", DEFAULT_OPENAI_EMBEDDING_MODEL)


def build_openai_embedding_profile(
    *,
    model: str | None = None,
    dimensions: int | None = None,
) -> EmbeddingProfile:
    """Build the OpenAI embedding profile used by the Remote Runtime MVP."""

    resolved_model = normalize_model_id(
        (model or default_embedding_model()).strip() or DEFAULT_OPENAI_EMBEDDING_MODEL
    )
    if resolved_model not in SUPPORTED_OPENAI_EMBEDDING_MODEL_IDS:
        raise ValueError(f"Unsupported OpenAI embedding model: {resolved_model}")
    resolved_dimensions = int(dimensions or read_dimensions_env())
    return EmbeddingProfile(
        ref=EmbeddingModelRef(provider=OPENAI_PROVIDER_ID, model=resolved_model),
        dimensions=resolved_dimensions,
        vector_family=f"{OPENAI_PROVIDER_ID}:{resolved_model}:{resolved_dimensions}",
    )


def require_embedding_profile(provider: str, model: str) -> EmbeddingProfile:
    """Return an embedding profile or raise for unsupported providers."""

    normalized_provider = normalize_provider_id(provider)
    if normalized_provider != OPENAI_PROVIDER_ID:
        raise ValueError(f"Unsupported embedding provider: {normalized_provider}")
    return build_openai_embedding_profile(model=model)


__all__ = [
    "DB_EMBEDDING_DIMENSIONS",
    "DEFAULT_OPENAI_EMBEDDING_MODEL",
    "SUPPORTED_OPENAI_EMBEDDING_MODEL_IDS",
    "build_openai_embedding_profile",
    "default_embedding_model",
    "read_dimensions_env",
    "require_embedding_profile",
]
