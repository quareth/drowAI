"""Deployment baseline tests for memory LLM selection independence."""

from __future__ import annotations

from uuid import uuid4

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import User
from backend.models.llm import UserEmbeddingSelection, UserMemoryLLMSelection
from backend.services.embeddings.profiles import (
    DB_EMBEDDING_DIMENSIONS,
    DEFAULT_OPENAI_EMBEDDING_MODEL,
)
from backend.services.embeddings.selection_service import (
    DEFAULT_MEMORY_EXTRACTION_MODEL,
    DEFAULT_MEMORY_GATE_MODEL,
    EmbeddingRuntimeSelectionService,
)
from backend.services.llm_provider import LLMCredentialRef


def _create_user(db, username_prefix: str = "deployment-memory-llm") -> User:
    user = User(
        username=f"{username_prefix}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_memory_llm_selection_table_remains_separate_from_embedding_identity() -> None:
    memory_columns = set(UserMemoryLLMSelection.__table__.columns.keys())

    assert {
        "id",
        "user_id",
        "provider",
        "gate_model",
        "extraction_model",
        "created_at",
        "updated_at",
    }.issubset(memory_columns)
    assert "embedding_model" not in memory_columns
    assert "embedding_dimensions" not in memory_columns
    assert "vector_family" not in memory_columns
    assert "deployment_id" not in memory_columns
    assert "deployment_ref" not in memory_columns
    assert "connection_id" not in memory_columns


def test_memory_llm_defaults_are_independent_from_embedding_selection_defaults() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        credential_refs: list[tuple[int, str]] = []
        service = EmbeddingRuntimeSelectionService(
            credential_ref_resolver=lambda user_id, provider: (
                credential_refs.append((user_id, provider))
                or LLMCredentialRef(user_id=user_id, provider=provider)
            ),
            db=db,
        )

        embedding_row = service.get_embedding_selection(user_id=user.id)
        memory_row = service.get_memory_llm_selection(user_id=user.id)
        memory_runtime = service.resolve_memory_llm_selection(user_id=user.id)
        db.commit()

        assert embedding_row.provider == OPENAI_PROVIDER_ID
        assert embedding_row.model == DEFAULT_OPENAI_EMBEDDING_MODEL
        assert embedding_row.dimensions == DB_EMBEDDING_DIMENSIONS
        assert memory_row.provider == OPENAI_PROVIDER_ID
        assert memory_row.gate_model == DEFAULT_MEMORY_GATE_MODEL
        assert memory_row.extraction_model == DEFAULT_MEMORY_EXTRACTION_MODEL
        assert memory_runtime.provider == OPENAI_PROVIDER_ID
        assert memory_runtime.gate_model == DEFAULT_MEMORY_GATE_MODEL
        assert memory_runtime.extraction_model == DEFAULT_MEMORY_EXTRACTION_MODEL
        assert memory_runtime.credential_ref == LLMCredentialRef(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
        )
        assert credential_refs == [(user.id, OPENAI_PROVIDER_ID)]
        assert memory_runtime.gate_model != embedding_row.model
        assert memory_runtime.extraction_model != embedding_row.model
    finally:
        db.close()


def test_memory_llm_updates_do_not_mutate_embedding_selection() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-memory-llm-update")
        service = EmbeddingRuntimeSelectionService(
            credential_ref_resolver=lambda user_id, provider: LLMCredentialRef(
                user_id=user_id,
                provider=provider,
            ),
            db=db,
        )

        embedding_row = service.get_embedding_selection(user_id=user.id)
        service.set_memory_llm_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            gate_model="gpt-5-mini",
            extraction_model="gpt-5-nano",
        )
        db.commit()

        refreshed_embedding = db.query(UserEmbeddingSelection).filter_by(
            user_id=user.id,
        ).one()
        memory_row = db.query(UserMemoryLLMSelection).filter_by(
            user_id=user.id,
        ).one()

        assert refreshed_embedding.provider == embedding_row.provider
        assert refreshed_embedding.model == DEFAULT_OPENAI_EMBEDDING_MODEL
        assert refreshed_embedding.dimensions == DB_EMBEDDING_DIMENSIONS
        assert refreshed_embedding.vector_family == embedding_row.vector_family
        assert memory_row.gate_model == "gpt-5-mini"
        assert memory_row.extraction_model == "gpt-5-nano"
    finally:
        db.close()
