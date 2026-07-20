"""Characterize the runtime client resolver boundary locked before extraction.

Scope: lock the public facade, live deployment resolution, and legacy
compatibility outcomes. The tests intentionally avoid asserting future module
internals and keep secret values inside request-scoped fakes only.
"""

from __future__ import annotations

import inspect
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    Task,
    Tenant,
    User,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.effective_profile_service import NativeRouteContract
from backend.services.llm_provider.migration_service import (
    deterministic_legacy_connection_id,
)
from backend.services.llm_provider.operation_registry import (
    FIXED_PROVIDER_ENDPOINT_POLICY_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider import legacy_target_resolver as legacy_module
from backend.services.llm_provider import live_target_resolver as live_module
from backend.services.llm_provider import runtime_client_resolver as resolver_module
from backend.services.llm_provider.runtime_client_resolver import (
    LLMRuntimeClientResolver,
    resolve_call_reasoning_effort,
    resolve_call_target,
)
from backend.services.llm_provider.types import (
    AuthorizedLLMConnectionOperation,
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionAccessContext,
    LLMConnectionAuthorizationError,
    LLMConnectionCredentialRef,
    LLMCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)

_LEGACY_NAMESPACE = UUID("24359013-a580-474c-933e-ddd1a2e78c92")


class _RecordingCredentialService:
    """Credential double that records non-secret resolver inputs."""

    def __init__(self, provider: str = "openai") -> None:
        self.provider = provider
        self.connection_auth_calls: list[dict[str, Any]] = []
        self.secret_calls: list[dict[str, Any]] = []
        self.credential_ref_calls: list[dict[str, Any]] = []

    def resolve_connection_auth(
        self,
        connection_ref: LLMConnectionCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None = None,
        purpose: str,
        auth_mode: LLMAuthMode | str,
    ) -> ResolvedAuth:
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
            provider=self.provider,
            secret=ProviderSecret(provider=self.provider, value="sk-boundary-secret"),
        )

    def resolve_secret(
        self,
        credential_ref: LLMCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None,
        purpose: str,
    ) -> ProviderSecret:
        self.secret_calls.append(
            {
                "credential_ref": credential_ref,
                "runtime_user_id": runtime_user_id,
                "task_id": task_id,
                "purpose": purpose,
            }
        )
        return ProviderSecret(
            provider=credential_ref.provider,
            value=f"sk-{credential_ref.provider}-detached",
        )

    def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        self.credential_ref_calls.append({"user_id": user_id, "provider": provider})
        return LLMCredentialRef(user_id=user_id, provider=provider)


