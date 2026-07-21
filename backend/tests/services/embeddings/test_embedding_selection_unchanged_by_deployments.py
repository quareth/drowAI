"""Tests that text deployment selection does not alter embedding identity."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
    UserEmbeddingSelection,
    UserMemoryLLMSelection,
)
from backend.services.embeddings.selection_service import EmbeddingRuntimeSelectionService
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.types import DeploymentRef, LLMCredentialRef


@pytest.fixture
def memory_selection_db() -> Iterator[Session]:
    """Yield an isolated database with memory and deployment identity tables."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            UserEmbeddingSelection.__table__,
            UserMemoryLLMSelection.__table__,
            LLMInferenceConnection.__table__,
            LLMModelDeployment.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _deployment(db: Session, *, user_id: int, model: str):
    connection = LLMConnectionService(db).create_draft(
        user_id=user_id,
        display_name=f"Memory {model}",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )
    return LLMDeploymentService(db).create_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id=model,
        canonical_model_id=model,
        display_name=model,
        discovery_source="operator",
    )


def test_memory_llm_deployments_preserve_embedding_selection_storage(
    memory_selection_db: Session,
) -> None:
    """Gate/extraction refs are text-only and cannot mutate embedding fields."""

    user = User(username="memory-deployment-owner", password="hashed")
    memory_selection_db.add(user)
    memory_selection_db.flush()
    service = EmbeddingRuntimeSelectionService(
        credential_ref_resolver=lambda user_id, provider: LLMCredentialRef(
            user_id=user_id,
            provider=provider,
        ),
        db=memory_selection_db,
    )
    embedding = service.get_embedding_selection(user_id=user.id)
    embedding_snapshot = (
        embedding.provider,
        embedding.model,
        embedding.dimensions,
        embedding.vector_family,
    )
    gate = _deployment(memory_selection_db, user_id=user.id, model="gpt-5-nano")
    extraction = _deployment(memory_selection_db, user_id=user.id, model="gpt-5-mini")

    saved = service.set_memory_llm_deployment_selection(
        user_id=user.id,
        gate_deployment_id=str(gate.id),
        expected_gate_revision=1,
        extraction_deployment_id=str(extraction.id),
        expected_extraction_revision=1,
    )
    runtime = service.resolve_memory_llm_selection(user_id=user.id)
    refreshed_embedding = memory_selection_db.get(UserEmbeddingSelection, embedding.id)

    assert saved.gate_deployment_id == gate.id
    assert saved.extraction_deployment_id == extraction.id
    assert saved.provider == "openai"
    assert saved.gate_model == "gpt-5-nano"
    assert saved.extraction_model == "gpt-5-mini"
    assert runtime.gate_deployment_ref == DeploymentRef(str(gate.id), 1)
    assert runtime.extraction_deployment_ref == DeploymentRef(str(extraction.id), 1)
    assert refreshed_embedding is not None
    assert (
        refreshed_embedding.provider,
        refreshed_embedding.model,
        refreshed_embedding.dimensions,
        refreshed_embedding.vector_family,
    ) == embedding_snapshot
    assert set(UserEmbeddingSelection.__table__.columns.keys()) == {
        "id",
        "user_id",
        "provider",
        "model",
        "dimensions",
        "vector_family",
        "created_at",
        "updated_at",
    }
