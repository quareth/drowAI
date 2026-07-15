"""Artifact domain services package.

Consolidates artifact provenance persistence, query, memory facade, and
catalog label contracts under one namespace.
"""

from .catalog_labels import (
    build_artifact_catalog_label,
    build_artifact_catalog_label_expression,
)
from .memory_service import (
    ArtifactCatalogEntry,
    ArtifactCatalogPage,
    ArtifactMemoryScopeError,
    ArtifactMemoryService,
    ArtifactReadRequest,
    ArtifactReadResult,
    ArtifactSearchFilters,
)
from .provenance_query_service import (
    ArtifactProvenanceQueryService,
    ArtifactProvenanceScopeError,
)
from .provenance_service import (
    ArtifactProvenanceService,
    MAX_CONTENT_SIZE,
    resolve_workspace_root,
    validate_artifact_path,
)

__all__ = [
    "build_artifact_catalog_label",
    "build_artifact_catalog_label_expression",
    "ArtifactCatalogEntry",
    "ArtifactCatalogPage",
    "ArtifactMemoryService",
    "ArtifactMemoryScopeError",
    "ArtifactProvenanceQueryService",
    "ArtifactProvenanceScopeError",
    "ArtifactProvenanceService",
    "ArtifactReadRequest",
    "ArtifactReadResult",
    "ArtifactSearchFilters",
    "MAX_CONTENT_SIZE",
    "resolve_workspace_root",
    "validate_artifact_path",
]
