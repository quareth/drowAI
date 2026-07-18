"""Tests for owner-scoped LLM deployment and route lookup services."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from backend.models import LLMDeploymentRoute, User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.types import LLMDeploymentNotFoundError


def test_deployment_creation_preserves_exact_wire_id_and_owner_scope(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Deployments retain endpoint identity and cannot cross connection owners."""

    owner, other = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Owner Connection",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )
    service = LLMDeploymentService(llm_identity_db)

    deployment = service.create_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="Org/Model-Case:Exact",
        display_name="Exact Model",
        discovery_source="operator",
    )

    assert deployment.wire_model_id == "Org/Model-Case:Exact"
    assert service.get_deployment(
        user_id=owner.id,
        deployment_id=deployment.id,
    ).id == deployment.id
    with pytest.raises(LLMDeploymentNotFoundError):
        service.get_deployment(
            user_id=other.id,
            deployment_id=deployment.id,
        )
    with pytest.raises(LLMDeploymentNotFoundError):
        service.create_deployment(
            user_id=other.id,
            connection_id=connection.id,
            expected_connection_revision=1,
            wire_model_id="foreign",
            display_name="Foreign",
            discovery_source="operator",
        )


def test_route_lookup_reloads_connection_ownership(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Route lookup joins through deployment to its current connection owner."""

    owner, other = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Owner Connection",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )
    service = LLMDeploymentService(llm_identity_db)
    deployment = service.create_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="gpt-5.2",
        display_name="GPT 5.2",
        discovery_source="catalog",
    )
    route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=deployment.id,
        adapter_id="openai_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
        enabled=True,
    )
    llm_identity_db.add(route)
    llm_identity_db.flush()

    assert service.get_route(
        user_id=owner.id,
        route_id=route.id,
    ).id == route.id
    assert service.list_routes(
        user_id=owner.id,
        deployment_id=deployment.id,
    ) == (route,)
    with pytest.raises(LLMDeploymentNotFoundError):
        service.get_route(user_id=other.id, route_id=route.id)
