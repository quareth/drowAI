"""PostgreSQL integration tests for pgvector-backed memory store behavior.

This module validates database-specific behavior that SQLite unit tests cannot
cover: cosine-based semantic deduplication, similarity ordering, and the HNSW
index presence for semantic memory retrieval.

Execution requirements:
- Set TEST_DATABASE_URL (preferred) or DATABASE_URL to a PostgreSQL database.
- Ensure the pgvector extension is available for CREATE EXTENSION vector.
- CI/staging should run this module with PostgreSQL; local runs may skip when
  environment requirements are not configured.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid as uuid_lib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.semantic_memory import SemanticMemory
from backend.services.embeddings.base import EmbeddingModelRef, EmbeddingProfile
from backend.services.memory.memory_models import MemoryCreateRequest, MemorySearchFilters, MemoryTier
from backend.services.memory.memory_store import MemoryStore

BASE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "c6d7e8f9a0b1_add_semantic_memories_table.py"
)
IDENTITY_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "f1e2d3c4b5a6_add_semantic_memory_embedding_identity.py"
)
_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"
TENANT_SCOPE_MIGRATION_PATH = next(_VERSIONS.glob("f0e1d2c3b4a5_*.py"))


def _postgres_test_url() -> str | None:
    """Return a PostgreSQL URL for integration tests, if configured."""
    from os import getenv

    candidate = getenv("TEST_DATABASE_URL") or getenv("DATABASE_URL")
    if not candidate:
        return None
    normalized = candidate.replace("postgres://", "postgresql://", 1)
    if "postgresql" not in normalized:
        return None
    return normalized


def _load_migration_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration_fn(module, connection, fn_name: str) -> None:
    context = MigrationContext.configure(connection)
    module.op = Operations(context)
    getattr(module, fn_name)()


def _build_postgres_session():
    """Create a PostgreSQL session and ensure required tables/extensions exist."""
    database_url = _postgres_test_url()
    if database_url is None:
        pytest.skip("PostgreSQL DATABASE_URL/TEST_DATABASE_URL is not configured")

    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.skip(f"PostgreSQL vector extension is unavailable: {exc}")

    Base.metadata.create_all(
        engine,
        tables=[User.__table__, Engagement.__table__],
    )
    with engine.begin() as conn:
        base_module = _load_migration_module(
            BASE_MIGRATION_PATH,
            "tenant_baseline_semantic_memories_migration",
        )
        _run_migration_fn(base_module, conn, "upgrade")
        identity_module = _load_migration_module(
            IDENTITY_MIGRATION_PATH,
            "semantic_memory_embedding_identity_migration",
        )
        _run_migration_fn(identity_module, conn, "upgrade")
        tenant_scope_module = _load_migration_module(
            TENANT_SCOPE_MIGRATION_PATH,
            "tenant_isolation_semantic_memory_tenant_scope_migration",
        )
        _run_migration_fn(tenant_scope_module, conn, "upgrade")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user(db, suffix: str) -> User:
    row = User(username=f"pgvector-{suffix}-{uuid_lib.uuid4()}", password="secret")
    db.add(row)
    db.flush()
    return row


def _seed_engagement(db, user_id: int, suffix: str) -> Engagement:
    row = Engagement(user_id=user_id, name=f"PGVector Engagement {suffix}", status="active")
    db.add(row)
    db.flush()
    return row


def _unit(dimensions: int, index: int) -> list[float]:
    vector = [0.0] * dimensions
    vector[index] = 1.0
    return vector


class _MappedEmbeddingService:
    """Deterministic embedding stub used for PostgreSQL integration tests."""

    def __init__(self, mapping: dict[str, list[float]], dimensions: int = 1536) -> None:
        self.mapping = mapping
        self.dimensions = dimensions

    async def embed(self, text_value: str) -> list[float]:
        if text_value not in self.mapping:
            raise KeyError(f"No embedding test vector configured for text: {text_value}")
        return self.mapping[text_value]


class _ProfiledMappedEmbeddingService(_MappedEmbeddingService):
    def __init__(
        self,
        mapping: dict[str, list[float]],
        *,
        provider: str = "openai",
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        vector_family: str | None = None,
    ) -> None:
        super().__init__(mapping, dimensions=dimensions)
        self.profile = EmbeddingProfile(
            ref=EmbeddingModelRef(provider=provider, model=model),
            dimensions=dimensions,
            vector_family=vector_family or f"{provider}:{model}:{dimensions}",
        )


@pytest.mark.asyncio
async def test_semantic_dedup_rejects_near_duplicate_same_scope() -> None:
    engine, db = _build_postgres_session()
    try:
        user = _seed_user(db, "semantic-dedup")
        engagement = _seed_engagement(db, user.id, "semantic-dedup")
        base = [1.0] * 1536
        near_duplicate = [0.99] * 1536
        store = MemoryStore(
            db=db,
            embedding_service=_MappedEmbeddingService(
                {
                    "first memory": base,
                    "near duplicate memory": near_duplicate,
                }
            ),
        )

        first = await store.store(
            MemoryCreateRequest(
                content="first memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=user.id,
                tenant_id=engagement.tenant_id,
                engagement_id=engagement.id,
            )
        )
        assert first is not None
        db.commit()

        duplicate = await store.store(
            MemoryCreateRequest(
                content="near duplicate memory",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=user.id,
                tenant_id=engagement.tenant_id,
                engagement_id=engagement.id,
            )
        )
        assert duplicate is None

        rows = db.execute(
            select(SemanticMemory).where(
                SemanticMemory.user_id == user.id,
                SemanticMemory.engagement_id == engagement.id,
            )
        ).scalars().all()
        assert len(rows) == 1
    finally:
        db.rollback()
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_semantic_dedup_does_not_compare_across_embedding_identities() -> None:
    engine, db = _build_postgres_session()
    try:
        user = _seed_user(db, "semantic-dedup-identity")
        base = [1.0] * 1536
        near_duplicate = [0.99] * 1536
        default_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledMappedEmbeddingService(
                {"first memory": base},
            ),
        )
        alternate_store = MemoryStore(
            db=db,
            embedding_service=_ProfiledMappedEmbeddingService(
                {"near duplicate memory": near_duplicate},
                model="text-embedding-3-small-v2",
                vector_family="openai:text-embedding-3-small-v2:1536",
            ),
        )

        first = await default_store.store(
            MemoryCreateRequest(
                content="first memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=user.id,
            )
        )
        assert first is not None
        db.commit()

        second = await alternate_store.store(
            MemoryCreateRequest(
                content="near duplicate memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=user.id,
            )
        )
        assert second is not None

        rows = db.execute(
            select(SemanticMemory).where(SemanticMemory.user_id == user.id)
        ).scalars().all()
        assert len(rows) == 2
        assert {row.embedding_vector_family for row in rows} == {
            "openai:text-embedding-3-small:1536",
            "openai:text-embedding-3-small-v2:1536",
        }
    finally:
        db.rollback()
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_retrieve_orders_by_cosine_similarity() -> None:
    engine, db = _build_postgres_session()
    try:
        user = _seed_user(db, "retrieve-order")
        e1 = [0.9] * 1536
        e2 = _unit(1536, 0)
        query = _unit(1536, 0)
        store = MemoryStore(
            db=db,
            embedding_service=_MappedEmbeddingService(
                {
                    "less similar memory": e1,
                    "most similar memory": e2,
                    "query text": query,
                }
            ),
        )

        first = await store.store(
            MemoryCreateRequest(
                content="less similar memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=user.id,
            )
        )
        second = await store.store(
            MemoryCreateRequest(
                content="most similar memory",
                memory_tier=MemoryTier.USER_PROFILE,
                user_id=user.id,
            )
        )
        assert first is not None and second is not None
        db.commit()

        results = await store.retrieve(
            query="query text",
            filters=MemorySearchFilters(user_id=user.id, memory_tier=MemoryTier.USER_PROFILE, max_results=5),
        )

        assert len(results) == 2
        assert results[0].content == "most similar memory"
        assert results[1].content == "less similar memory"
        assert results[0].similarity_score >= results[1].similarity_score
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def test_hnsw_index_exists_for_embedding_column() -> None:
    engine, db = _build_postgres_session()
    try:
        dialect_name = engine.dialect.name
        assert dialect_name == "postgresql"

        inspector = inspect(engine)
        assert "semantic_memories" in inspector.get_table_names()

        row = db.execute(
            text(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'semantic_memories'
                  AND indexname = 'ix_semantic_memories_embedding'
                """
            )
        ).mappings().first()
        assert row is not None
        assert "USING hnsw" in str(row["indexdef"])
        assert "vector_cosine_ops" in str(row["indexdef"])
    finally:
        db.close()
        engine.dispose()


