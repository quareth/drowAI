"""Tests for live deployment-aware runtime target resolution."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import ProviderModelRef
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    Task,
    Tenant,
    User,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.migration_service import (
    deterministic_legacy_connection_id,
)
from backend.services.llm_provider.operation_registry import OPENAI_BASE_URL_ENV
from backend.services.llm_provider.runtime_client_resolver import (
    BudgetEnforcingLLMClient,
    LLMRuntimeClientResolver,
)
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMConnectionAuthorizationError,
    LLMConnectionState,
    LLMCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
)


def _deployment_runtime_fixture(
    db: Session,
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "backend.services.llm_provider.credential_service.encrypt_api_key",
        lambda value: f"encrypted:{value}",
    )
    monkeypatch.setattr(
        "backend.services.llm_provider.credential_service.decrypt_api_key",
        lambda value: value.removeprefix("encrypted:"),
    )
    credentials = LLMCredentialService(db)
    credentials.upsert_api_key(
        user_id=owner.id,
        provider="openai",
        api_key="sk-deployment",
    )
    connection_id = deterministic_legacy_connection_id(owner.id, "openai")
    deployment = LLMDeploymentService(db).create_deployment(
        user_id=owner.id,
        connection_id=connection_id,
        expected_connection_revision=1,
        wire_model_id="Org/Model-Case:Exact",
        canonical_model_id="gpt-5.2",
        display_name="Exact deployment",
        discovery_source="operator",
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
    db.add(route)
    db.flush()
    return credentials, deployment, route


def test_v2_resolver_normalizes_live_target_through_factory_and_budget(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V2 resolution authorizes identity, composes profile, and keeps budgeting."""

    monkeypatch.setenv(OPENAI_BASE_URL_ENV, "http://127.0.0.1:4100/v1")
    owner, _ = identity_users
    llm_identity_db.add(Tenant(id=41, slug="runtime", name="Runtime"))
    llm_identity_db.flush()
    task = Task(user_id=owner.id, tenant_id=41, name="Runtime task")
    llm_identity_db.add(task)
    llm_identity_db.flush()
    credentials, deployment, route = _deployment_runtime_fixture(
        llm_identity_db,
        owner,
        monkeypatch,
    )
    selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(
            deployment_id=str(deployment.id),
            expected_revision=1,
        ),
        preferred_route_id=str(route.id),
        reasoning_effort="high",
        legacy_provider="openai",
        legacy_model="Org/Model-Case:Exact",
    )
    access = LLMRuntimeAccessContext(
        runtime_user_id=owner.id,
        task_id=task.id,
        tenant_id=41,
    )
    calls: list[dict[str, Any]] = []

    def fake_get_client(**kwargs: Any) -> object:
        calls.append(dict(kwargs))
        return object()

    monkeypatch.setattr(
        "backend.services.llm_provider.runtime_client_resolver.LLMClientFactory.get_client",
        fake_get_client,
    )
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)

    target = resolver.resolve_target(
        selection,
        access_context=access,
        purpose="chat",
    )
    client = resolver.get_client(
        selection,
        access_context=access,
        purpose="chat",
    )

    assert target.connection.connection_id == str(
        deterministic_legacy_connection_id(owner.id, "openai")
    )
    assert target.exact_wire_model_id == "Org/Model-Case:Exact"
    assert target.effective_profile.ref == ProviderModelRef("openai", "gpt-5.2")
    assert target.connection.resolved_auth.secret is not None
    assert target.connection.resolved_auth.secret.value == "sk-deployment"
    assert not hasattr(target, "to_dict")
    assert isinstance(client, BudgetEnforcingLLMClient)
    assert calls[0]["provider_model"] == ProviderModelRef(
        "openai",
        "Org/Model-Case:Exact",
    )
    assert calls[0]["api_key"] == "sk-deployment"
    assert calls[0]["model_profile"] is target.effective_profile
    assert calls[0]["base_url"] == "http://127.0.0.1:4100/v1"


def test_legacy_selection_normalizes_to_same_persisted_target(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy provider/model identity enters the same live target resolver."""

    owner, _ = identity_users
    credentials, deployment, _ = _deployment_runtime_fixture(
        llm_identity_db,
        owner,
        monkeypatch,
    )
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)
    legacy = LLMRuntimeSelection(
        provider="openai",
        model="Org/Model-Case:Exact",
        credential_ref=LLMCredentialRef(user_id=owner.id, provider="openai"),
    )

    target = resolver.resolve_target(
        legacy,
        access_context=LLMRuntimeAccessContext(runtime_user_id=owner.id),
        purpose="legacy",
    )

    assert target.deployment_id == str(deployment.id)
    assert target.exact_wire_model_id == "Org/Model-Case:Exact"


def test_v2_resolution_fails_closed_on_revision_state_and_untrusted_context(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale deployment, disabled connection, and mapping context are rejected."""

    owner, _ = identity_users
    credentials, deployment, route = _deployment_runtime_fixture(
        llm_identity_db,
        owner,
        monkeypatch,
    )
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)
    selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(str(deployment.id), 1),
        preferred_route_id=str(route.id),
    )
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)

    with pytest.raises(TypeError):
        resolver.resolve_target(
            selection,
            access_context={"runtime_user_id": owner.id},  # type: ignore[arg-type]
            purpose="untrusted",
        )

    deployment.revision = 2
    llm_identity_db.flush()
    with pytest.raises(LLMDeploymentNotFoundError):
        resolver.resolve_target(selection, access_context=access, purpose="stale")

    deployment.revision = 1
    connection = llm_identity_db.get(
        LLMInferenceConnection,
        deployment.connection_id,
    )
    assert connection is not None
    LLMConnectionService(llm_identity_db).transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=connection.revision,
        target_state=LLMConnectionState.DISABLED,
    )
    with pytest.raises(LLMConnectionAuthorizationError):
        resolver.resolve_target(selection, access_context=access, purpose="disabled")
