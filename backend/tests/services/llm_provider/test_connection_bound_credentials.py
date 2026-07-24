"""Tests for connection-bound typed LLM authentication resolution."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    LLMConnectionCredential,
    LLMInferenceConnection,
    User,
    UserLLMProviderCredential,
)
from backend.services.llm_provider import credential_service as credential_module
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.migration_service import (
    deterministic_legacy_connection_id,
)
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.types import (
    CredentialAuthorizationError,
    CredentialNotFoundError,
    LLMAuthMode,
    LLMConnectionCredentialRef,
    LLMConnectionValidationError,
    ResolvedAuth,
)


def _use_test_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credential_module,
        "encrypt_api_key",
        lambda value: f"encrypted:{value}",
    )
    monkeypatch.setattr(
        credential_module,
        "decrypt_api_key",
        lambda value: value.removeprefix("encrypted:"),
    )


def test_none_auth_resolves_without_dummy_credential(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """An owned connection can use no auth without a placeholder secret row."""

    owner, _ = identity_users
    connections = LLMConnectionService(llm_identity_db)
    connection = connections.create_draft(
        user_id=owner.id,
        display_name="Local unauthenticated endpoint",
        connection_preset_id="openai",
        runtime_family_id="openai_compatible",
    )
    ref = connections.authorize_credential_binding(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
    )

    resolved = LLMCredentialService(llm_identity_db).resolve_connection_auth(
        ref,
        runtime_user_id=owner.id,
        purpose="connection_test",
        auth_mode=LLMAuthMode.NONE,
    )

    assert resolved == ResolvedAuth.none()
    assert resolved.secret is None
    assert ResolvedAuth.operator_managed().secret is None
    assert llm_identity_db.execute(
        select(UserLLMProviderCredential).where(
            UserLLMProviderCredential.user_id == owner.id
        )
    ).scalar_one_or_none() is None
    assert llm_identity_db.execute(
        select(LLMConnectionCredential).where(
            LLMConnectionCredential.connection_id == connection.id
        )
    ).scalar_one_or_none() is None


def test_legacy_facade_targets_the_provider_singleton(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy credentials resolve only through the user's provider singleton."""

    _use_test_cipher(monkeypatch)
    owner, _ = identity_users
    credentials = LLMCredentialService(llm_identity_db)
    status = credentials.upsert_api_key(
        user_id=owner.id,
        provider="openai",
        api_key="sk-owner-only",
    )
    default_id = deterministic_legacy_connection_id(owner.id, "openai")
    assert status.connection_id == str(default_id)
    assert status.auth_mode is LLMAuthMode.API_KEY

    connections = LLMConnectionService(llm_identity_db)
    default = llm_identity_db.get(LLMInferenceConnection, default_id)
    assert default is not None
    assert default.legacy_default_provider == "openai"
    assert connections.get_owned_for_preset(
        user_id=owner.id,
        connection_preset_id="openai",
    ) == default
    with pytest.raises(
        LLMConnectionValidationError,
        match="only one connection per preset",
    ):
        connections.create_draft(
            user_id=owner.id,
            display_name="Additional OpenAI",
            connection_preset_id="openai",
            runtime_family_id="openai_native",
        )

    default_ref = connections.authorize_credential_binding(
        user_id=owner.id,
        connection_id=default.id,
        expected_revision=default.revision,
    )
    resolved = credentials.resolve_connection_auth(
        default_ref,
        runtime_user_id=owner.id,
        purpose="legacy_provider_test",
        auth_mode=LLMAuthMode.API_KEY,
    )
    assert resolved.mode is LLMAuthMode.API_KEY
    assert resolved.secret is not None
    assert resolved.secret.value == "sk-owner-only"
    bearer = credentials.resolve_connection_auth(
        default_ref,
        runtime_user_id=owner.id,
        purpose="legacy_bearer_test",
        auth_mode=LLMAuthMode.BEARER,
    )
    assert bearer.mode is LLMAuthMode.BEARER
    assert bearer.secret is not None
    assert bearer.secret.value == "sk-owner-only"

    safe_status = asdict(status)
    assert "sk-owner-only" not in repr(status)
    assert "sk-owner-only" not in repr(resolved)
    assert "sk-owner-only" not in repr(bearer)
    assert "encrypted:sk-owner-only" not in repr(safe_status)
    assert "endpoint" not in safe_status