def test_semantic_memories_id_column_is_uuid() -> None:
    engine, db = _build_postgres_session()
    try:
        inspector = inspect(engine)
        columns = {row["name"]: row for row in inspector.get_columns("semantic_memories")}
        assert "id" in columns
        assert str(columns["id"]["type"]).lower() == "uuid"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_cross_scope_same_content_stores_separately() -> None:
    engine, db = _build_postgres_session()
    try:
        user = _seed_user(db, "cross-scope")
        engagement_one = _seed_engagement(db, user.id, "one")
        engagement_two = _seed_engagement(db, user.id, "two")
        store = MemoryStore(
            db=db,
            embedding_service=_MappedEmbeddingService({"shared content": _unit(1536, 0)}),
        )

        first = await store.store(
            MemoryCreateRequest(
                content="shared content",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=user.id,
                tenant_id=engagement_one.tenant_id,
                engagement_id=engagement_one.id,
            )
        )
        second = await store.store(
            MemoryCreateRequest(
                content="shared content",
                memory_tier=MemoryTier.TASK_ENGAGEMENT,
                user_id=user.id,
                tenant_id=engagement_two.tenant_id,
                engagement_id=engagement_two.id,
            )
        )

        assert first is not None
        assert second is not None
        assert first.id != second.id

        rows = db.execute(
            select(SemanticMemory).where(
                SemanticMemory.user_id == user.id,
                SemanticMemory.memory_tier == MemoryTier.TASK_ENGAGEMENT.value,
            )
        ).scalars().all()
        assert len(rows) == 2
        assert len({row.scope_key for row in rows}) == 2
    finally:
        db.rollback()
        db.close()
        engine.dispose()
