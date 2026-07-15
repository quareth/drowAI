"""Exercise semantic memory store behavior required by tenant baseline acceptance criteria."""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.semantic_memory import SemanticMemory
from backend.services.embeddings.base import EmbeddingModelRef, EmbeddingProfile
from backend.services.memory.memory_models import MemoryCreateRequest, MemorySearchFilters, MemoryTier
from backend.services.memory.memory_store import MemoryStore


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, Engagement.__table__, Task.__table__, SemanticMemory.__table__],
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user(db, username: str) -> User:
    row = User(username=username, password="secret")
    db.add(row)
    db.flush()
    return row


def _seed_engagement(db, user_id: int, name: str, *, tenant_id: int = 1) -> Engagement:
    row = Engagement(user_id=user_id, tenant_id=tenant_id, name=name, status="active")
    db.add(row)
    db.flush()
    return row


def _seed_task(db, user_id: int, name: str, *, tenant_id: int = 1, engagement_id: int | None = None) -> Task:
    row = Task(user_id=user_id, tenant_id=tenant_id, engagement_id=engagement_id, name=name)
    db.add(row)
    db.flush()
    return row


def _vector(seed: float, dimensions: int = 1536) -> list[float]:
    return [seed] * dimensions


class _StubEmbeddingService:
    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        return _vector(float(len(text)), self.dimensions)


class _FailingEmbeddingService:
    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    async def embed(self, _text: str) -> list[float]:
        raise RuntimeError("embedding provider unavailable")


class _CountingEmbeddingService(_StubEmbeddingService):
    def __init__(self, dimensions: int = 1536) -> None:
        super().__init__(dimensions=dimensions)
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return await super().embed(text)


class _RecordingEmbeddingService(_StubEmbeddingService):
    def __init__(self, dimensions: int = 1536) -> None:
        super().__init__(dimensions=dimensions)
        self.inputs: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.inputs.append(text)
        return await super().embed(text)


class _ProfiledEmbeddingProvider(_StubEmbeddingService):
    def __init__(
        self,
        *,
        provider: str = "openai",
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        vector_family: str | None = None,
    ) -> None:
        super().__init__(dimensions=dimensions)
        self.profile = EmbeddingProfile(
            ref=EmbeddingModelRef(provider=provider, model=model),
            dimensions=dimensions,
            vector_family=vector_family or f"{provider}:{model}:{dimensions}",
        )


def test_store_init_raises_on_embedding_dimension_mismatch() -> None:
    engine, db = _build_session()
    try:
        with pytest.raises(ValueError, match="Embedding dimension mismatch"):
            MemoryStore(db=db, embedding_service=_StubEmbeddingService(dimensions=8))
    finally:
        db.close()
        engine.dispose()


