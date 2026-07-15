"""Knowledge domain services package.

Consolidates ingestion, projection, query, evidence, identity, retention, and
replay services under one canonical namespace. Sub-packages: adapters,
identity, projection, candidate_extraction, query.
"""

from . import candidate_extraction, query
from .adapter_registry import KnowledgeAdapterRegistryService
from .archive_service import KnowledgeArchiveService
from .contracts import IngestionRunCreate, IngestionRunStatus, ObservationCreate
from .delete_guard_service import KnowledgeDeleteGuardService
from .evidence_storage_service import EvidenceStorageService
from .evidence_read_service import (
    KnowledgeEvidenceReadRequest,
    KnowledgeEvidenceReadResult,
    KnowledgeEvidenceReadService,
)
from .historical_backfill_service import KnowledgeHistoricalBackfillService
from .identity_service import IdentityMergeDecision, KnowledgeIdentityService, ResolvedIdentityObservation
from .ingestion_service import KnowledgeIngestionService
from .ingestion_trigger_service import enqueue_execution_ingestion, run_execution_ingestion_once
from .projection_service import KnowledgeProjectionService, ProjectionResult
from .query_service import KnowledgeQueryService
from .read_model_rebuild_service import KnowledgeReadModelRebuildService
from .replay_service import KnowledgeReplayService
from .replay_source_resolver import KnowledgeReplaySourceResolver
from .retention_service import KnowledgeRetentionService

__all__ = [
    "IngestionRunCreate",
    "IngestionRunStatus",
    "ObservationCreate",
    "KnowledgeAdapterRegistryService",
    "KnowledgeArchiveService",
    "KnowledgeDeleteGuardService",
    "EvidenceStorageService",
    "KnowledgeEvidenceReadRequest",
    "KnowledgeEvidenceReadResult",
    "KnowledgeEvidenceReadService",
    "KnowledgeHistoricalBackfillService",
    "IdentityMergeDecision",
    "KnowledgeIdentityService",
    "ResolvedIdentityObservation",
    "KnowledgeIngestionService",
    "enqueue_execution_ingestion",
    "run_execution_ingestion_once",
    "KnowledgeProjectionService",
    "ProjectionResult",
    "KnowledgeQueryService",
    "KnowledgeReadModelRebuildService",
    "KnowledgeReplayService",
    "KnowledgeReplaySourceResolver",
    "KnowledgeRetentionService",
    "candidate_extraction",
    "query",
]
