"""Tests for live LLM connection operation authorization boundaries."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.models import Task, Tenant, User
from backend.services.llm_provider.connection_authorization import (
    LLMConnectionAuthorizer,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_BASE_URL_ENV,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.types import (
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionState,
)


def _create_connection(
    db: Session,
    *,
    user_id: int,
    preset: str = "openai",
):
    return LLMConnectionService(db).create_draft(
        user_id=user_id,
        display_name=f"{preset} connection",
        connection_preset_id=preset,
        runtime_family_id=f"{preset}_native",
    )


def _enable_connection(db: Session, *, user_id: int, connection_id):
    service = LLMConnectionService(db)
    service.transition_state(
        user_id=user_id,
        connection_id=connection_id,
        expected_revision=1,
        target_state=LLMConnectionState.DISABLED,
    )
    return service.transition_state(
        user_id=user_id,
        connection_id=connection_id,
        expected_revision=2,
        target_state=LLMConnectionState.ENABLED,
    )


def test_authorization_reloads_revision_state_policy_operation_and_revocation(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Every authorization decision uses current server-side connection facts."""

    owner, _ = identity_users
    connection = _create_connection(llm_identity_db, user_id=owner.id)
    authorizer = LLMConnectionAuthorizer(llm_identity_db)
    context = LLMConnectionAccessContext(authenticated_user_id=owner.id)

    health = authorizer.authorize(
        access_context=context,
        connection_id=connection.id,
        expected_revision=1,
        operation="health",
    )
    assert health.operation_target.url == "https://api.openai.com/v1/models"
    with pytest.raises(LLMConnectionAuthorizationError) as draft_inference:
        authorizer.authorize(
            access_context=context,
            connection_id=connection.id,
            expected_revision=1,
            operation="inference",
        )
    assert draft_inference.value.code == "connection_not_enabled"

    enabled = _enable_connection(
        llm_identity_db,
        user_id=owner.id,
        connection_id=connection.id,
    )
    authorized = authorizer.authorize(
        access_context=context,
        connection_id=connection.id,
        expected_revision=enabled.revision,
        operation="inference",
    )
    assert authorized.connection_revision == 3

    with pytest.raises(LLMConnectionAuthorizationError) as stale:
        authorizer.authorize(
            access_context=context,
            connection_id=connection.id,
            expected_revision=2,
            operation="inference",
        )
    assert stale.value.code == "stale_connection_revision"

    connection.endpoint_policy_id = "unregistered_policy"
    llm_identity_db.flush()
    with pytest.raises(LLMConnectionAuthorizationError) as policy:
        authorizer.authorize(
            access_context=context,
            connection_id=connection.id,
            expected_revision=3,
            operation="inference",
        )
    assert policy.value.code == "endpoint_policy_denied"

    connection.endpoint_policy_id = "fixed_provider_v1"
    connection.endpoint_url = "https://attacker.invalid"
    llm_identity_db.flush()
    with pytest.raises(LLMConnectionAuthorizationError) as endpoint:
        authorizer.authorize(
            access_context=context,
            connection_id=connection.id,
            expected_revision=3,
            operation="inference",
        )
    assert endpoint.value.code == "endpoint_policy_denied"

    connection.endpoint_url = None
    llm_identity_db.flush()
    disabled = LLMConnectionService(llm_identity_db).transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=3,
        target_state=LLMConnectionState.DISABLED,
    )
    with pytest.raises(LLMConnectionAuthorizationError) as disabled_access:
        authorizer.authorize(
            access_context=context,
            connection_id=connection.id,
            expected_revision=disabled.revision,
            operation="health",
        )
    assert disabled_access.value.code == "connection_not_enabled"

    LLMConnectionService(llm_identity_db).delete(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=disabled.revision,
    )
    with pytest.raises(LLMConnectionAuthorizationError) as revoked:
        authorizer.authorize(
            access_context=context,
            connection_id=connection.id,
            expected_revision=disabled.revision,
            operation="health",
        )
    assert revoked.value.code == "connection_unavailable"