def test_store_accepts_profiled_embedding_provider() -> None:
    engine, db = _build_session()
    try:
        store = MemoryStore(db=db, embedding_service=_ProfiledEmbeddingProvider())
        assert store.embedding_service.profile.ref.provider == "openai"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_persists_with_correct_fields() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "memory-owner")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        request = MemoryCreateRequest(
            content="critical host detail",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=user.id,
            metadata={"origin": "test"},
        )

        result = await store.store(request)
        assert result is not None

        row = db.execute(select(SemanticMemory)).scalar_one()
        assert row.memory_tier == MemoryTier.USER_PROFILE.value
        assert row.content_hash == hashlib.sha256(request.content.encode("utf-8")).hexdigest()
        assert row.scope_key.startswith(f"up:{user.id}:")
        assert list(row.embedding) == _vector(float(len(request.content)))
        assert row.memory_metadata == {"origin": "test"}
        assert row.embedding_provider == "openai"
        assert row.embedding_model == "text-embedding-3-small"
        assert row.embedding_dimensions == 1536
        assert row.embedding_vector_family == "openai:text-embedding-3-small:1536"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_masks_memory_content_metadata_hash_and_embedding_input() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "memory-mask-owner")
        raw_secret = "memory-secret-12345"
        embedding_service = _RecordingEmbeddingService()
        store = MemoryStore(db=db, embedding_service=embedding_service)
        request = MemoryCreateRequest(
            content=f"The captured login password is password={raw_secret}.",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=user.id,
            metadata={
                "origin": "test",
                "authorization": f"Bearer {raw_secret}",
            },
        )

        result = await store.store(request)
        assert result is not None

        row = db.execute(select(SemanticMemory)).scalar_one()
        assert raw_secret not in row.content
        assert "<DURABLE_SECRET_MASK:" in row.content
        assert row.content_hash == hashlib.sha256(row.content.encode("utf-8")).hexdigest()
        assert embedding_service.inputs == [row.content]
        assert raw_secret not in repr(row.memory_metadata)
        assert row.memory_metadata["origin"] == "test"
        assert "<DURABLE_SECRET_MASK:" in row.memory_metadata["authorization"]
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_exact_dedup_returns_none_for_same_scope_key() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "memory-exact-dedup")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        request = MemoryCreateRequest(
            content="same content",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=user.id,
        )

        first = await store.store(request)
        assert first is not None
        db.commit()

        second = await store.store(request)
        assert second is None

        rows = db.execute(select(SemanticMemory)).scalars().all()
        assert len(rows) == 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_exact_dedup_allows_same_scope_key_for_new_embedding_identity() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "memory-exact-dedup-identity")
        default_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledEmbeddingProvider(),
        )
        alternate_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledEmbeddingProvider(
                model="text-embedding-3-small-v2",
                vector_family="openai:text-embedding-3-small-v2:1536",
            ),
        )
        request = MemoryCreateRequest(
            content="same content, different vector family",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=user.id,
        )

        first = await default_store.store(request)
        assert first is not None
        db.commit()

        second = await alternate_store.store(request)
        assert second is not None

        rows = db.execute(select(SemanticMemory)).scalars().all()
        assert len(rows) == 2
        assert len({row.scope_key for row in rows}) == 1
        assert {row.embedding_vector_family for row in rows} == {
            "openai:text-embedding-3-small:1536",
            "openai:text-embedding-3-small-v2:1536",
        }
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_returns_none_when_semantic_duplicate_is_detected() -> None:
    """Semantic duplicate path is forced by patching helper in SQLite unit tests."""

    engine, db = _build_session()
    try:
        user = _seed_user(db, "memory-semantic-dedup")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        async def _fake_semantic_duplicate(**_kwargs):  # noqa: ANN003 - test seam
            return object()

        store._find_semantic_duplicate = _fake_semantic_duplicate  # type: ignore[method-assign]
        request = MemoryCreateRequest(
            content="near duplicate",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=user.id,
        )

        result = await store.store(request)
        assert result is None

        rows = db.execute(select(SemanticMemory)).scalars().all()
        assert rows == []
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_different_scopes_are_stored_separately() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "memory-scope-separation")
        engagement_a = _seed_engagement(db, user_id=user.id, name="Engagement A")
        engagement_b = _seed_engagement(db, user_id=user.id, name="Engagement B")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        content = "shared value"
        first = await store.store(
            MemoryCreateRequest(
                content=content,
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=user.id,
                tenant_id=engagement_a.tenant_id,
                engagement_id=engagement_a.id,
            )
        )
        second = await store.store(
            MemoryCreateRequest(
                content=content,
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=user.id,
                tenant_id=engagement_b.tenant_id,
                engagement_id=engagement_b.id,
            )
        )

        assert first is not None
        assert second is not None
        keys = [row.scope_key for row in db.execute(select(SemanticMemory)).scalars().all()]
        assert len(keys) == 2
        assert len(set(keys)) == 2
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_filters_by_user_tier_and_updates_access_metadata() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-retrieve-owner")
        foreign = _seed_user(db, "memory-retrieve-foreign")
        engagement = _seed_engagement(db, user_id=owner.id, name="Engagement")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        await store.store(
            MemoryCreateRequest(
                content="owner profile",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        await store.store(
            MemoryCreateRequest(
                content="owner engagement",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=owner.id,
                tenant_id=engagement.tenant_id,
                engagement_id=engagement.id,
            )
        )
        await store.store(
            MemoryCreateRequest(
                content="foreign profile",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=foreign.id,
            )
        )
        db.commit()

        filters = MemorySearchFilters(
            user_id=owner.id,
            memory_tier=MemoryTier.USER_PROFILE,
            max_results=5,
        )
        results = await store.retrieve(query="owner", filters=filters)

        assert len(results) == 1
        assert results[0].content == "owner profile"
        touched = db.execute(select(SemanticMemory).where(SemanticMemory.id == results[0].id)).scalar_one()
        assert touched.access_count == 1
        assert isinstance(touched.last_accessed_at, datetime)
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_task_engagement_filters_by_tenant_and_engagement() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-tenant-scope-owner")
        foreign = _seed_user(db, "memory-tenant-scope-foreign")
        owner_engagement = _seed_engagement(db, user_id=owner.id, name="Tenant A", tenant_id=1)
        foreign_engagement = _seed_engagement(db, user_id=foreign.id, name="Tenant B", tenant_id=2)
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        await store.store(
            MemoryCreateRequest(
                content="tenant-a memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=owner.id,
                tenant_id=owner_engagement.tenant_id,
                engagement_id=owner_engagement.id,
            )
        )
        await store.store(
            MemoryCreateRequest(
                content="tenant-b memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=foreign.id,
                tenant_id=foreign_engagement.tenant_id,
                engagement_id=foreign_engagement.id,
            )
        )
        db.commit()

        filters = MemorySearchFilters(
            tenant_id=owner_engagement.tenant_id,
            memory_tier=MemoryTier.TASK_ENGAGEMENT,
            engagement_id=owner_engagement.id,
            max_results=5,
        )
        results = await store.retrieve(query="tenant", filters=filters)

        assert len(results) == 1
        assert results[0].content == "tenant-a memory"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_task_engagement_filters_by_task_scope() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-task-scope-owner")
        engagement = _seed_engagement(db, user_id=owner.id, name="Task Scope Engagement", tenant_id=1)
        task_a = _seed_task(
            db,
            user_id=owner.id,
            name="Task A",
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
        )
        task_b = _seed_task(
            db,
            user_id=owner.id,
            name="Task B",
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
        )
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        await store.store(
            MemoryCreateRequest(
                content="task-a memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=owner.id,
                tenant_id=engagement.tenant_id,
                task_id=task_a.id,
            )
        )
        await store.store(
            MemoryCreateRequest(
                content="task-b memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=owner.id,
                tenant_id=engagement.tenant_id,
                task_id=task_b.id,
            )
        )
        db.commit()

        filters = MemorySearchFilters(
            tenant_id=engagement.tenant_id,
            memory_tier=MemoryTier.TASK_ENGAGEMENT,
            task_id=task_a.id,
            max_results=5,
        )
        results = await store.retrieve(query="task", filters=filters)

        assert len(results) == 1
        assert results[0].content == "task-a memory"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_filters_by_embedding_identity() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-retrieve-identity-owner")
        default_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledEmbeddingProvider(),
        )
        alternate_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledEmbeddingProvider(
                model="text-embedding-3-small-v2",
                vector_family="openai:text-embedding-3-small-v2:1536",
            ),
        )

        await default_store.store(
            MemoryCreateRequest(
                content="default vector family",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        await alternate_store.store(
            MemoryCreateRequest(
                content="alternate vector family",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        db.commit()

        filters = MemorySearchFilters(
            user_id=owner.id,
            memory_tier=MemoryTier.USER_PROFILE,
            max_results=5,
        )
        default_results = await default_store.retrieve(query="family", filters=filters)
        alternate_results = await alternate_store.retrieve(query="family", filters=filters)

        assert [result.content for result in default_results] == ["default vector family"]
        assert [result.content for result in alternate_results] == [
            "alternate vector family"
        ]
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_empty_returns_empty_list() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-empty-retrieve")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        filters = MemorySearchFilters(
            user_id=owner.id,
            memory_tier=MemoryTier.USER_PROFILE,
            max_results=5,
        )

        results = await store.retrieve(query="nothing", filters=filters)
        assert results == []
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_respects_max_results() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-max-results-owner")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        await store.store(
            MemoryCreateRequest(
                content="first memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        await store.store(
            MemoryCreateRequest(
                content="second memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        db.commit()

        filters = MemorySearchFilters(
            user_id=owner.id,
            memory_tier=MemoryTier.USER_PROFILE,
            max_results=1,
        )
        results = await store.retrieve(query="memory", filters=filters)

        assert len(results) == 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_delete_enforces_user_ownership() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-delete-owner")
        foreign = _seed_user(db, "memory-delete-foreign")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        created = await store.store(
            MemoryCreateRequest(
                content="delete me",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        assert created is not None
        db.commit()

        denied = await store.delete(memory_id=created.id, user_id=foreign.id)
        allowed = await store.delete(memory_id=created.id, user_id=owner.id)

        assert denied is False
        assert allowed is True
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_update_reembeds_and_recomputes_scope_key() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-update-owner")
        embedding_service = _RecordingEmbeddingService()
        store = MemoryStore(db=db, embedding_service=embedding_service)

        created = await store.store(
            MemoryCreateRequest(
                content="initial content",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        assert created is not None
        db.commit()

        updated = await store.update(
            memory_id=created.id,
            user_id=owner.id,
            content="updated content for memory",
        )
        assert updated is not None

        row = db.execute(select(SemanticMemory).where(SemanticMemory.id == created.id)).scalar_one()
        assert row.content == "updated content for memory"
        assert row.content_hash == hashlib.sha256("updated content for memory".encode("utf-8")).hexdigest()
        assert row.scope_key.startswith(f"up:{owner.id}:")
        assert list(row.embedding) == _vector(float(len("updated content for memory")))
        assert embedding_service.inputs[-1] == row.content
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_update_masks_memory_content_hash_and_embedding_input() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-update-mask-owner")
        raw_secret = "memory-update-secret-12345"
        embedding_service = _RecordingEmbeddingService()
        store = MemoryStore(db=db, embedding_service=embedding_service)

        created = await store.store(
            MemoryCreateRequest(
                content="initial update mask content",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        assert created is not None
        db.commit()

        updated = await store.update(
            memory_id=created.id,
            user_id=owner.id,
            content=f"Rotated credential is password={raw_secret}.",
        )
        assert updated is not None

        row = db.execute(select(SemanticMemory).where(SemanticMemory.id == created.id)).scalar_one()
        assert raw_secret not in row.content
        assert "<DURABLE_SECRET_MASK:" in row.content
        assert row.content_hash == hashlib.sha256(row.content.encode("utf-8")).hexdigest()
        assert row.scope_key.endswith(row.content_hash)
        assert embedding_service.inputs[-1] == row.content
        assert raw_secret not in embedding_service.inputs[-1]
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_update_does_not_move_memory_between_embedding_identities() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-update-identity-owner")
        default_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledEmbeddingProvider(),
        )
        alternate_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledEmbeddingProvider(
                model="text-embedding-3-small-v2",
                vector_family="openai:text-embedding-3-small-v2:1536",
            ),
        )

        created = await default_store.store(
            MemoryCreateRequest(
                content="default family content",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        assert created is not None
        db.commit()

        updated = await alternate_store.update(
            memory_id=created.id,
            user_id=owner.id,
            content="alternate family update",
        )
        assert updated is None

        row = db.execute(select(SemanticMemory).where(SemanticMemory.id == created.id)).scalar_one()
        assert row.content == "default family content"
        assert row.embedding_vector_family == "openai:text-embedding-3-small:1536"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_does_not_call_commit() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-no-commit")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        commit_called = False

        def _track_commit() -> None:
            nonlocal commit_called
            commit_called = True

        db.commit = _track_commit  # type: ignore[method-assign]
        result = await store.store(
            MemoryCreateRequest(
                content="flush-only transaction",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        assert result is not None
        assert commit_called is False
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_embedding_failure_removes_provisional_row() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-embed-failure-owner")
        store = MemoryStore(db=db, embedding_service=_FailingEmbeddingService())

        with pytest.raises(RuntimeError, match="embedding provider unavailable"):
            await store.store(
                MemoryCreateRequest(
                    content="will fail embedding",
                    memory_tier=MemoryTier.USER_PROFILE,
                    user_id=owner.id,
                )
            )

        db.commit()
        rows = db.execute(select(SemanticMemory)).scalars().all()
        assert rows == []
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_exact_dedup_does_not_rollback_unrelated_changes() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-dedup-rollback-owner")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())
        request = MemoryCreateRequest(
            content="dedup content",
            memory_tier=MemoryTier.USER_PROFILE,
            user_id=owner.id,
        )

        first = await store.store(request)
        assert first is not None
        db.commit()

        unrelated = User(username="memory-dedup-unrelated", password="secret")
        db.add(unrelated)
        db.flush()

        duplicate = await store.store(request)
        assert duplicate is None

        db.commit()
        usernames = [row.username for row in db.execute(select(User)).scalars().all()]
        assert "memory-dedup-unrelated" in usernames
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_update_exact_dedup_does_not_rollback_unrelated_changes() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-update-dedup-owner")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        first = await store.store(
            MemoryCreateRequest(
                content="alpha memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        second = await store.store(
            MemoryCreateRequest(
                content="beta memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        assert first is not None and second is not None
        db.commit()

        unrelated = User(username="memory-update-unrelated", password="secret")
        db.add(unrelated)
        db.flush()

        updated = await store.update(
            memory_id=second.id,
            user_id=owner.id,
            content="alpha memory",
        )
        assert updated is None

        db.commit()
        usernames = [row.username for row in db.execute(select(User)).scalars().all()]
        assert "memory-update-unrelated" in usernames

        row = db.execute(select(SemanticMemory).where(SemanticMemory.id == second.id)).scalar_one()
        assert row.content == "beta memory"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_store_reraises_non_dedup_integrity_errors() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-nondedup-integrity-owner")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        original_flush = db.flush
        state = {"raised": False}

        def _flush_once_with_non_dedup_error() -> None:
            if not state["raised"]:
                state["raised"] = True
                raise IntegrityError(
                    statement="INSERT INTO semantic_memories (...) VALUES (...)",
                    params={},
                    orig=Exception("FOREIGN KEY constraint failed"),
                )
            original_flush()

        db.flush = _flush_once_with_non_dedup_error  # type: ignore[method-assign]

        with pytest.raises(IntegrityError):
            await store.store(
                MemoryCreateRequest(
                    content="integrity error path",
                    memory_tier=MemoryTier.USER_PROFILE,
                    user_id=owner.id,
                )
            )
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_with_embedding_does_not_call_embed_again() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-precomputed-embedding-owner")
        embedding_service = _CountingEmbeddingService()
        store = MemoryStore(db=db, embedding_service=embedding_service)

        await store.store(
            MemoryCreateRequest(
                content="first memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        db.commit()
        assert embedding_service.calls == 1

        query_embedding = _vector(1.0)
        results = await store.retrieve_with_embedding(
            query_embedding=query_embedding,
            filters=MemorySearchFilters(
                user_id=owner.id,
                memory_tier=MemoryTier.USER_PROFILE,
                max_results=5,
            ),
        )

        assert len(results) == 1
        assert embedding_service.calls == 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_get_candidate_tier_presence_checks_user_and_engagement_scope() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "memory-tier-presence-owner")
        engagement = _seed_engagement(db, user_id=owner.id, name="Tier Presence Engagement")
        store = MemoryStore(db=db, embedding_service=_StubEmbeddingService())

        await store.store(
            MemoryCreateRequest(
                content="user profile memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=owner.id,
            )
        )
        await store.store(
            MemoryCreateRequest(
                content="engagement memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=owner.id,
                tenant_id=engagement.tenant_id,
                engagement_id=engagement.id,
            )
        )
        db.commit()

        with_engagement = await store.get_candidate_tier_presence(
            user_id=owner.id,
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            task_id=None,
        )
        without_engagement = await store.get_candidate_tier_presence(
            user_id=owner.id,
            tenant_id=engagement.tenant_id,
            engagement_id=None,
            task_id=None,
        )

        assert with_engagement == {
            MemoryTier.USER_PROFILE: True,
            MemoryTier.TASK_ENGAGEMENT: True,
        }
        assert without_engagement == {
            MemoryTier.USER_PROFILE: True,
            MemoryTier.TASK_ENGAGEMENT: False,
        }
    finally:
        db.close()
        engine.dispose()