class _RecordingDeploymentService:
    """Deployment double that records live resolver lookup order."""

    def __init__(
        self,
        *,
        events: list[str],
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
        route_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.deployment = deployment
        self.route = route
        self.route_error = route_error
        self.get_calls: list[dict[str, Any]] = []
        self.route_calls: list[dict[str, Any]] = []

    def get_deployment(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
    ) -> LLMModelDeployment:
        self.events.append("deployment.get")
        self.get_calls.append({"user_id": user_id, "deployment_id": str(deployment_id)})
        return self.deployment

    def select_enabled_route(
        self,
        *,
        user_id: int,
        deployment_id: UUID | str,
        preferred_route_id: UUID | str | None = None,
    ) -> LLMDeploymentRoute | None:
        self.events.append("deployment.route")
        self.route_calls.append(
            {
                "user_id": user_id,
                "deployment_id": str(deployment_id),
                "preferred_route_id": str(preferred_route_id)
                if preferred_route_id is not None
                else None,
            }
        )
        if self.route_error is not None:
            raise self.route_error
        return self.route


class _RecordingAuthorizer:
    """Authorization double that records resolved live access context."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        error: LLMConnectionAuthorizationError | None = None,
    ) -> None:
        self.events = events if events is not None else []
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
    """Effective-profile double that records profile and contract resolution."""

    def __init__(self, *, events: list[str]) -> None:
        self.events = events
        self.profile = require_model_profile(ProviderModelRef("openai", "gpt-5.2"))
        self.resolve_calls: list[dict[str, Any]] = []

    def resolve(
        self,
        *,
        connection: LLMInferenceConnection,
        deployment: LLMModelDeployment,
        route: LLMDeploymentRoute | None,
    ):
        self.events.append("profiles.resolve")
        self.resolve_calls.append(
            {
                "connection_id": str(connection.id),
                "deployment_id": str(deployment.id),
                "route_id": str(route.id) if route is not None else None,
            }
        )
        return self.profile

    def native_route_contract(self, _profile) -> NativeRouteContract:
        self.events.append("profiles.contract")
        return NativeRouteContract(
            adapter_id="openai_responses",
            adapter_version="1",
            api_surface="responses",
            dialect_policy_id="openai_responses.native_v1",
        )


def _capture_metrics(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str], int]]:
    calls: list[tuple[str, dict[str, str], int]] = []
    for module in (legacy_module, live_module):
        monkeypatch.setattr(
            module,
            "safe_inc_labeled",
            lambda name, labels, value=1: calls.append((name, dict(labels), value)),
        )
    return calls


def _add_connection(
    db: Session,
    *,
    owner: User,
    provider: str = "openai",
    legacy_default_provider: str | None = "openai",
) -> LLMInferenceConnection:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name=f"{provider} connection",
        connection_preset_id=provider,
        runtime_family_id=f"{provider}_native",
        serving_operator_id=provider,
        transport_origin="backend",
        endpoint_policy_id=FIXED_PROVIDER_ENDPOINT_POLICY_ID,
        state="enabled",
        revision=1,
        legacy_default_provider=legacy_default_provider,
    )
    db.add(connection)
    db.flush()
    return connection


def _add_deployment(
    db: Session,
    *,
    connection: LLMInferenceConnection,
    wire_model_id: str = "gpt-5.2",
    canonical_model_id: str | None = "gpt-5.2",
    enabled: bool = True,
    lifecycle_state: str = "active",
) -> LLMModelDeployment:
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id=wire_model_id,
        canonical_model_id=canonical_model_id,
        display_name="Boundary deployment",
        discovery_source="test",
        lifecycle_state=lifecycle_state,
        enabled=enabled,
        revision=1,
    )
    db.add(deployment)
    db.flush()
    return deployment


def _add_route(
    db: Session,
    *,
    deployment: LLMModelDeployment,
    enabled: bool = True,
) -> LLMDeploymentRoute:
    route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=deployment.id,
        adapter_id="openai_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
        enabled=enabled,
    )
    db.add(route)
    db.flush()
    return route


def _legacy_selection(
    owner: User,
    *,
    provider: str = "openai",
    model: str = "gpt-5.2",
) -> LLMRuntimeSelection:
    return LLMRuntimeSelection(
        provider=provider,
        model=model,
        credential_ref=LLMCredentialRef(user_id=owner.id, provider=provider),
    )


def _v2_selection(
    deployment: LLMModelDeployment,
    *,
    route: LLMDeploymentRoute | None = None,
    expected_revision: int = 1,
) -> LLMRuntimeSelectionV2:
    return LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(str(deployment.id), expected_revision),
        preferred_route_id=str(route.id) if route is not None else None,
    )


def _assert_no_secret_metric_values(calls: list[tuple[str, dict[str, str], int]]) -> None:
    serialized = repr(calls)
    assert "sk-" not in serialized
    assert "secret" not in serialized.lower()


def test_facade_signatures_and_public_helpers_preserve_runtime_contract() -> None:
    """Public resolver methods and helpers stay importable with current signatures."""

    constructor = inspect.signature(LLMRuntimeClientResolver)
    assert list(constructor.parameters) == [
        "credential_service",
        "db",
        "deployment_service",
        "connection_authorizer",
        "effective_profile_service",
    ]

    get_client = inspect.signature(LLMRuntimeClientResolver.get_client)
    assert list(get_client.parameters) == [
        "self",
        "selection",
        "target",
        "access_context",
        "runtime_user_id",
        "task_id",
        "tenant_id",
        "purpose",
        "client_kwargs",
    ]
    assert get_client.parameters["target"].kind is inspect.Parameter.KEYWORD_ONLY
    assert get_client.parameters["client_kwargs"].kind is inspect.Parameter.VAR_KEYWORD

    assert list(inspect.signature(LLMRuntimeClientResolver.resolve_target).parameters) == [
        "self",
        "selection",
        "access_context",
        "purpose",
        "target",
    ]
    assert list(inspect.signature(LLMRuntimeClientResolver.resolve_secret).parameters) == [
        "self",
        "selection",
        "runtime_user_id",
        "task_id",
        "purpose",
    ]
    assert list(inspect.signature(LLMRuntimeClientResolver.get_credential_ref).parameters) == [
        "self",
        "user_id",
        "provider",
    ]

    selection = {
        "provider": "OpenAI",
        "model": "GPT-5.2",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "reasoning_effort": "HIGH",
    }
    call_target = LLMCallTarget(
        provider="Anthropic",
        model="Claude-Sonnet-4-6",
        reasoning_effort="low",
    )

    assert resolve_call_target(selection) == ProviderModelRef("OpenAI", "GPT-5.2")
    assert resolve_call_target(selection, call_target) == ProviderModelRef(
        "anthropic",
        "claude-sonnet-4-6",
    )
    assert resolve_call_reasoning_effort(selection) == "HIGH"
    assert resolve_call_reasoning_effort(selection, call_target) == "low"
    assert "resolve_call_target" in resolver_module.__all__
    assert "resolve_call_reasoning_effort" in resolver_module.__all__


def test_facade_validates_selection_and_trusted_context_before_resolution(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selection parsing and trusted runtime identity are checked before live work."""

    owner, _ = identity_users
    credential_service = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(credential_service, db=llm_identity_db)
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    selection = _v2_selection(deployment)

    with pytest.raises(TypeError, match="access_context must be LLMRuntimeAccessContext"):
        resolver.resolve_target(
            selection,
            access_context={"runtime_user_id": owner.id},  # type: ignore[arg-type]
            purpose="boundary",
        )

    with pytest.raises(TypeError, match="Runtime selection requires"):
        resolver.resolve_target(
            object(),  # type: ignore[arg-type]
            access_context=LLMRuntimeAccessContext(runtime_user_id=owner.id),
            purpose="boundary",
        )

    captured: list[LLMRuntimeAccessContext] = []

    def fake_resolve_target(self, _selection, *, access_context, purpose, target=None):
        captured.append(access_context)
        raise RuntimeError("stop after trusted context")

    monkeypatch.setattr(
        LLMRuntimeClientResolver,
        "resolve_target",
        fake_resolve_target,
    )
    llm_identity_db.add(Tenant(id=81, slug="boundary", name="Boundary"))
    llm_identity_db.flush()
    task = Task(user_id=owner.id, tenant_id=81, name="Boundary task")
    llm_identity_db.add(task)
    llm_identity_db.flush()

    with pytest.raises(RuntimeError, match="stop after trusted context"):
        resolver.get_client(
            selection,
            runtime_user_id=owner.id,
            task_id=task.id,
            purpose="boundary",
        )
    assert captured == [
        LLMRuntimeAccessContext(
            runtime_user_id=owner.id,
            task_id=task.id,
            tenant_id=81,
        )
    ]

    with pytest.raises(LLMConfigurationError, match="Conflicting runtime user identity"):
        resolver.get_client(
            selection,
            access_context=LLMRuntimeAccessContext(runtime_user_id=owner.id),
            runtime_user_id=owner.id + 1,
            purpose="boundary",
        )

    with pytest.raises(LLMConfigurationError, match="Runtime task identity is unavailable"):
        resolver.get_client(
            selection,
            runtime_user_id=owner.id,
            task_id=task.id + 1000,
            purpose="boundary",
        )

    assert credential_service.connection_auth_calls == []
    assert credential_service.secret_calls == []


def test_facade_mapping_discriminator_preserves_current_resolution_paths(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapping payload discrimination stays at the facade boundary."""

    owner, _ = identity_users
    credential_service = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(credential_service, db=llm_identity_db)
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)
    v2_payload = _v2_selection(deployment).to_dict()
    deployment_ref_payload = dict(v2_payload)
    deployment_ref_payload.pop("schema_version")
    legacy_payload = _legacy_selection(owner).to_dict()

    parse_calls: list[str] = []
    branch_calls: list[
        tuple[str, object, ProviderModelRef | None, str, ProviderModelRef | None]
    ] = []
    get_client_handoffs: list[str] = []
    original_v2_from_mapping = LLMRuntimeSelectionV2.from_mapping
    original_legacy_from_mapping = LLMRuntimeSelection.from_mapping

    def spy_v2_from_mapping(cls, value):
        parse_calls.append("v2")
        return original_v2_from_mapping(value)

    def spy_legacy_from_mapping(cls, value):
        parse_calls.append("legacy")
        return original_legacy_from_mapping(value)

    def fake_resolve_v2(selection, *, access_context, purpose, target=None):
        branch_calls.append(("v2", selection, None, purpose, target))
        raise RuntimeError("v2 resolver path")

    def fake_resolve_legacy(
        selection,
        *,
        call_ref,
        access_context,
        purpose,
        target=None,
    ):
        branch_calls.append(("legacy", selection, call_ref, purpose, target))
        raise RuntimeError("legacy resolver path")

    monkeypatch.setattr(
        LLMRuntimeSelectionV2,
        "from_mapping",
        classmethod(spy_v2_from_mapping),
    )
    monkeypatch.setattr(
        LLMRuntimeSelection,
        "from_mapping",
        classmethod(spy_legacy_from_mapping),
    )
    monkeypatch.setattr(resolver._live_resolver, "resolve_target", fake_resolve_v2)
    monkeypatch.setattr(resolver._legacy_resolver, "resolve", fake_resolve_legacy)

    with pytest.raises(RuntimeError, match="v2 resolver path"):
        resolver.resolve_target(v2_payload, access_context=access, purpose="schema-v2")

    with pytest.raises(ValueError, match="schema_version must be 2"):
        resolver.resolve_target(
            deployment_ref_payload,
            access_context=access,
            purpose="deployment-ref",
        )

    target = ProviderModelRef("openai", "gpt-5.2")
    with pytest.raises(RuntimeError, match="legacy resolver path"):
        resolver.resolve_target(
            legacy_payload,
            access_context=access,
            purpose="legacy",
            target=target,
        )

    assert parse_calls == ["v2", "v2", "legacy", "legacy"]
    assert len(branch_calls) == 2
    assert branch_calls[0] == (
        "v2",
        LLMRuntimeSelectionV2.from_mapping(v2_payload),
        None,
        "schema-v2",
        None,
    )
    assert branch_calls[1] == (
        "legacy",
        LLMRuntimeSelection.from_mapping(legacy_payload),
        target,
        "legacy",
        target,
    )

    def fake_resolve_target(self, selection, *, access_context, purpose, target=None):
        get_client_handoffs.append(type(selection).__name__)
        raise RuntimeError("get_client parsed selection")

    monkeypatch.setattr(LLMRuntimeClientResolver, "resolve_target", fake_resolve_target)

    with pytest.raises(RuntimeError, match="get_client parsed selection"):
        resolver.get_client(v2_payload, runtime_user_id=owner.id, purpose="v2-client")

    with pytest.raises(RuntimeError, match="get_client parsed selection"):
        resolver.get_client(
            legacy_payload,
            runtime_user_id=owner.id,
            purpose="legacy-client",
        )

    assert get_client_handoffs == ["LLMRuntimeSelectionV2", "LLMRuntimeSelection"]
    assert credential_service.connection_auth_calls == []
    assert credential_service.secret_calls == []


def test_v2_success_preserves_call_order_and_resolved_target_fields(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Live deployment resolution order and target fields are observable contracts."""

    owner, _ = identity_users
    events: list[str] = []
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="Org/Model-Case:Exact",
    )
    route = _add_route(llm_identity_db, deployment=deployment)
    deployments = _RecordingDeploymentService(
        events=events,
        deployment=deployment,
        route=route,
    )
    authorizer = _RecordingAuthorizer(events=events)
    credentials = _RecordingCredentialService()
    profiles = _RecordingProfileService(events=events)
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=deployments,
        connection_authorizer=authorizer,
        effective_profile_service=profiles,
    )

    target = resolver.resolve_target(
        _v2_selection(deployment, route=route),
        access_context=LLMRuntimeAccessContext(
            runtime_user_id=owner.id,
            task_id=23,
            tenant_id=29,
        ),
        purpose="chat",
    )

    assert events == [
        "deployment.get",
        "deployment.route",
        "profiles.resolve",
        "authorizer.authorize",
        "profiles.contract",
    ]
    assert deployments.get_calls == [
        {"user_id": owner.id, "deployment_id": str(deployment.id)}
    ]
    assert deployments.route_calls == [
        {
            "user_id": owner.id,
            "deployment_id": str(deployment.id),
            "preferred_route_id": str(route.id),
        }
    ]
    assert authorizer.calls == [
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
    assert credentials.connection_auth_calls == [
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

    assert target.connection.connection_id == str(connection.id)
    assert target.connection.connection_revision == 1
    assert target.connection.connection_preset_id == "openai"
    assert target.connection.runtime_family_id == "openai_native"
    assert target.connection.serving_operator_id == "openai"
    assert target.connection.transport_origin == "backend"
    assert target.connection.endpoint_policy_id == FIXED_PROVIDER_ENDPOINT_POLICY_ID
    assert target.connection.resolved_auth.secret is not None
    assert target.connection.resolved_auth.secret.value == "sk-boundary-secret"
    assert target.deployment_id == str(deployment.id)
    assert target.deployment_revision == 1
    assert target.route_id == str(route.id)
    assert target.adapter_id == "openai_responses"
    assert target.adapter_version == "1"
    assert target.api_surface == "responses"
    assert target.dialect_policy_id == "openai_responses.native_v1"
    assert target.canonical_model_id == "gpt-5.2"
    assert target.exact_wire_model_id == "Org/Model-Case:Exact"
    assert target.effective_profile is profiles.profile


def test_v2_failures_preserve_exceptions_and_safe_metrics(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V2 resolution fails closed with current exception and metric labels."""

    calls = _capture_metrics(monkeypatch)
    owner, _ = identity_users
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)
    credentials = _RecordingCredentialService()

    missing_db_resolver = LLMRuntimeClientResolver(credentials)
    with pytest.raises(LLMConfigurationError, match="database is unavailable"):
        missing_db_resolver.resolve_target(
            LLMRuntimeSelectionV2(
                deployment_ref=DeploymentRef(str(uuid4()), 1),
            ),
            access_context=access,
            purpose="chat",
        )

    connection = _add_connection(llm_identity_db, owner=owner)
    stale = _add_deployment(llm_identity_db, connection=connection)
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=stale,
            route=None,
        ),
        connection_authorizer=_RecordingAuthorizer(),
    )
    with pytest.raises(LLMDeploymentNotFoundError, match="revision is unavailable"):
        resolver.resolve_target(
            _v2_selection(stale, expected_revision=99),
            access_context=access,
            purpose="chat",
        )
    assert (
        "llm_provider.deployment_resolution.total",
        {"status": "stale_revision", "deployment_id": str(stale.id)},
        1,
    ) in calls

    disabled = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="disabled-model",
        enabled=False,
    )
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=disabled,
            route=None,
        ),
        connection_authorizer=_RecordingAuthorizer(),
    )
    with pytest.raises(LLMDeploymentNotFoundError, match="Deployment is unavailable"):
        resolver.resolve_target(_v2_selection(disabled), access_context=access, purpose="chat")
    assert (
        "llm_provider.deployment_resolution.total",
        {"status": "deployment_unavailable", "deployment_id": str(disabled.id)},
        1,
    ) in calls

    missing_connection = LLMModelDeployment(
        id=uuid4(),
        connection_id=uuid4(),
        wire_model_id="gpt-5.2",
        canonical_model_id="gpt-5.2",
        display_name="Missing connection",
        discovery_source="test",
        enabled=True,
        lifecycle_state="active",
        revision=1,
    )
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=missing_connection,
            route=None,
        ),
        connection_authorizer=_RecordingAuthorizer(),
    )
    with pytest.raises(LLMDeploymentNotFoundError, match="connection was not found"):
        resolver.resolve_target(
            _v2_selection(missing_connection),
            access_context=access,
            purpose="chat",
        )
    assert (
        "llm_provider.deployment_resolution.total",
        {"status": "connection_unavailable", "deployment_id": str(missing_connection.id)},
        1,
    ) in calls

    route_failure = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="route-failure-model",
    )
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=route_failure,
            route=None,
            route_error=LLMDeploymentNotFoundError("Preferred deployment route is unavailable"),
        ),
        connection_authorizer=_RecordingAuthorizer(),
    )
    with pytest.raises(LLMDeploymentNotFoundError, match="Preferred deployment route"):
        resolver.resolve_target(
            _v2_selection(route_failure),
            access_context=access,
            purpose="chat",
        )

    route = _add_route(llm_identity_db, deployment=route_failure)
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=route_failure,
            route=route,
        ),
        connection_authorizer=_RecordingAuthorizer(),
        effective_profile_service=_RecordingProfileService(events=[]),
    )
    with pytest.raises(LLMDeploymentNotFoundError, match="Call target does not match"):
        resolver.resolve_target(
            _v2_selection(route_failure, route=route),
            access_context=access,
            purpose="chat",
            target=ProviderModelRef("anthropic", "claude-sonnet-4-6"),
        )

    denied = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="denied-model",
    )
    denied_route = _add_route(llm_identity_db, deployment=denied)
    denial = LLMConnectionAuthorizationError(
        code="stale_connection_revision",
        message="Connection revision is stale",
    )
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=denied,
            route=denied_route,
        ),
        connection_authorizer=_RecordingAuthorizer(error=denial),
        effective_profile_service=_RecordingProfileService(events=[]),
    )
    with pytest.raises(LLMConnectionAuthorizationError, match="Connection revision is stale"):
        resolver.resolve_target(
            _v2_selection(denied, route=denied_route),
            access_context=access,
            purpose="credential-purpose",
        )
    assert (
        "llm_provider.deployment_resolution.total",
        {
            "status": "connection_revision_conflict",
            "deployment_id": str(denied.id),
            "connection_id": str(connection.id),
        },
        1,
    ) in calls
    _assert_no_secret_metric_values(calls)


def test_v2_credential_purpose_is_used_after_authorization(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Credential resolution receives the authorized connection and caller purpose."""

    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    route = _add_route(llm_identity_db, deployment=deployment)
    credentials = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        deployment_service=_RecordingDeploymentService(
            events=[],
            deployment=deployment,
            route=route,
        ),
        connection_authorizer=_RecordingAuthorizer(),
        effective_profile_service=_RecordingProfileService(events=[]),
    )

    resolver.resolve_target(
        _v2_selection(deployment, route=route),
        access_context=LLMRuntimeAccessContext(
            runtime_user_id=owner.id,
            task_id=31,
            tenant_id=41,
        ),
        purpose="summarize",
    )

    assert credentials.connection_auth_calls == [
        {
            "connection_ref": LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=1,
            ),
            "runtime_user_id": owner.id,
            "task_id": 31,
            "purpose": "summarize",
            "auth_mode": LLMAuthMode.API_KEY,
        }
    ]
    assert credentials.secret_calls == []


def test_legacy_mapped_resolution_preserves_delegation_inputs_and_metrics(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped legacy identity delegates to V2 selection with current fields."""

    calls = _capture_metrics(monkeypatch)
    owner, _ = identity_users
    connection_id = deterministic_legacy_connection_id(owner.id, "openai")
    connection = LLMInferenceConnection(
        id=connection_id,
        user_id=owner.id,
        display_name="Mapped OpenAI",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
        serving_operator_id="openai",
        transport_origin="backend",
        endpoint_policy_id=FIXED_PROVIDER_ENDPOINT_POLICY_ID,
        state="enabled",
        revision=1,
        legacy_default_provider="openai",
    )
    llm_identity_db.add(connection)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    route = _add_route(llm_identity_db, deployment=deployment)
    credentials = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)
    delegated: list[dict[str, Any]] = []
    sentinel = ResolvedLLMTarget(
        connection=ResolvedConnectionTarget(
            connection_id=str(connection.id),
            connection_revision=1,
            connection_preset_id="openai",
            runtime_family_id="openai_native",
            serving_operator_id="openai",
            transport_origin="backend",
            endpoint_policy_id=FIXED_PROVIDER_ENDPOINT_POLICY_ID,
            endpoint="https://api.openai.com/v1/chat/completions",
            operation_target=ConnectionOperationRegistry().resolve(
                "inference",
                provider="openai",
            ),
            resolved_auth=ResolvedAuth.with_secret(
                mode=LLMAuthMode.API_KEY,
                provider="openai",
                secret=ProviderSecret(provider="openai", value="sk-sentinel"),
            ),
        ),
        deployment_id=str(deployment.id),
        deployment_revision=1,
        route_id=str(route.id),
        adapter_id="openai_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
        canonical_model_id="gpt-5.2",
        exact_wire_model_id="gpt-5.2",
        effective_profile=require_model_profile(ProviderModelRef("openai", "gpt-5.2")),
    )

    def fake_v2(selection, *, access_context, purpose, target):
        delegated.append(
            {
                "selection": selection,
                "access_context": access_context,
                "purpose": purpose,
                "target": target,
            }
        )
        return sentinel

    monkeypatch.setattr(resolver._live_resolver, "resolve_target", fake_v2)

    result = resolver.resolve_target(
        LLMRuntimeSelection(
            provider="openai",
            model="gpt-5.2",
            credential_ref=LLMCredentialRef(user_id=owner.id, provider="openai"),
            reasoning_effort="high",
        ),
        access_context=access,
        purpose="legacy-chat",
        target=LLMCallTarget(provider="openai", model="gpt-5.2", role="chat"),
    )

    assert result is sentinel
    assert delegated == [
        {
            "selection": LLMRuntimeSelectionV2(
                deployment_ref=DeploymentRef(str(deployment.id), 1),
                reasoning_effort="high",
                legacy_provider="openai",
                legacy_model="gpt-5.2",
            ),
            "access_context": access,
            "purpose": "legacy-chat",
            "target": LLMCallTarget(provider="openai", model="gpt-5.2", role="chat"),
        }
    ]
    assert (
        "llm_provider.legacy_identity_read.total",
        {"status": "mapped", "deployment_id": str(deployment.id)},
        1,
    ) in calls
    _assert_no_secret_metric_values(calls)


def test_legacy_live_unmapped_outputs_are_deterministic_for_known_and_unknown_profiles(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live unmapped legacy resolution keeps deterministic ids and safe metrics."""

    calls = _capture_metrics(monkeypatch)
    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    credentials = _RecordingCredentialService()
    authorizer = _RecordingAuthorizer()
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        connection_authorizer=authorizer,
    )
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id, task_id=11, tenant_id=22)

    known = resolver.resolve_target(
        _legacy_selection(owner, model="gpt-5.2"),
        access_context=access,
        purpose="known-live",
    )
    unknown = resolver.resolve_target(
        _legacy_selection(owner, model="private-model"),
        access_context=access,
        purpose="unknown-live",
    )

    assert known.deployment_id == str(
        uuid5(_LEGACY_NAMESPACE, f"live:{connection.id}:gpt-5.2")
    )
    assert known.deployment_revision == 1
    assert known.connection.connection_id == str(connection.id)
    assert known.canonical_model_id == "gpt-5.2"
    assert known.effective_profile == require_model_profile(
        ProviderModelRef("openai", "gpt-5.2")
    )
    assert known.adapter_id == "openai_responses"
    assert known.api_surface == "responses"
    assert known.dialect_policy_id == "openai_responses.native_v1"

    assert unknown.deployment_id == str(
        uuid5(_LEGACY_NAMESPACE, f"live:{connection.id}:private-model")
    )
    assert unknown.canonical_model_id is None
    assert unknown.effective_profile is None
    assert unknown.adapter_id == "openai_unresolved"
    assert unknown.adapter_version == "legacy"
    assert unknown.api_surface == "unknown"
    assert unknown.dialect_policy_id == "openai_unresolved.legacy"

    assert credentials.connection_auth_calls[0]["purpose"] == "known-live"
    assert credentials.connection_auth_calls[1]["purpose"] == "unknown-live"
    assert authorizer.calls[0]["access_context"] == LLMConnectionAccessContext(
        authenticated_user_id=owner.id,
        task_id=11,
        tenant_id=22,
    )
    assert (
        "llm_provider.legacy_identity_read.total",
        {"status": "live_unmapped", "connection_id": str(connection.id)},
        1,
    ) in calls
    _assert_no_secret_metric_values(calls)


