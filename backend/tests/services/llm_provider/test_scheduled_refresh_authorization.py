"""Tests for scheduled LLM inventory refresh service authorization."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from backend.models import User
from backend.services.llm_provider.connection_authorization import LLMConnectionAuthorizer
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.inventory_service import LLMInventoryService
from backend.services.llm_provider.operation_registry import (
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.runtime_services import (
    LLM_INVENTORY_REFRESH_SERVICE_ACTOR,
    LLMServiceOperationContext,
)
from backend.services.llm_provider.types import (
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionOperation,
    LLMConnectionState,
)


def test_job_user_id_is_correlation_not_authorization(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Background refresh authorizer reloads owner and revision server-side."""

    owner, other = identity_users
    connection = _enabled_connection(llm_identity_db, owner=owner)

    with pytest.raises(ValueError, match="unsupported authorization fields"):
        LLMServiceOperationContext.from_job_payload(
            service_actor=LLM_INVENTORY_REFRESH_SERVICE_ACTOR,
            job_id="refresh-job-forged-user",
            correlation_metadata={"user_id": other.id},
        )

    deployments = LLMInventoryService(llm_identity_db).refresh_inventory_for_service(
        service_context=LLMServiceOperationContext.from_job_payload(
            service_actor=LLM_INVENTORY_REFRESH_SERVICE_ACTOR,
            job_id="refresh-job-valid",
            correlation_id="correlation-123",
            correlation_metadata={"source": "scheduler"},
        ),
        connection_id=connection.id,
        expected_connection_revision=connection.revision,
        discovered_model_ids=("hf/org-model-a",),
    )

    assert tuple(str(deployment.connection_id) for deployment in deployments) == (
        str(connection.id),
    )
    assert deployments[0].source_metadata == {
        "availability_source": "scheduled_refresh",
        "service_actor": LLM_INVENTORY_REFRESH_SERVICE_ACTOR,
        "job_id": "refresh-job-valid",
        "correlation_id": "correlation-123",
    }


@pytest.mark.parametrize(
    ("mutate_connection", "expected_revision", "operation", "expected_code"),
    (
        (lambda _db, connection, _owner: None, 2, LLMConnectionOperation.INVENTORY, "stale_connection_revision"),
        (
            lambda _db, connection, _owner: setattr(
                connection,
                "endpoint_policy_id",
                "forged_policy",
            ),
            3,
            LLMConnectionOperation.INVENTORY,
            "endpoint_policy_denied",
        ),
        (
            lambda db, connection, owner: LLMConnectionService(db).transition_state(
                user_id=owner.id,
                connection_id=connection.id,
                expected_revision=connection.revision,
                target_state=LLMConnectionState.DISABLED,
            ),
            4,
            LLMConnectionOperation.INVENTORY,
            "connection_not_enabled",
        ),
        (lambda _db, _connection, _owner: None, 3, LLMConnectionOperation.INFERENCE, "operation_not_permitted"),
    ),
)
def test_stale_disabled_and_broadened_jobs_fail_closed(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    mutate_connection,
    expected_revision: int,
    operation: LLMConnectionOperation,
    expected_code: str,
) -> None:
    owner, _ = identity_users
    connection = _enabled_connection(llm_identity_db, owner=owner)
    mutate_connection(llm_identity_db, connection, owner)
    llm_identity_db.flush()

    with pytest.raises(LLMConnectionAuthorizationError) as denied:
        LLMConnectionAuthorizer(llm_identity_db).authorize_service_operation(
            service_context=_service_context("refresh-job-denied"),
            connection_id=connection.id,
            expected_revision=expected_revision,
            operation=operation,
        )

    assert denied.value.code == expected_code


def test_forged_and_revoked_jobs_fail_closed(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    connection = _enabled_connection(llm_identity_db, owner=owner)
    authorizer = LLMConnectionAuthorizer(llm_identity_db)

    with pytest.raises(LLMConnectionAuthorizationError) as forged:
        authorizer.authorize_service_operation(
            service_context=_service_context("refresh-job-forged-connection"),
            connection_id=uuid4(),
            expected_revision=connection.revision,
            operation=LLMConnectionOperation.INVENTORY,
        )
    assert forged.value.code == "connection_unavailable"

    LLMConnectionService(llm_identity_db).delete(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=connection.revision,
    )
    with pytest.raises(LLMConnectionAuthorizationError) as revoked:
        authorizer.authorize_service_operation(
            service_context=_service_context("refresh-job-revoked"),
            connection_id=connection.id,
            expected_revision=connection.revision,
            operation=LLMConnectionOperation.INVENTORY,
        )
    assert revoked.value.code == "connection_unavailable"


def test_audit_context_distinguishes_service_and_authenticated_user(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    connection = _enabled_connection(llm_identity_db, owner=owner)
    authorizer = LLMConnectionAuthorizer(llm_identity_db)

    user_authorized = authorizer.authorize(
        access_context=LLMConnectionAccessContext(authenticated_user_id=owner.id),
        connection_id=connection.id,
        expected_revision=connection.revision,
        operation=LLMConnectionOperation.INVENTORY,
    )
    service_authorized = authorizer.authorize_service_operation(
        service_context=_service_context("refresh-job-audit"),
        connection_id=connection.id,
        expected_revision=connection.revision,
        operation=LLMConnectionOperation.INVENTORY,
    )

    assert user_authorized.audit_actor_type == "authenticated_user"
    assert user_authorized.audit_actor_id == str(owner.id)
    assert user_authorized.audit_correlation_id is None
    assert service_authorized.audit_actor_type == "service"
    assert service_authorized.audit_actor_id == LLM_INVENTORY_REFRESH_SERVICE_ACTOR
    assert service_authorized.audit_correlation_id == "refresh-job-audit"


def _enabled_connection(
    db: Session,
    *,
    owner: User,
):
    connection_service = LLMConnectionService(db)
    connection = connection_service.create_draft(
        user_id=owner.id,
        display_name="HF Router",
        connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="huggingface",
    )
    connection_service.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
        target_state=LLMConnectionState.DISABLED,
    )
    return connection_service.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=2,
        target_state=LLMConnectionState.ENABLED,
    )


def _service_context(job_id: str) -> LLMServiceOperationContext:
    return LLMServiceOperationContext.from_job_payload(
        service_actor=LLM_INVENTORY_REFRESH_SERVICE_ACTOR,
        job_id=job_id,
    )
