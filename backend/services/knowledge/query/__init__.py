"""Internal knowledge query package.

This package provides contracts, selectors, mappers, and orchestration engine
used by the compatibility facade in `knowledge_query_service.py`."""

from .contracts import (
    AssetSort,
    AssetsFilters,
    DEFAULT_LIMIT,
    EngagementListFilters,
    EvidenceFilters,
    EvidenceSort,
    FindingsFilters,
    FindingSort,
    MAX_LIMIT,
    PaginatedResult,
    PaginationParams,
    WebSurfacePathsFilters,
    normalize_optional_bool,
)
from .engine import KnowledgeQueryEngine

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "FindingSort",
    "AssetSort",
    "EvidenceSort",
    "PaginationParams",
    "PaginatedResult",
    "EngagementListFilters",
    "FindingsFilters",
    "AssetsFilters",
    "EvidenceFilters",
    "WebSurfacePathsFilters",
    "KnowledgeQueryEngine",
    "normalize_optional_bool",
]