def test_legacy_detached_resolution_selects_provider_credentials_and_deterministic_ids(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached legacy resolution covers known/unknown profiles and credential swaps."""

    calls = _capture_metrics(monkeypatch)
    owner, _ = identity_users
    credentials = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id, task_id=13, tenant_id=17)

    known = resolver.resolve_target(
        _legacy_selection(owner, model="gpt-5.2"),
        access_context=access,
        purpose="known-detached",
    )
    unknown = resolver.resolve_target(
        _legacy_selection(owner, model="private-model"),
        access_context=access,
        purpose="unknown-detached",
    )
    switched = resolver.resolve_target(
        _legacy_selection(owner, provider="openai", model="gpt-5.2"),
        access_context=access,
        purpose="switched-detached",
        target=LLMCallTarget(
            provider="anthropic",
            model="claude-sonnet-4-6",
        ),
    )

    openai_connection_id = uuid5(
        _LEGACY_NAMESPACE,
        f"detached-connection:{owner.id}:openai",
    )
    anthropic_connection_id = uuid5(
        _LEGACY_NAMESPACE,
        f"detached-connection:{owner.id}:anthropic",
    )
    assert known.connection.connection_id == str(openai_connection_id)
    assert known.deployment_id == str(
        uuid5(_LEGACY_NAMESPACE, f"detached-deployment:{openai_connection_id}:gpt-5.2")
    )
    assert known.canonical_model_id == "gpt-5.2"
    assert known.effective_profile == require_model_profile(
        ProviderModelRef("openai", "gpt-5.2")
    )

    assert unknown.connection.connection_id == str(openai_connection_id)
    assert unknown.deployment_id == str(
        uuid5(
            _LEGACY_NAMESPACE,
            f"detached-deployment:{openai_connection_id}:private-model",
        )
    )
    assert unknown.canonical_model_id is None
    assert unknown.effective_profile is None
    assert unknown.adapter_id == "openai_unresolved"
    assert unknown.api_surface == "unknown"

    assert switched.connection.connection_id == str(anthropic_connection_id)
    assert switched.exact_wire_model_id == "claude-sonnet-4-6"
    assert switched.connection.resolved_auth.secret is not None
    assert switched.connection.resolved_auth.secret.provider == "anthropic"
    assert credentials.credential_ref_calls == [
        {"user_id": owner.id, "provider": "anthropic"}
    ]
    assert credentials.secret_calls == [
        {
            "credential_ref": LLMCredentialRef(user_id=owner.id, provider="openai"),
            "runtime_user_id": owner.id,
            "task_id": 13,
            "purpose": "known-detached",
        },
        {
            "credential_ref": LLMCredentialRef(user_id=owner.id, provider="openai"),
            "runtime_user_id": owner.id,
            "task_id": 13,
            "purpose": "unknown-detached",
        },
        {
            "credential_ref": LLMCredentialRef(user_id=owner.id, provider="anthropic"),
            "runtime_user_id": owner.id,
            "task_id": 13,
            "purpose": "switched-detached",
        },
    ]
    assert (
        "llm_provider.legacy_identity_read.total",
        {"status": "detached"},
        1,
    ) in calls
    _assert_no_secret_metric_values(calls)


def test_legacy_user_mismatch_fails_before_credential_or_metric_work(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy selections remain bound to their credential user."""

    calls = _capture_metrics(monkeypatch)
    owner, other = identity_users
    credentials = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)

    with pytest.raises(LLMConfigurationError, match="Legacy selection user is not authorized"):
        resolver.resolve_target(
            LLMRuntimeSelection(
                provider="openai",
                model="gpt-5.2",
                credential_ref=LLMCredentialRef(user_id=other.id, provider="openai"),
            ),
            access_context=LLMRuntimeAccessContext(runtime_user_id=owner.id),
            purpose="mismatch",
        )

    assert calls == []
    assert credentials.connection_auth_calls == []
    assert credentials.secret_calls == []


