"""Semantic memory services package.

Provides long-term memory persistence, embedding, retrieval, and
deduplication for user-profile and task-engagement memories.
"""

from typing import TYPE_CHECKING

from .memory_extraction_schemas import (
    ExtractionFact,
    ExtractionResult,
    GateClassifierOutput,
)
from .memory_models import (
    MemoryCreateRequest,
    MemorySearchFilters,
    MemorySearchResult,
    MemoryTier,
)
from .memory_store import MemoryStore

if TYPE_CHECKING:
    from .extraction_trigger import enqueue_memory_extraction
    from .memory_extraction import MemoryExtractionService

__all__ = [
    "enqueue_memory_extraction",
    "ExtractionFact",
    "ExtractionResult",
    "GateClassifierOutput",
    "MemoryExtractionService",
    "MemoryCreateRequest",
    "MemorySearchFilters",
    "MemorySearchResult",
    "MemoryStore",
    "MemoryTier",
]


def __getattr__(name: str):
    if name == "MemoryExtractionService":
        from .memory_extraction import MemoryExtractionService

        return MemoryExtractionService
    if name == "enqueue_memory_extraction":
        from .extraction_trigger import enqueue_memory_extraction

        return enqueue_memory_extraction
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
