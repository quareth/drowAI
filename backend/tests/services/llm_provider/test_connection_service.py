"""Tests for user-owned LLM inference connection lifecycle services."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import LLMInferenceConnection, User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.types import (
    LLMConnectionNotFoundError,
    LLMConnectionRevisionConflictError,
    LLMConnectionState,
    LLMConnectionStateTransitionError,
    LLMConnectionValidationError,
)


def test_create_persists_owned_revisioned_draft_before_credential_binding(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Credential binding can target only a flushed user-owned draft row."""

    owner, other = identity_users
    service = LLMConnectionService(llm_identity_db)

    connection = service.create_draft(
        user_id=owner.id,
        display_name="Owner OpenAI",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )

    persisted = llm_identity_db.execute(
        select(LLMInferenceConnection).where(
            LLMInferenceConnection.id == connection.id
        )
    ).scalar_one()
    assert persisted.user_id == owner.id
    assert persisted.state == LLMConnectionState.DRAFT.value
    assert persisted.revision == 1
    assert persisted.endpoint_policy_id == "fixed_provider_v1"

    binding = service.authorize_credential_binding(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
    )
    assert binding.connection_id == str(connection.id)
    assert binding.expected_revision == 1

    with pytest.raises(LLMConnectionNotFoundError):
        service.authorize_credential_binding(
            user_id=other.id,
            connection_id=connection.id,
            expected_revision=1,
        )
    with pytest.raises(LLMConnectionNotFoundError):
        service.authorize_credential_binding(
            user_id=owner.id,
            connection_id=uuid4(),
            expected_revision=1,
        )


def test_crud_and_state_transitions_are_owner_scoped_and_revision_checked(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Mutations fail closed for foreign owners, stale writes, and unsafe jumps."""

    owner, other = identity_users
    service = LLMConnectionService(llm_identity_db)
    connection = service.create_draft(
        user_id=owner.id,
        display_name="Connection",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )

    updated = service.update_draft(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
        display_name="Renamed Connection",
        non_secret_config={"organization": "example"},
    )
    assert updated.display_name == "Renamed Connection"
    assert updated.non_secret_config == {"organization": "example"}
    assert updated.revision == 2

    with pytest.raises(LLMConnectionRevisionConflictError):
        service.update_draft(
            user_id=owner.id,
            connection_id=connection.id,
            expected_revision=1,
            display_name="Stale",
        )
    with pytest.raises(LLMConnectionNotFoundError):
        service.update_draft(
            user_id=other.id,
            connection_id=connection.id,
            expected_revision=2,
            display_name="Foreign",
        )
    with pytest.raises(LLMConnectionValidationError):
        service.update_draft(
            user_id=owner.id,
            connection_id=connection.id,
            expected_revision=2,
            display_name="Unsafe Config",
            non_secret_config={"api_key": "must-not-persist"},
        )
    with pytest.raises(LLMConnectionStateTransitionError):
        service.transition_state(
            user_id=owner.id,
            connection_id=connection.id,
            expected_revision=2,
            target_state=LLMConnectionState.ENABLED,
        )

    disabled = service.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=2,
        target_state=LLMConnectionState.DISABLED,
    )
    assert disabled.state == LLMConnectionState.DISABLED.value
    enabled = service.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=3,
        target_state=LLMConnectionState.ENABLED,
    )
    assert enabled.state == LLMConnectionState.ENABLED.value
    assert enabled.revision == 4
    assert service.list_for_user(user_id=other.id) == ()

    service.delete(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=4,
    )
    assert service.list_for_user(user_id=owner.id) == ()
