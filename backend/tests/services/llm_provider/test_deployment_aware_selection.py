"""Tests for deployment-aware conversation LLM selection persistence."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.models import User, UserLLMProviderCredential, UserLLMSelection
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.selection_service import LLMProviderSelectionService
from backend.services.llm_provider.types import (
    LLMConnectionState,
    LLMDeploymentNotFoundError,
    LLMRuntimeSelectionV2,
    ProviderConfigurationError,
)


def _active_deployment(db: Session, *, user_id: int, model: str = "gpt-5.2"):
    connections = LLMConnectionService(db)
    connection = connections.create_draft(
        user_id=user_id,
        display_name="Conversation endpoint",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )
    connection = connections.transition_state(
        user_id=user_id,
        connection_id=connection.id,
        expected_revision=1,
        target_state=LLMConnectionState.DISABLED,
    )
    connections.transition_state(
        user_id=user_id,
        connection_id=connection.id,
        expected_revision=connection.revision,
        target_state=LLMConnectionState.ENABLED,
    )
    return LLMDeploymentService(db).create_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=3,
        wire_model_id=model,
        canonical_model_id=model,
        display_name=model,
        discovery_source="operator",
    )


def test_conversation_selection_persists_deployment_and_legacy_snapshot(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """A deployment selection remains readable by legacy provider/model clients."""

    owner, _ = identity_users
    deployment = _active_deployment(llm_identity_db, user_id=owner.id)
    service = LLMProviderSelectionService(llm_identity_db)

    saved = service.set_deployment_selection(
        user_id=owner.id,
        deployment_id=str(deployment.id),
        expected_deployment_revision=1,
    )
    runtime = service.build_deployment_runtime_selection(user_id=owner.id)

    assert saved.deployment_id == deployment.id
    assert saved.provider == "openai"
    assert saved.model == "gpt-5.2"
    assert isinstance(runtime, LLMRuntimeSelectionV2)
    assert runtime.deployment_ref.deployment_id == str(deployment.id)
    assert runtime.deployment_ref.expected_revision == 1
    assert runtime.legacy_provider == "openai"
    assert runtime.legacy_model == "gpt-5.2"

    deployment.enabled = False
    llm_identity_db.flush()
    with pytest.raises(LLMDeploymentNotFoundError):
        service.build_deployment_runtime_selection(user_id=owner.id)


def test_conversation_deployment_selection_rejects_foreign_and_stale_refs(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Selection writes recheck owner scope and optimistic deployment revision."""

    owner, other = identity_users
    deployment = _active_deployment(llm_identity_db, user_id=owner.id)
    service = LLMProviderSelectionService(llm_identity_db)

    with pytest.raises(LLMDeploymentNotFoundError):
        service.set_deployment_selection(
            user_id=other.id,
            deployment_id=str(deployment.id),
            expected_deployment_revision=1,
        )
    with pytest.raises(LLMDeploymentNotFoundError):
        service.set_deployment_selection(
            user_id=owner.id,
            deployment_id=str(deployment.id),
            expected_deployment_revision=2,
        )


def test_auth_missing_migrated_deployment_binding_is_selectable_not_runnable(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """A mapped legacy row remains visible when its provider credential is absent."""

    owner, _ = identity_users
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider="openai",
                encrypted_api_key="",
                enabled=False,
            ),
            UserLLMSelection(user_id=owner.id, provider="openai", model="gpt-5.2"),
        ]
    )
    llm_identity_db.flush()

    service = LLMProviderSelectionService(llm_identity_db)
    read = service.get_selection_read(owner.id)

    assert read.selection.deployment_id is not None
    assert read.status.status == "credential_missing"
    assert read.status.selectable is True
    assert read.status.runnable is False
    with pytest.raises(ProviderConfigurationError):
        service.build_deployment_runtime_selection(user_id=owner.id)
