"""Regression tests for embedding credential resolution after uniqueness retirement."""

from __future__ import annotations

from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.models import User
from backend.services.embeddings.profiles import DEFAULT_OPENAI_EMBEDDING_MODEL
from backend.services.embeddings.selection_service import EmbeddingRuntimeSelectionService
from backend.services.llm_provider import LLMCredentialService


def test_embedding_provider_credential_ref_resolves_updated_singleton(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Provider-based embeddings resolve the latest singleton credential."""

    owner, _ = identity_users
    credential_service = LLMCredentialService(llm_identity_db)
    credential_service.upsert_api_key(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        api_key="sk-embedding-old",
    )
    credential_service.upsert_api_key(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        api_key="sk-embedding-default",
    )
    selection = EmbeddingRuntimeSelectionService(
        credential_ref_resolver=credential_service.get_credential_ref,
        db=llm_identity_db,
    ).resolve_embedding_selection(user_id=owner.id)
    secret = credential_service.resolve_secret(
        selection.credential_ref,
        runtime_user_id=owner.id,
        task_id=None,
        purpose="embedding",
    )

    assert selection.provider == OPENAI_PROVIDER_ID
    assert selection.model == DEFAULT_OPENAI_EMBEDDING_MODEL
    assert selection.dimensions == 1536
    assert (
        selection.vector_family
        == f"{OPENAI_PROVIDER_ID}:{DEFAULT_OPENAI_EMBEDDING_MODEL}:1536"
    )
    assert secret.value == "sk-embedding-default"
