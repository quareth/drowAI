"""Tests for deterministic remote conversation origin backfill."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models import (
    LLMConversation,
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    Task,
    Tenant,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
)
from backend.services.llm_provider.conversation_lifecycle_service import (
    LLMConversationLifecycleService,
)


def _task_identity(db: Session, owner: User) -> tuple[Tenant, Task]:
    tenant = Tenant(slug=f"origin-backfill-{owner.id}", name="Origin Backfill")
    db.add(tenant)
    db.flush()
    task = Task(user_id=owner.id, tenant_id=tenant.id, name="Origin Backfill")
    db.add(task)
    db.flush()
    return tenant, task


def _legacy_remote_row(
    db: Session,
    *,
    owner: User,
    task: Task,
    model: str = "gpt-5.2",
    remote_id: str = "remote-legacy-1",
) -> LLMConversation:
    row = LLMConversation(
        task_id=task.id,
        tenant_id=task.tenant_id,
        user_id=owner.id,
        provider="openai",
        model=model,
        conversation_id=remote_id,
        status="active",
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def test_deterministic_legacy_remote_row_receives_origin_snapshots(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Legacy remote rows map only through deterministic legacy identity."""

    owner, _ = identity_users
    _tenant, task = _task_identity(llm_identity_db, owner)
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider="openai",
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            UserLLMSelection(
                user_id=owner.id,
                provider="openai",
                model="gpt-5.2",
            ),
        ]
    )
    row = _legacy_remote_row(llm_identity_db, owner=owner, task=task)
    llm_identity_db.flush()

    service = LLMConversationLifecycleService(llm_identity_db)

    assert service.backfill_remote_conversation_origin(row) is True
    assert row.remote_resource_id == "remote-legacy-1"
    assert row.connection_id is not None
    assert row.deployment_id is not None
    assert row.route_id is not None
    assert row.origin_revision == 1
    assert row.origin_deployment_revision == 1
    deployment = llm_identity_db.get(LLMModelDeployment, row.deployment_id)
    assert deployment is not None
    assert deployment.wire_model_id == "gpt-5.2"
    route = llm_identity_db.get(LLMDeploymentRoute, row.route_id)
    assert route is not None
    assert route.deployment_id == deployment.id
    assert route.api_surface == "responses"


def test_unmapped_legacy_remote_row_remains_readable_history(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Rows without deterministic identity stay unmodified for history reads."""

    owner, _ = identity_users
    _tenant, task = _task_identity(llm_identity_db, owner)
    row = _legacy_remote_row(llm_identity_db, owner=owner, task=task)

    assert (
        LLMConversationLifecycleService(
            llm_identity_db
        ).backfill_remote_conversation_origin(row)
        is False
    )
    assert row.conversation_id == "remote-legacy-1"
    assert row.remote_resource_id is None
    assert row.connection_id is None
    assert row.deployment_id is None
    assert row.route_id is None
    assert row.origin_revision is None
    assert row.origin_deployment_revision is None


def test_backfill_does_not_substitute_current_selection_for_row_model(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Current selection/default identity cannot repair a mismatched origin."""

    owner, _ = identity_users
    _tenant, task = _task_identity(llm_identity_db, owner)
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider="openai",
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            UserLLMSelection(
                user_id=owner.id,
                provider="openai",
                model="gpt-5.2",
            ),
        ]
    )
    row = _legacy_remote_row(
        llm_identity_db,
        owner=owner,
        task=task,
        model="gpt-unmapped-legacy",
    )
    llm_identity_db.flush()

    assert (
        LLMConversationLifecycleService(
            llm_identity_db
        ).backfill_remote_conversation_origin(row)
        is False
    )
    assert row.connection_id is None
    assert row.deployment_id is None
    assert row.route_id is None


def test_non_remote_lifecycle_model_remains_unmapped_without_route_artifacts(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Backfill does not create origin refs for non-responses API models."""

    owner, _ = identity_users
    _tenant, task = _task_identity(llm_identity_db, owner)
    llm_identity_db.add(
        UserLLMProviderCredential(
            user_id=owner.id,
            provider="openai",
            encrypted_api_key="ciphertext",
            enabled=True,
        )
    )
    row = _legacy_remote_row(
        llm_identity_db,
        owner=owner,
        task=task,
        model="gpt-4o",
    )
    llm_identity_db.flush()

    assert (
        LLMConversationLifecycleService(
            llm_identity_db
        ).backfill_remote_conversation_origin(row)
        is False
    )
    assert row.deployment_id is None
    assert llm_identity_db.query(LLMInferenceConnection).count() == 0
    assert llm_identity_db.query(LLMModelDeployment).count() == 0
    assert llm_identity_db.query(LLMDeploymentRoute).count() == 0
