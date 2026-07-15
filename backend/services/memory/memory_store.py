"""Orchestrate semantic memory persistence, deduplication, and retrieval.

This module coordinates ORM writes/reads with embedding generation and scope
deduplication rules. It does not own transaction commits; callers control
commit/rollback boundaries.
"""

from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.core import Engagement, Task
from backend.models.semantic_memory import SemanticMemory
from backend.services.embeddings.base import (
    EmbeddingModelRef,
    EmbeddingProfile,
    EmbeddingProvider,
)
from backend.services.embeddings.profiles import DEFAULT_OPENAI_EMBEDDING_MODEL
from runtime_shared.durable_secret_masking import mask_durable_secrets

from .memory_models import (
    MemoryCreateRequest,
    MemorySearchFilters,
    MemorySearchResult,
    MemoryTier,
)

SEMANTIC_DEDUP_SIMILARITY_THRESHOLD = 0.92
DEFAULT_EMBEDDING_PROVIDER = "openai"
LEGACY_SCOPE_KEY_UNIQUE_CONSTRAINT = "ux_semantic_memories_scope_key"
SCOPE_KEY_UNIQUE_CONSTRAINT = "ux_semantic_memories_scope_key_identity"

def _compute_scope_key(
    memory_tier: MemoryTier,
    user_id: int,
    tenant_id: int | None,
    engagement_id: int | None,
    task_id: int | None,
    content_hash: str,
) -> str:
    """Build deterministic scope key used for exact deduplication."""
    if memory_tier == MemoryTier.USER_PROFILE:
        return f"up:{user_id}:{content_hash}"
    if tenant_id is None:
        raise ValueError("tenant_id is required for task_engagement memories")
    if engagement_id is not None:
        return f"te:{tenant_id}:eng:{engagement_id}:{content_hash}"
    if task_id is not None:
        return f"te:{tenant_id}:task:{task_id}:{content_hash}"
    raise ValueError("engagement_id or task_id is required for task_engagement memories")

