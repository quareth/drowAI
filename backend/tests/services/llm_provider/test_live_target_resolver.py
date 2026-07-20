"""Direct proof tests for the canonical live LLM target resolver.

Purpose: compare the deployment-aware resolver against the facade delegation
that now composes it.
Scope boundary: these tests cover V2 lookup order, target fields, credentials,
metrics, and exceptions only; they do not exercise legacy compatibility or
provider-client construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
)
from backend.services.llm_provider import LLMRuntimeClientResolver
from backend.services.llm_provider.effective_profile_service import NativeRouteContract
from backend.services.llm_provider.live_target_resolver import LiveLLMTargetResolver
from backend.services.llm_provider import live_target_resolver as live_module
from backend.services.llm_provider.operation_registry import (
    FIXED_PROVIDER_ENDPOINT_POLICY_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.types import (
    AuthorizedLLMConnectionOperation,
    DeploymentRef,
    LLMAuthMode,
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeAccessContext,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedAuth,
)
from backend.services.metrics import utils as metric_utils


class _RecordingCredentialService:
    """Credential double that records safe live resolution inputs."""

    def __init__(self, *, events: list[str]) -> None:
        self.events = events
        self.connection_auth_calls: list[dict[str, Any]] = []

    def resolve_connection_auth(
        self,
        connection_ref: LLMConnectionCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None = None,
        purpose: str,
        auth_mode: LLMAuthMode | str,
    ) -> ResolvedAuth:
        self.events.append("credentials.resolve_connection_auth")
        self.connection_auth_calls.append(
            {
                "connection_ref": connection_ref,
                "runtime_user_id": runtime_user_id,
                "task_id": task_id,
                "purpose": purpose,
                "auth_mode": auth_mode,
            }
        )
        mode = auth_mode if isinstance(auth_mode, LLMAuthMode) else LLMAuthMode(auth_mode)
        return ResolvedAuth.with_secret(
            mode=mode,
            provider="openai",
            secret=ProviderSecret(provider="openai", value="sk-live-proof"),
        )


class _RecordingDeploymentService:
    """Deployment double that records live lookup order."""

    def __init__(
        self,
        *,
        events: list[str],
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
    ) -> None:
        self.events = events
        self.deployment = deployment
        self.route = route

    def get_deployment(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
    ) -> LLMModelDeployment:
        self.events.append("deployment.get")
        return self.deployment

    def select_enabled_route(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
        preferred_route_id: UUID | str | None = None,
    ) -> LLMDeploymentRoute | None:
        self.events.append("deployment.route")
        return self.route


class _RecordingAuthorizer:
    """Authorization double that records access context and optional denial."""

    def __init__(
        self,
        *,
        events: list[str],
        error: LLMConnectionAuthorizationError | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def authorize(
        self,
        *,
        access_context: LLMConnectionAccessContext,
        connection_id: UUID | str,
        expected_revision: int,
        operation: str,
    ) -> AuthorizedLLMConnectionOperation:
        self.events.append("authorizer.authorize")
        self.calls.append(
            {
                "access_context": access_context,
                "connection_id": str(connection_id),
                "expected_revision": expected_revision,
                "operation": operation,
            }
        )
        if self.error is not None:
            raise self.error
        return AuthorizedLLMConnectionOperation(
            connection_id=str(connection_id),
            connection_revision=expected_revision,
            operation_target=ConnectionOperationRegistry().resolve(
                operation,
                provider="openai",
            ),
        )


class _RecordingProfileService:
    """Profile double that records profile and native contract resolution."""

    def __init__(self, *, events: list[str]) -> None:
        self.events = events
        self.profile = require_model_profile(ProviderModelRef("openai", "gpt-5.2"))

    def resolve(
        self,
        *,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
    ):
        self.events.append("profiles.resolve")
        return self.profile

    def native_route_contract(self, _profile) -> NativeRouteContract:
        self.events.append("profiles.contract")
        return NativeRouteContract(
            adapter_id="openai_responses",
            adapter_version="1",
            api_surface="responses",
            dialect_policy_id="openai_responses.native_v1",
        )


def _add_connection(db: Session, *, owner: User) -> LLMInferenceConnection:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name="Live proof connection",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
        serving_operator_id="openai",
        transport_origin="backend",
        endpoint_policy_id=FIXED_PROVIDER_ENDPOINT_POLICY_ID,
        state="enabled",
        revision=1,
        legacy_default_provider="openai",
    )
    db.add(connection)
    db.flush()
    return connection


def _add_deployment(
    db: Session,
    *,
    connection: LLMInferenceConnection,
    wire_model_id: str = "Org/Model-Case:Exact",
) -> LLMModelDeployment:
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id=wire_model_id,
        canonical_model_id="gpt-5.2",
        display_name="Live proof deployment",
        discovery_source="test",
        lifecycle_state="active",
        enabled=True,
        revision=1,
    )
    db.add(deployment)
    db.flush()
    return deployment


def _add_route(db: Session, *, deployment: LLMModelDeployment) -> LLMDeploymentRoute:
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
    return route


def _selection(
    deployment: LLMModelDeployment,
    *,
    route: LLMDeploymentRoute | None = None,
    expected_revision: int = 1,
) -> LLMRuntimeSelectionV2:
    return LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(str(deployment.id), expected_revision),
        preferred_route_id=str(route.id) if route is not None else None,
    )


def _build_resolvers(
    db: Session,
    *,
    deployment: LLMModelDeployment,
    route: LLMDeploymentRoute | None,
    authorization_error: LLMConnectionAuthorizationError | None = None,
):
    facade_events: list[str] = []
    live_events: list[str] = []
    facade_credentials = _RecordingCredentialService(events=facade_events)
    live_credentials = _RecordingCredentialService(events=live_events)
    facade_authorizer = _RecordingAuthorizer(
        events=facade_events,
        error=authorization_error,
    )
    live_authorizer = _RecordingAuthorizer(
        events=live_events,
        error=authorization_error,
    )
    facade = LLMRuntimeClientResolver(
        facade_credentials,
        db=db,
        deployment_service=_RecordingDeploymentService(
            events=facade_events,
            deployment=deployment,
            route=route,
        ),
        connection_authorizer=facade_authorizer,
        effective_profile_service=_RecordingProfileService(events=facade_events),
    )
    live = LiveLLMTargetResolver(
        live_credentials,
        db=db,
        deployment_service=_RecordingDeploymentService(
            events=live_events,
            deployment=deployment,
            route=route,
        ),
        connection_authorizer=live_authorizer,
        effective_profile_service=_RecordingProfileService(events=live_events),
    )
    return (
        facade,
        live,
        facade_events,
        live_events,
        facade_credentials,
        live_credentials,
        facade_authorizer,
        live_authorizer,
    )


def _capture_metric_names(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        metric_utils,
        "safe_inc",
        lambda name, value=1: calls.append((name, value)),
    )
    return calls


def test_live_target_resolver_matches_facade_success_fields_and_event_order(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """The extracted live resolver reproduces facade V2 success behavior exactly."""

    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    route = _add_route(llm_identity_db, deployment=deployment)
    (
        facade,
        live,
        facade_events,
        live_events,
        facade_credentials,
        live_credentials,
        facade_authorizer,
        live_authorizer,
    ) = _build_resolvers(llm_identity_db, deployment=deployment, route=route)
    access = LLMRuntimeAccessContext(
        runtime_user_id=owner.id,
        task_id=23,
        tenant_id=29,
    )

    facade_target = facade.resolve_target(
        _selection(deployment, route=route),
        access_context=access,
        purpose="chat",
        target=ProviderModelRef("openai", "Org/Model-Case:Exact"),
    )
    live_target = live.resolve_target(
        _selection(deployment, route=route),
        access_context=access,
        purpose="chat",
        target=ProviderModelRef("openai", "Org/Model-Case:Exact"),
    )

    assert live_target == facade_target
    assert live_events == facade_events == [
        "deployment.get",
        "deployment.route",
        "profiles.resolve",
        "authorizer.authorize",
        "credentials.resolve_connection_auth",
        "profiles.contract",
    ]
    assert live_credentials.connection_auth_calls == facade_credentials.connection_auth_calls
    assert live_credentials.connection_auth_calls == [
        {
            "connection_ref": LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=1,
            ),
            "runtime_user_id": owner.id,
            "task_id": 23,
            "purpose": "chat",
            "auth_mode": LLMAuthMode.API_KEY,
        }
    ]
    assert live_authorizer.calls == facade_authorizer.calls == [
        {
            "access_context": LLMConnectionAccessContext(
                authenticated_user_id=owner.id,
                task_id=23,
                tenant_id=29,
            ),
            "connection_id": str(connection.id),
            "expected_revision": 1,
            "operation": "inference",
        }
    ]
    assert "sk-live-proof" not in repr(live_events)


def test_live_target_resolver_matches_facade_revision_failure_metrics(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale deployment failure type, message, event order, and metrics match."""

    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    (
        facade,
        live,
        facade_events,
        live_events,
        *_rest,
    ) = _build_resolvers(llm_identity_db, deployment=deployment, route=None)
    facade_metrics = _capture_metric_names(monkeypatch)
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)
    selection = _selection(deployment, expected_revision=2)

    with pytest.raises(LLMDeploymentNotFoundError) as facade_error:
        facade.resolve_target(selection, access_context=access, purpose="chat")
    live_metrics = _capture_metric_names(monkeypatch)
    with pytest.raises(LLMDeploymentNotFoundError) as live_error:
        live.resolve_target(selection, access_context=access, purpose="chat")

    assert str(live_error.value) == str(facade_error.value)
    assert live_events == facade_events == ["deployment.get"]
    assert live_metrics == facade_metrics == [
        (
            (
                "llm_provider.deployment_resolution.total"
                f".deployment_id.{deployment.id}.status.stale_revision"
            ),
            1,
        )
    ]


