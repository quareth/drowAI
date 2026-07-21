"""Regression tests for embedding credential resolution after uniqueness retirement."""

from __future__ import annotations

from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.models import User, UserLLMProviderCredential
from backend.services.embeddings.profiles import DEFAULT_OPENAI_EMBEDDING_MODEL
from backend.services.embeddings.selection_service import EmbeddingRuntimeSelectionService
from backend.services.llm_provider.credential_service import encrypt_api_key
from backend.services.llm_provider import LLMCredentialService


def test_embedding_provider_credential_ref_resolves_duplicate_legacy_rows(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Provider-based embeddings keep deterministic credential compatibility."""

    owner, _ = identity_users
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key="",
                enabled=False,
            ),
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key=encrypt_api_key("sk-embedding-default"),
                enabled=True,
            ),
        ]
    )
    llm_identity_db.flush()

    credential_service = LLMCredentialService(llm_identity_db)
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