class MemoryStore:
    """Service for create/retrieve/update/delete memory operations."""

    def __init__(self, db: Session, embedding_service: EmbeddingProvider) -> None:
        self.db = db
        self.embedding_service = embedding_service
        self.embedding_profile = _embedding_profile(self.embedding_service)
        expected_dimensions = int(getattr(SemanticMemory.__table__.c.embedding.type, "dim", 1536))
        service_dimensions = int(self.embedding_profile.dimensions)
        if service_dimensions <= 0:
            raise ValueError("embedding_service.dimensions must be a positive integer")
        if expected_dimensions > 0 and service_dimensions != expected_dimensions:
            raise ValueError(
                f"Embedding dimension mismatch: service={service_dimensions}, semantic_memories.embedding={expected_dimensions}"
            )

    @staticmethod
    def _is_scope_key_unique_violation(error: IntegrityError) -> bool:
        """Return True only for exact-dedup scope-key unique violations."""
        # PostgreSQL psycopg exposes structured diagnostics.
        constraint_name = (
            getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if getattr(error, "orig", None) is not None
            else None
        )
        if constraint_name in {
            SCOPE_KEY_UNIQUE_CONSTRAINT,
            LEGACY_SCOPE_KEY_UNIQUE_CONSTRAINT,
        }:
            return True

        # SQLite and generic DBAPI adapters usually expose only string messages.
        raw_message = str(getattr(error, "orig", error))
        return (
            "UNIQUE constraint failed: semantic_memories.scope_key" in raw_message
            or (
                "UNIQUE constraint failed: semantic_memories.scope_key,"
                in raw_message
                and "semantic_memories.embedding_provider" in raw_message
            )
            or SCOPE_KEY_UNIQUE_CONSTRAINT in raw_message
            or LEGACY_SCOPE_KEY_UNIQUE_CONSTRAINT in raw_message
        )

    @staticmethod
    def _parse_memory_id(memory_id: str) -> UUID | None:
        """Normalize incoming memory IDs to UUID for GUID-backed columns."""
        try:
            return UUID(str(memory_id))
        except (ValueError, TypeError):
            return None

    def _apply_scope_filters(self, stmt, filters: MemorySearchFilters):
        """Apply ownership and optional scope filters to a SQLAlchemy statement."""
        if filters.memory_tier == MemoryTier.USER_PROFILE:
            stmt = stmt.where(
                SemanticMemory.memory_tier == MemoryTier.USER_PROFILE.value,
                SemanticMemory.user_id == int(filters.user_id),
                SemanticMemory.tenant_id.is_(None),
            )
            return self._apply_embedding_identity_filters(stmt)

        if filters.memory_tier == MemoryTier.TASK_ENGAGEMENT:
            stmt = stmt.where(
                SemanticMemory.memory_tier == MemoryTier.TASK_ENGAGEMENT.value,
                SemanticMemory.tenant_id == int(filters.tenant_id),
            )
            if filters.engagement_id is not None:
                stmt = stmt.where(SemanticMemory.engagement_id == int(filters.engagement_id))
            if filters.task_id is not None:
                stmt = stmt.where(SemanticMemory.task_id == int(filters.task_id))
            return self._apply_embedding_identity_filters(stmt)

        if filters.user_id is not None:
            stmt = stmt.where(SemanticMemory.user_id == int(filters.user_id))
        if filters.tenant_id is not None:
            stmt = stmt.where(SemanticMemory.tenant_id == int(filters.tenant_id))
        if filters.engagement_id is not None:
            stmt = stmt.where(SemanticMemory.engagement_id == int(filters.engagement_id))
        if filters.task_id is not None:
            stmt = stmt.where(SemanticMemory.task_id == int(filters.task_id))
        return self._apply_embedding_identity_filters(stmt)

    def _resolve_request_tenant_id(self, request: MemoryCreateRequest) -> int | None:
        """Resolve and verify tenant ownership for task-engagement memory writes."""
        if request.memory_tier != MemoryTier.TASK_ENGAGEMENT:
            return None
        if request.tenant_id is None:
            raise ValueError("tenant_id is required for task_engagement memories")

        tenant_id = int(request.tenant_id)
        if request.engagement_id is not None:
            engagement_tenant_id = self.db.execute(
                select(Engagement.tenant_id).where(Engagement.id == int(request.engagement_id))
            ).scalar_one_or_none()
            if engagement_tenant_id is None:
                raise ValueError("engagement_id does not exist for task_engagement memories")
            if int(engagement_tenant_id) != tenant_id:
                raise ValueError("tenant_id does not match engagement tenant ownership")

        if request.task_id is not None:
            task_row = self.db.execute(
                select(Task.tenant_id, Task.engagement_id).where(Task.id == int(request.task_id))
            ).first()
            if task_row is None:
                raise ValueError("task_id does not exist for task_engagement memories")
            if int(task_row.tenant_id) != tenant_id:
                raise ValueError("tenant_id does not match task tenant ownership")
            if (
                request.engagement_id is not None
                and task_row.engagement_id is not None
                and int(task_row.engagement_id) != int(request.engagement_id)
            ):
                raise ValueError("task_id engagement_id does not match request engagement_id")

        return tenant_id

    def _apply_embedding_identity_filters(self, stmt):
        """Restrict reads to the active embedding identity."""
        profile = self.embedding_profile
        return stmt.where(
            SemanticMemory.embedding_provider == profile.ref.provider,
            SemanticMemory.embedding_model == profile.ref.model,
            SemanticMemory.embedding_dimensions == int(profile.dimensions),
            SemanticMemory.embedding_vector_family == profile.vector_family,
        )

    def _identity_columns(self) -> dict[str, object]:
        """Return ORM column values for the active embedding identity."""
        profile = self.embedding_profile
        return {
            "embedding_provider": profile.ref.provider,
            "embedding_model": profile.ref.model,
            "embedding_dimensions": int(profile.dimensions),
            "embedding_vector_family": profile.vector_family,
        }

    async def store(self, request: MemoryCreateRequest) -> MemorySearchResult | None:
        """Store one memory, returning None when deduplicated."""
        resolved_tenant_id = self._resolve_request_tenant_id(request)
        durable_content = str(
            mask_durable_secrets(
                request.content,
                source="semantic_memory.content",
            )
            or ""
        )
        durable_metadata = _mask_memory_metadata(request.metadata)
        content_hash = hashlib.sha256(durable_content.encode("utf-8")).hexdigest()
        scope_key = _compute_scope_key(
            request.memory_tier,
            request.user_id,
            resolved_tenant_id,
            request.engagement_id,
            request.task_id,
            content_hash,
        )

        provisional = SemanticMemory(
            user_id=request.user_id,
            tenant_id=resolved_tenant_id,
            engagement_id=request.engagement_id,
            task_id=request.task_id,
            memory_tier=request.memory_tier.value,
            content=durable_content,
            scope_key=scope_key,
            content_hash=content_hash,
            embedding=[0.0] * max(1, _embedding_dimensions(self.embedding_service)),
            **self._identity_columns(),
            source_type=request.source_type,
            conversation_id=request.conversation_id,
            source_turn_id=request.source_turn_id,
            memory_metadata=durable_metadata,
        )
        with self.db.begin_nested():
            self.db.add(provisional)
            try:
                self.db.flush()
            except IntegrityError as error:
                if self._is_scope_key_unique_violation(error):
                    return None
                raise

            try:
                embedding = await self.embedding_service.embed(durable_content)
            except Exception:
                self.db.delete(provisional)
                self.db.flush()
                raise

            semantic_duplicate = await self._find_semantic_duplicate(
                memory_id=str(provisional.id),
                user_id=request.user_id,
                tenant_id=resolved_tenant_id,
                memory_tier=request.memory_tier,
                engagement_id=request.engagement_id,
                task_id=request.task_id,
                embedding=embedding,
            )
            if semantic_duplicate is not None:
                self.db.delete(provisional)
                self.db.flush()
                return None

            provisional.embedding = embedding
            self.db.flush()
        return self._to_result(provisional, similarity_score=1.0)

    async def retrieve(
        self,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]:
        """Retrieve top semantic memories within one ownership scope."""
        query_embedding = await self.embedding_service.embed(query)
        return await self.retrieve_with_embedding(query_embedding, filters)

    async def retrieve_with_embedding(
        self,
        query_embedding: list[float],
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]:
        """Retrieve memories using a caller-provided embedding vector."""

        is_postgres = (
            self.db.bind is not None and self.db.bind.dialect.name == "postgresql"
        )
        if is_postgres:
            distance = SemanticMemory.embedding.cosine_distance(query_embedding)
            stmt = (
                select(SemanticMemory, distance.label("distance"))
                .order_by(distance.asc())
                .limit(int(filters.max_results))
            )
            stmt = self._apply_scope_filters(stmt, filters)

            rows = self.db.execute(stmt).all()
            memories: list[MemorySearchResult] = []
            now = datetime.now(timezone.utc)
            for row, distance_value in rows:
                row.last_accessed_at = now
                row.access_count = int(row.access_count or 0) + 1
                normalized_distance = 1.0 if distance_value is None else float(distance_value)
                similarity = max(0.0, 1.0 - normalized_distance)
                memories.append(self._to_result(row, similarity_score=similarity))
            self.db.flush()
            return memories

        stmt = (
            select(SemanticMemory)
            .order_by(SemanticMemory.created_at.desc())
            .limit(int(filters.max_results))
        )
        stmt = self._apply_scope_filters(stmt, filters)

        rows = self.db.execute(stmt).scalars().all()
        now = datetime.now(timezone.utc)
        results: list[MemorySearchResult] = []
        for row in rows:
            row.last_accessed_at = now
            row.access_count = int(row.access_count or 0) + 1
            results.append(self._to_result(row, similarity_score=0.0))
        self.db.flush()
        return results

    async def has_candidates(self, filters: MemorySearchFilters) -> bool:
        """Return True when at least one row exists for the provided scope filters."""
        stmt = select(SemanticMemory.id).limit(1)
        stmt = self._apply_scope_filters(stmt, filters)
        return self.db.execute(stmt).first() is not None

    async def get_candidate_tier_presence(
        self,
        *,
        user_id: int,
        tenant_id: int | None,
        engagement_id: int | None,
        task_id: int | None,
    ) -> dict[MemoryTier, bool]:
        """Return candidate presence for user-profile and engagement tiers."""
        user_profile_present = await self.has_candidates(
            MemorySearchFilters(
                user_id=int(user_id),
                memory_tier=MemoryTier.USER_PROFILE,
                max_results=1,
            )
        )

        task_engagement_present = False
        if tenant_id is not None and task_id is not None:
            task_engagement_present = await self.has_candidates(
                MemorySearchFilters(
                    tenant_id=int(tenant_id),
                    memory_tier=MemoryTier.TASK_ENGAGEMENT,
                    task_id=int(task_id),
                    max_results=1,
                )
            )
        if not task_engagement_present and tenant_id is not None and engagement_id is not None:
            task_engagement_present = await self.has_candidates(
                MemorySearchFilters(
                    tenant_id=int(tenant_id),
                    memory_tier=MemoryTier.TASK_ENGAGEMENT,
                    engagement_id=int(engagement_id),
                    max_results=1,
                )
            )

        return {
            MemoryTier.USER_PROFILE: user_profile_present,
            MemoryTier.TASK_ENGAGEMENT: task_engagement_present,
        }

    async def delete(self, memory_id: str, user_id: int) -> bool:
        """Delete one memory in user scope."""
        parsed_memory_id = self._parse_memory_id(memory_id)
        if parsed_memory_id is None:
            return False

        row = self.db.execute(
            select(SemanticMemory).where(
                SemanticMemory.id == parsed_memory_id,
                SemanticMemory.user_id == int(user_id),
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self.db.delete(row)
        self.db.flush()
        return True

    async def update(
        self,
        memory_id: str,
        user_id: int,
        content: str,
    ) -> MemorySearchResult | None:
        """Update content and embedding for one memory in user scope."""
        parsed_memory_id = self._parse_memory_id(memory_id)
        if parsed_memory_id is None:
            return None

        stmt = select(SemanticMemory).where(
            SemanticMemory.id == parsed_memory_id,
            SemanticMemory.user_id == int(user_id),
        )
        stmt = self._apply_embedding_identity_filters(stmt)
        row = self.db.execute(stmt).scalar_one_or_none()
        if row is None:
            return None

        masked_content = str(
            mask_durable_secrets(
                content,
                source="semantic_memory.content",
            )
            or ""
        )
        new_hash = hashlib.sha256(masked_content.encode("utf-8")).hexdigest()
        tier = MemoryTier(str(row.memory_tier))
        new_scope_key = _compute_scope_key(
            tier,
            int(row.user_id),
            int(row.tenant_id) if row.tenant_id is not None else None,
            int(row.engagement_id) if row.engagement_id is not None else None,
            int(row.task_id) if row.task_id is not None else None,
            new_hash,
        )
        new_embedding = await self.embedding_service.embed(masked_content)

        with self.db.begin_nested():
            row.content = masked_content
            row.content_hash = new_hash
            row.scope_key = new_scope_key
            row.embedding = new_embedding
            for column_name, value in self._identity_columns().items():
                setattr(row, column_name, value)
            try:
                self.db.flush()
            except IntegrityError as error:
                if self._is_scope_key_unique_violation(error):
                    return None
                raise
        return self._to_result(row, similarity_score=1.0)

    async def _find_semantic_duplicate(
        self,
        *,
        memory_id: str,
        user_id: int,
        tenant_id: int | None,
        memory_tier: MemoryTier,
        engagement_id: int | None,
        task_id: int | None,
        embedding: list[float],
    ) -> SemanticMemory | None:
        """Return matching semantic duplicate in the same scope when present."""
        is_postgres = (
            self.db.bind is not None and self.db.bind.dialect.name == "postgresql"
        )
        if not is_postgres:
            return None

        parsed_memory_id = self._parse_memory_id(memory_id)
        if parsed_memory_id is None:
            return None

        distance = SemanticMemory.embedding.cosine_distance(embedding)
        stmt = select(SemanticMemory, distance.label("distance")).where(SemanticMemory.id != parsed_memory_id)
        if memory_tier == MemoryTier.USER_PROFILE:
            stmt = stmt.where(
                SemanticMemory.user_id == int(user_id),
                SemanticMemory.memory_tier == MemoryTier.USER_PROFILE.value,
                SemanticMemory.tenant_id.is_(None),
            )
        else:
            if tenant_id is None:
                raise ValueError("tenant_id is required for task_engagement semantic dedupe")
            stmt = stmt.where(
                SemanticMemory.tenant_id == int(tenant_id),
                SemanticMemory.memory_tier == MemoryTier.TASK_ENGAGEMENT.value,
            )
            if engagement_id is not None:
                stmt = stmt.where(SemanticMemory.engagement_id == int(engagement_id))
            if task_id is not None:
                stmt = stmt.where(SemanticMemory.task_id == int(task_id))
        stmt = self._apply_embedding_identity_filters(stmt)
        stmt = stmt.order_by(distance.asc()).limit(1)

        match = self.db.execute(stmt).first()
        if match is None:
            return None
        row, distance_value = match
        normalized_distance = 1.0 if distance_value is None else float(distance_value)
        similarity = max(0.0, 1.0 - normalized_distance)
        if similarity > SEMANTIC_DEDUP_SIMILARITY_THRESHOLD:
            return row
        return None

    @staticmethod
    def _to_result(row: SemanticMemory, similarity_score: float) -> MemorySearchResult:
        """Map one ORM row into API-level result contract."""
        return MemorySearchResult(
            id=str(row.id),
            content=str(row.content),
            memory_tier=MemoryTier(str(row.memory_tier)),
            similarity_score=float(similarity_score),
            created_at=row.created_at,
            metadata=dict(row.memory_metadata or {}) or None,
            embedding_provider=str(row.embedding_provider),
            embedding_model=str(row.embedding_model),
            embedding_dimensions=int(row.embedding_dimensions),
            embedding_vector_family=str(row.embedding_vector_family),
        )


def _embedding_profile(embedding_service: EmbeddingProvider) -> EmbeddingProfile:
    """Return provider-declared embedding identity with a legacy OpenAI fallback."""
    profile = getattr(embedding_service, "profile", None)
    if profile is not None:
        return profile

    dimensions = _embedding_dimensions(embedding_service)
    return EmbeddingProfile(
        ref=EmbeddingModelRef(
            provider=DEFAULT_EMBEDDING_PROVIDER,
            model=DEFAULT_OPENAI_EMBEDDING_MODEL,
        ),
        dimensions=dimensions,
        vector_family=(
            f"{DEFAULT_EMBEDDING_PROVIDER}:"
            f"{DEFAULT_OPENAI_EMBEDDING_MODEL}:"
            f"{dimensions}"
        ),
    )


def _mask_memory_metadata(metadata: dict | None) -> dict | None:
    """Mask reusable secrets in semantic-memory metadata before persistence."""
    if metadata is None:
        return None
    masked = mask_durable_secrets(metadata, source="semantic_memory.metadata")
    return dict(masked) if isinstance(masked, dict) else None


def _embedding_dimensions(embedding_service: EmbeddingProvider) -> int:
    """Return provider-declared embedding dimensions with legacy fallback."""
    profile = getattr(embedding_service, "profile", None)
    if profile is not None:
        dimensions = getattr(profile, "dimensions", None)
        if dimensions is not None:
            return int(dimensions)
    return int(getattr(embedding_service, "dimensions", 0))