def test_live_target_resolver_matches_facade_authorization_failure_order_and_metrics(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization denial occurs before credential resolution in both paths."""

    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    route = _add_route(llm_identity_db, deployment=deployment)
    denial = LLMConnectionAuthorizationError(
        code="stale_connection_revision",
        message="Connection revision is stale",
    )
    (
        facade,
        live,
        facade_events,
        live_events,
        facade_credentials,
        live_credentials,
        *_rest,
    ) = _build_resolvers(
        llm_identity_db,
        deployment=deployment,
        route=route,
        authorization_error=denial,
    )
    facade_metrics = _capture_metric_names(monkeypatch)
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)

    with pytest.raises(LLMConnectionAuthorizationError) as facade_error:
        facade.resolve_target(
            _selection(deployment, route=route),
            access_context=access,
            purpose="chat",
        )
    live_metrics = _capture_metric_names(monkeypatch)
    with pytest.raises(LLMConnectionAuthorizationError) as live_error:
        live.resolve_target(
            _selection(deployment, route=route),
            access_context=access,
            purpose="chat",
        )

    assert str(live_error.value) == str(facade_error.value)
    assert live_events == facade_events == [
        "deployment.get",
        "deployment.route",
        "profiles.resolve",
        "authorizer.authorize",
    ]
    assert live_credentials.connection_auth_calls == []
    assert facade_credentials.connection_auth_calls == []
    assert live_metrics == facade_metrics == [
        (
            (
                "llm_provider.deployment_resolution.total"
                f".connection_id.{connection.id}"
                f".deployment_id.{deployment.id}"
                ".status.connection_revision_conflict"
            ),
            1,
        )
    ]


def test_live_target_resolver_module_has_no_facade_or_legacy_dependency() -> None:
    """The extracted collaborator does not import facade or compatibility owners."""

    module_file = live_module.__file__
    assert module_file is not None
    source = Path(module_file).read_text(encoding="utf-8")

    assert "runtime_client_resolver" not in source
    assert "legacy_target_resolver" not in source