def test_authorization_keeps_endpoint_substitution_connection_scoped(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """A managed override cannot replace another connection's configured endpoint."""

    owner, _ = identity_users
    connections = LLMConnectionService(llm_identity_db)
    nvidia = connections.create_draft(
        user_id=owner.id,
        display_name="NVIDIA",
        connection_preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="nvidia_nim",
    )
    custom = connections.create_draft(
        user_id=owner.id,
        display_name="Team gateway",
        connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="organization_managed",
        non_secret_config={
            "base_url": "https://team-gateway.example.test",
            "auth_mode": "bearer",
        },
    )
    registry = ConnectionOperationRegistry(
        env_getter={
            NVIDIA_NIM_BASE_URL_ENV: "http://127.0.0.1:4000/v1",
        }.get
    )
    authorizer = LLMConnectionAuthorizer(
        llm_identity_db,
        operation_registry=registry,
    )
    context = LLMConnectionAccessContext(authenticated_user_id=owner.id)

    authorized_nvidia = authorizer.authorize(
        access_context=context,
        connection_id=nvidia.id,
        expected_revision=nvidia.revision,
        operation="health",
    )
    authorized_custom = authorizer.authorize(
        access_context=context,
        connection_id=custom.id,
        expected_revision=custom.revision,
        operation="health",
    )

    assert authorized_nvidia.operation_target.client_base_url == (
        "http://127.0.0.1:4000/v1"
    )
    assert authorized_custom.operation_target.client_base_url == (
        "https://team-gateway.example.test/v1"
    )


def test_authorization_rejects_unregistered_provider_operation_matrix(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Persisted data cannot broaden the code-owned operation registry."""

    owner, _ = identity_users
    connection = _create_connection(
        llm_identity_db,
        user_id=owner.id,
        preset="anthropic",
    )
    enabled = _enable_connection(
        llm_identity_db,
        user_id=owner.id,
        connection_id=connection.id,
    )

    with pytest.raises(LLMConnectionAuthorizationError) as denied:
        LLMConnectionAuthorizer(llm_identity_db).authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=owner.id
            ),
            connection_id=connection.id,
            expected_revision=enabled.revision,
            operation="lifecycle_create",
        )
    assert denied.value.code == "operation_not_permitted"


def test_task_bound_authorization_requires_same_owner_and_tenant_context(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Tenant membership or context never transfers a user's connection."""

    owner, other = identity_users
    llm_identity_db.add_all(
        [
            Tenant(id=1, slug="one", name="One"),
            Tenant(id=2, slug="two", name="Two"),
        ]
    )
    llm_identity_db.flush()
    owner_task = Task(user_id=owner.id, tenant_id=1, name="Owner task")
    other_task = Task(user_id=other.id, tenant_id=1, name="Other task")
    foreign_tenant_task = Task(
        user_id=owner.id,
        tenant_id=2,
        name="Foreign tenant task",
    )
    llm_identity_db.add_all([owner_task, other_task, foreign_tenant_task])
    llm_identity_db.flush()
    connection = _create_connection(llm_identity_db, user_id=owner.id)
    enabled = _enable_connection(
        llm_identity_db,
        user_id=owner.id,
        connection_id=connection.id,
    )
    authorizer = LLMConnectionAuthorizer(llm_identity_db)

    authorized = authorizer.authorize(
        access_context=LLMConnectionAccessContext(
            authenticated_user_id=owner.id,
            task_id=owner_task.id,
            tenant_id=1,
        ),
        connection_id=connection.id,
        expected_revision=enabled.revision,
        operation="inference",
    )
    assert authorized.connection_id == str(connection.id)

    denied_contexts = (
        LLMConnectionAccessContext(
            authenticated_user_id=owner.id,
            task_id=other_task.id,
            tenant_id=1,
        ),
        LLMConnectionAccessContext(
            authenticated_user_id=other.id,
            task_id=other_task.id,
            tenant_id=1,
        ),
        LLMConnectionAccessContext(
            authenticated_user_id=owner.id,
            task_id=foreign_tenant_task.id,
            tenant_id=1,
        ),
    )
    for denied_context in denied_contexts:
        with pytest.raises(LLMConnectionAuthorizationError):
            authorizer.authorize(
                access_context=denied_context,
                connection_id=connection.id,
                expected_revision=enabled.revision,
                operation="inference",
            )
