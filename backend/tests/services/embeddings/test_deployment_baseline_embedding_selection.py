"""Deployment baseline tests for embedding selection non-regression."""

from __future__ import annotations

from uuid import uuid4

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import User
from backend.models.llm import UserEmbeddingSelection
from backend.services.embeddings.factory import EmbeddingProviderFactory
from backend.services.embeddings.profiles import (
    DB_EMBEDDING_DIMENSIONS,
    DEFAULT_OPENAI_EMBEDDING_MODEL,
)
from backend.services.embeddings.providers.openai import OpenAIEmbeddingProvider
from backend.services.embeddings.selection_service import (
    EmbeddingRuntimeSelectionService,
)
from backend.services.llm_provider import LLMCredentialRef


def _create_user(db, username_prefix: str = "deployment-embedding") -> User:
    user = User(
        username=f"{username_prefix}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_embedding_selection_table_remains_provider_model_vector_identity() -> None:
    columns = set(UserEmbeddingSelection.__table__.columns.keys())

    assert {
        "id",
        "user_id",
        "provider",
        "model",
        "dimensions",
        "vector_family",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert "deployment_id" not in columns
    assert "deployment_ref" not in columns
    assert "connection_id" not in columns
    assert "route_id" not in columns


def test_default_embedding_selection_persists_stable_dimensions_and_vector_family() -> None:
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

        selection = service.get_embedding_selection(user_id=user.id)
        runtime_selection = service.resolve_embedding_selection(user_id=user.id)
        db.commit()

        assert selection.provider == OPENAI_PROVIDER_ID
        assert selection.model == DEFAULT_OPENAI_EMBEDDING_MODEL
        assert selection.dimensions == DB_EMBEDDING_DIMENSIONS
        assert (
            selection.vector_family
            == f"{OPENAI_PROVIDER_ID}:{DEFAULT_OPENAI_EMBEDDING_MODEL}:{DB_EMBEDDING_DIMENSIONS}"
        )
        assert runtime_selection.provider == selection.provider
        assert runtime_selection.model == selection.model
        assert runtime_selection.dimensions == selection.dimensions
        assert runtime_selection.vector_family == selection.vector_family
        assert runtime_selection.credential_ref == LLMCredentialRef(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
        )
        assert credential_refs == [(user.id, OPENAI_PROVIDER_ID)]
    finally:
        db.close()


def test_embedding_factory_preserves_openai_embedding_provider_contract() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-embedding-factory")
        service = EmbeddingRuntimeSelectionService(
            credential_ref_resolver=lambda user_id, provider: LLMCredentialRef(
                user_id=user_id,
                provider=provider,
            ),
            db=db,
        )
        selection = service.resolve_embedding_selection(user_id=user.id)

        provider = EmbeddingProviderFactory().create(
            selection,
            api_key="sk-embedding",
        )

        assert isinstance(provider, OpenAIEmbeddingProvider)
        assert provider.model == DEFAULT_OPENAI_EMBEDDING_MODEL
        assert provider.dimensions == DB_EMBEDDING_DIMENSIONS
        assert provider.profile.ref.provider == OPENAI_PROVIDER_ID
        assert provider.profile.ref.model == DEFAULT_OPENAI_EMBEDDING_MODEL
        assert provider.profile.vector_family == selection.vector_family
    finally:
        db.close()
