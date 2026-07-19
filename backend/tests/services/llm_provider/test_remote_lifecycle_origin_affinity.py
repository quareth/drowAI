"""Verify remote conversation lifecycle stays bound to its creation origin."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    Task,
    Tenant,
    User,
    UserLLMSelection,
)
from backend.services.llm_provider.conversation_lifecycle_service import (
    LLMConversationLifecycleService,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMAuthMode,
    LLMConnectionAuthorizationError,
    LLMConnectionOperation,
    LLMConnectionCredentialRef,
    ProviderConfigurationError,
    ProviderSecret,
    ResolvedAuth,
)


class _CredentialService:
    """Record the exact snapshotted connection used for credential resolution."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def resolve_connection_auth(
        self,
        connection_ref: LLMConnectionCredentialRef,
        **kwargs,
    ) -> ResolvedAuth:
        self.calls.append({"connection_ref": connection_ref, **kwargs})
        return ResolvedAuth.with_secret(
            mode=LLMAuthMode.API_KEY,
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-origin"),
        )


class _Transport:
    """Return deterministic remote IDs and record guarded lifecycle operations."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, operation, **kwargs) -> GuardedHTTPResponse:
        self.calls.append({"operation": operation, **kwargs})
        body = b'{"id":"remote-origin-1"}' if operation == LLMConnectionOperation.LIFECYCLE_CREATE else b"{}"
        return GuardedHTTPResponse(status_code=200, body=body, audit_id="origin-audit")


def _target(
    db: Session,
    *,
    user: User,
    tenant: Tenant,
    task: Task,
    model: str,
    api_surface: str = "responses",
) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=user.id,
        display_name=f"Origin {model}",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
        serving_operator_id="openai",
        transport_origin="backend",
        endpoint_url=None,
        endpoint_policy_id="fixed_provider_v1",
        config_schema_version=1,
        non_secret_config={"auth_mode": "api_key"},
        state="enabled",
        revision=1,
    )
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id=model,
        canonical_model_id=model,
        display_name=model,
        discovery_source="test",
        lifecycle_state="active",
        availability_state="available",
        enabled=True,
        revision=1,
    )
    route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=deployment.id,
        adapter_id="openai_responses" if api_surface == "responses" else "openai_chat",
        adapter_version="1",
        api_surface=api_surface,
        dialect_policy_id=(
            "openai_responses.native_v1"
            if api_surface == "responses"
            else "openai_chat.native_v1"
        ),
        enabled=True,
    )
    db.add_all([connection, deployment, route])
    db.flush()
    return connection, deployment, route


def _identity(db: Session, owner: User) -> tuple[Tenant, Task]:
    tenant = Tenant(slug=f"origin-{uuid4().hex}", name="Origin Tenant")
    db.add(tenant)
    db.flush()
    task = Task(user_id=owner.id, tenant_id=tenant.id, name="Origin Task")
    db.add(task)
    db.flush()
    return tenant, task


def test_delete_uses_creation_origin_after_selection_changes(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _other = identity_users
    tenant, task = _identity(llm_identity_db, owner)
    first = _target(
        llm_identity_db,
        user=owner,
        tenant=tenant,
        task=task,
        model="gpt-5.2",
    )
    second = _target(
        llm_identity_db,
        user=owner,
        tenant=tenant,
        task=task,
        model="gpt-5-mini",
    )
    selection = UserLLMSelection(
        user_id=owner.id,
        provider="openai",
        model="gpt-5.2",
        deployment_id=first[1].id,
    )
    llm_identity_db.add(selection)
    llm_identity_db.flush()
    credentials = _CredentialService()
    transport = _Transport()
    service = LLMConversationLifecycleService(
        llm_identity_db,
        credential_service=credentials,
        guarded_transport=transport,
    )

    origin = service.create_remote_conversation(
        runtime_user_id=owner.id,
        task_id=task.id,
        tenant_id=tenant.id,
    )
    selection.deployment_id = second[1].id
    selection.model = "gpt-5-mini"
    llm_identity_db.flush()
    service.delete_remote_conversation(
        origin=origin,
        runtime_user_id=owner.id,
        task_id=task.id,
        tenant_id=tenant.id,
    )

    assert origin.connection_id == str(first[0].id)
    assert origin.deployment_id == str(first[1].id)
    assert origin.route_id == str(first[2].id)
    assert origin.origin_revision == 1
    assert origin.deployment_revision == 1
    assert origin.remote_resource_id == "remote-origin-1"
    assert [call["connection_ref"].connection_id for call in credentials.calls] == [
        str(first[0].id),
        str(first[0].id),
    ]
    assert transport.calls[-1]["resource_id"] == "remote-origin-1"


def test_stale_creation_origin_fails_closed_before_delete_transport(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _other = identity_users
    tenant, task = _identity(llm_identity_db, owner)
    connection, deployment, _route = _target(
        llm_identity_db,
        user=owner,
        tenant=tenant,
        task=task,
        model="gpt-5.2",
    )
    llm_identity_db.add(
        UserLLMSelection(
            user_id=owner.id,
            provider="openai",
            model="gpt-5.2",
            deployment_id=deployment.id,
        )
    )
    transport = _Transport()
    service = LLMConversationLifecycleService(
        llm_identity_db,
        credential_service=_CredentialService(),
        guarded_transport=transport,
    )
    origin = service.create_remote_conversation(
        runtime_user_id=owner.id,
        task_id=task.id,
        tenant_id=tenant.id,
    )
    connection.revision = 2
    llm_identity_db.flush()

    with pytest.raises(LLMConnectionAuthorizationError, match="stale"):
        service.delete_remote_conversation(
            origin=origin,
            runtime_user_id=owner.id,
            task_id=task.id,
            tenant_id=tenant.id,
        )

    assert [call["operation"] for call in transport.calls] == [
        LLMConnectionOperation.LIFECYCLE_CREATE
    ]


def test_openai_chat_route_does_not_inherit_remote_lifecycle(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _other = identity_users
    tenant, task = _identity(llm_identity_db, owner)
    _connection, deployment, _route = _target(
        llm_identity_db,
        user=owner,
        tenant=tenant,
        task=task,
        model="gpt-4o",
        api_surface="chat_completions",
    )
    llm_identity_db.add(
        UserLLMSelection(
            user_id=owner.id,
            provider="openai",
            model="gpt-4o",
            deployment_id=deployment.id,
        )
    )
    transport = _Transport()
    service = LLMConversationLifecycleService(
        llm_identity_db,
        credential_service=_CredentialService(),
        guarded_transport=transport,
    )

    with pytest.raises(ProviderConfigurationError, match="does not support"):
        service.create_remote_conversation(
            runtime_user_id=owner.id,
            task_id=task.id,
            tenant_id=tenant.id,
        )

    assert transport.calls == []