def test_legacy_delete_leaves_provider_singleton_unconfigured(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting legacy auth keeps the provider singleton without a credential."""

    _use_test_cipher(monkeypatch)
    owner, _ = identity_users
    credentials = LLMCredentialService(llm_identity_db)
    credentials.upsert_api_key(
        user_id=owner.id,
        provider="openai",
        api_key="sk-delete-me",
    )
    default_id = deterministic_legacy_connection_id(owner.id, "openai")
    credentials.delete(user_id=owner.id, provider="openai")

    default = llm_identity_db.get(LLMInferenceConnection, default_id)
    assert default is not None
    assert default.legacy_default_provider == "openai"
    assert credentials.get_masked_status(owner.id, "openai").connection_id == str(
        default_id
    )
    with pytest.raises(CredentialNotFoundError):
        credentials.resolve_connection_auth(
            LLMConnectionService(llm_identity_db).authorize_credential_binding(
                user_id=owner.id,
                connection_id=default_id,
                expected_revision=default.revision,
            ),
            runtime_user_id=owner.id,
            purpose="deleted_default",
            auth_mode=LLMAuthMode.API_KEY,
        )


def test_connection_auth_rejects_foreign_owner_and_stale_revision(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Live connection ownership and revision are reloaded before auth lookup."""

    owner, other = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Owned connection",
        connection_preset_id="openai",
        runtime_family_id="openai_compatible",
    )
    service = LLMCredentialService(llm_identity_db)

    with pytest.raises(CredentialAuthorizationError):
        service.resolve_connection_auth(
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=1,
            ),
            runtime_user_id=other.id,
            purpose="foreign",
            auth_mode=LLMAuthMode.NONE,
        )

    ref = LLMConnectionService(llm_identity_db).authorize_credential_binding(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
    )
    connection.revision = 2
    llm_identity_db.flush()
    with pytest.raises(CredentialAuthorizationError):
        service.resolve_connection_auth(
            ref,
            runtime_user_id=owner.id,
            purpose="stale",
            auth_mode=LLMAuthMode.NONE,
        )


def test_same_preset_connections_are_isolated_between_users(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each user owns one isolated connector and credential for a preset."""

    _use_test_cipher(monkeypatch)
    owner, other = identity_users
    connections = LLMConnectionService(llm_identity_db)
    credentials = LLMCredentialService(llm_identity_db)
    preset = ConnectionOperationRegistry().get_connection_preset(
        CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
    )

    def create_connection(
        user_id: int,
        label: str,
        endpoint: str,
        secret: str,
    ) -> LLMInferenceConnection:
        connection = connections.create_draft(
            user_id=user_id,
            display_name=label,
            connection_preset_id=preset.id,
            runtime_family_id=preset.runtime_family_id,
            serving_operator_id=preset.serving_operator_id,
            non_secret_config={
                "auth_mode": "bearer",
                "base_url": endpoint,
            },
        )
        credentials.upsert_connection_api_key(
            user_id=user_id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            api_key=secret,
        )
        return connection

    connection_a = create_connection(
        owner.id,
        "Endpoint A",
        "https://a.example.test",
        "key-a-placeholder",
    )
    connection_b = create_connection(
        other.id,
        "Endpoint B",
        "https://b.example.test",
        "key-b-placeholder",
    )

    stored_credentials = llm_identity_db.execute(
        select(LLMConnectionCredential).order_by(
            LLMConnectionCredential.connection_id
        )
    ).scalars().all()
    assert {credential.connection_id for credential in stored_credentials} == {
        connection_a.id,
        connection_b.id,
    }
    assert llm_identity_db.execute(
        select(UserLLMProviderCredential).where(
            UserLLMProviderCredential.provider == preset.id,
        )
    ).scalar_one_or_none() is None

    def resolved_secret(
        connection: LLMInferenceConnection,
        user_id: int,
    ) -> str:
        resolved = credentials.resolve_connection_auth(
            LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            runtime_user_id=user_id,
            purpose="same-preset-isolation",
            auth_mode=LLMAuthMode.BEARER,
        )
        assert resolved.secret is not None
        return resolved.secret.value

    assert resolved_secret(connection_a, owner.id) == "key-a-placeholder"
    assert resolved_secret(connection_b, other.id) == "key-b-placeholder"
    with pytest.raises(CredentialAuthorizationError):
        resolved_secret(connection_b, owner.id)