def test_real_v2_failures_cover_inactive_route_target_and_authorization_statuses(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real services keep route and authorization failure behavior at the facade."""

    calls = _capture_metrics(monkeypatch)
    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(llm_identity_db, connection=connection)
    disabled_route = _add_route(llm_identity_db, deployment=deployment, enabled=False)
    credentials = _RecordingCredentialService()
    resolver = LLMRuntimeClientResolver(credentials, db=llm_identity_db)
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)

    with pytest.raises(LLMDeploymentNotFoundError, match="Preferred deployment route"):
        resolver.resolve_target(
            _v2_selection(deployment, route=disabled_route),
            access_context=access,
            purpose="route-disabled",
        )

    disabled_route.enabled = True
    llm_identity_db.flush()
    active_route = disabled_route
    with pytest.raises(LLMDeploymentNotFoundError, match="Call target does not match"):
        resolver.resolve_target(
            _v2_selection(deployment, route=active_route),
            access_context=access,
            purpose="target-mismatch",
            target=ProviderModelRef("openai", "different-model"),
        )

    LLMConnectionService(llm_identity_db).transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
        target_state="disabled",
    )
    with pytest.raises(LLMConnectionAuthorizationError, match="not enabled"):
        resolver.resolve_target(
            _v2_selection(deployment, route=active_route),
            access_context=access,
            purpose="authorization-denied",
        )
    assert (
        "llm_provider.deployment_resolution.total",
        {
            "status": "connection_not_enabled",
            "deployment_id": str(deployment.id),
            "connection_id": str(connection.id),
        },
        1,
    ) in calls
    _assert_no_secret_metric_values(calls)


def test_real_v2_deployment_unavailable_states_raise_before_authorization(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled and inactive deployments fail before connection authorization."""

    calls = _capture_metrics(monkeypatch)
    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    disabled = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="disabled-live-model",
        enabled=False,
    )
    inactive = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="inactive-live-model",
        lifecycle_state="retired",
    )
    credentials = _RecordingCredentialService()
    authorizer = _RecordingAuthorizer()
    resolver = LLMRuntimeClientResolver(
        credentials,
        db=llm_identity_db,
        connection_authorizer=authorizer,
    )

    for deployment in (disabled, inactive):
        with pytest.raises(LLMDeploymentNotFoundError, match="Deployment is unavailable"):
            resolver.resolve_target(
                _v2_selection(deployment),
                access_context=LLMRuntimeAccessContext(runtime_user_id=owner.id),
                purpose="unavailable",
            )

    assert authorizer.calls == []
    assert (
        "llm_provider.deployment_resolution.total",
        {"status": "deployment_unavailable", "deployment_id": str(disabled.id)},
        1,
    ) in calls
    assert (
        "llm_provider.deployment_resolution.total",
        {"status": "deployment_unavailable", "deployment_id": str(inactive.id)},
        1,
    ) in calls
    _assert_no_secret_metric_values(calls)
