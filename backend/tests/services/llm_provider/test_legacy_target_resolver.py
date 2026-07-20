"""Direct proof tests for the extracted legacy LLM target resolver.

Purpose: prove mapped, live-unmapped, and detached legacy compatibility
resolution beside the currently wired facade branch before production cutover.
Scope boundary: these tests cover legacy authorization order, metrics,
deterministic target assembly, credential fallback, and mapped live delegation;
they do not exercise facade parsing or provider-client construction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.providers.llm.core.identity import ProviderModelRef
from agent.providers.llm.profiles.registry import require_model_profile
from backend.models import LLMInferenceConnection, LLMModelDeployment, User
from backend.services.llm_provider.effective_profile_service import NativeRouteContract
from backend.services.llm_provider.legacy_target_resolver import LegacyLLMTargetResolver
from backend.services.llm_provider import legacy_target_resolver as legacy_module
from backend.services.llm_provider.operation_registry import (
    FIXED_PROVIDER_ENDPOINT_POLICY_ID,
    ConnectionOperationRegistry,
)
from backend.services.llm_provider.types import (
    AuthorizedLLMConnectionOperation,
    DeploymentRef,
    LLMAuthMode,
    LLMCallTarget,
    LLMConnectionAccessContext,
    LLMConnectionCredentialRef,
    LLMCredentialRef,
    LLMConnectionOperation,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderSecret,
    ResolvedAuth,
    ResolvedConnectionTarget,
    ResolvedLLMTarget,
)

_LEGACY_NORMALIZATION_NAMESPACE = UUID("24359013-a580-474c-933e-ddd1a2e78c92")


class _RecordingCredentialService:
    """Credential double that records legacy credential resolution order."""

    def __init__(self, *, db: Session | None = None, events: list[str]) -> None:
        self._db = db
        self.events = events
        self.connection_auth_calls: list[dict[str, Any]] = []
        self.credential_ref_calls: list[dict[str, Any]] = []
        self.secret_calls: list[dict[str, Any]] = []

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
            secret=ProviderSecret(provider="openai", value="sk-live-legacy"),
        )

    def get_credential_ref(self, user_id: int, provider: str) -> LLMCredentialRef:
        self.events.append("credentials.get_credential_ref")
        self.credential_ref_calls.append({"user_id": user_id, "provider": provider})
        return LLMCredentialRef(user_id=user_id, provider=provider)

    def resolve_secret(
        self,
        credential_ref: LLMCredentialRef,
        *,
        runtime_user_id: int,
        task_id: int | None = None,
        purpose: str,
    ) -> ProviderSecret:
        self.events.append("credentials.resolve_secret")
        self.secret_calls.append(
            {
                "credential_ref": credential_ref,
                "runtime_user_id": runtime_user_id,
                "task_id": task_id,
                "purpose": purpose,
            }
        )
        return ProviderSecret(provider=credential_ref.provider, value="sk-detached")


class _RecordingAuthorizer:
    """Authorization double that records live legacy access checks."""

    def __init__(self, *, events: list[str]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def authorize(
        self,
        *,
        access_context: LLMConnectionAccessContext,
        connection_id,
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
        return AuthorizedLLMConnectionOperation(
            connection_id=str(connection_id),
            connection_revision=expected_revision,
            operation_target=ConnectionOperationRegistry().resolve(
                LLMConnectionOperation.INFERENCE,
                provider="openai",
            ),
        )


class _RecordingProfileService:
    """Profile double that records native contract resolution."""

    def __init__(self, *, events: list[str]) -> None:
        self.events = events
        self.profile = require_model_profile(ProviderModelRef("openai", "gpt-5.2"))

    def native_route_contract(self, _profile) -> NativeRouteContract:
        self.events.append("profiles.contract")
        return NativeRouteContract(
            adapter_id="openai_responses",
            adapter_version="1",
            api_surface="responses",
            dialect_policy_id="openai_responses.native_v1",
        )


class _RecordingLiveResolver:
    """Live resolver double that records mapped delegation input."""

    def __init__(self, result: ResolvedLLMTarget) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def resolve_target(
        self,
        selection: LLMRuntimeSelectionV2,
        *,
        access_context: LLMRuntimeAccessContext,
        purpose: str,
        target: ProviderModelRef | LLMCallTarget | None = None,
    ) -> ResolvedLLMTarget:
        self.calls.append(
            {
                "selection": selection,
                "access_context": access_context,
                "purpose": purpose,
                "target": target,
            }
        )
        return self.result


def _capture_metric_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list[tuple[str, dict[str, str], int]] = []
    monkeypatch.setattr(
        legacy_module,
        "safe_inc_labeled",
        lambda name, labels, value=1: calls.append((name, dict(labels), value)),
    )
    return calls


def _add_connection(
    db: Session,
    *,
    owner: User,
    provider: str = "openai",
) -> LLMInferenceConnection:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name="Legacy proof connection",
        connection_preset_id=provider,
        runtime_family_id=f"{provider}_native",
        serving_operator_id=provider,
        transport_origin="backend",
        endpoint_policy_id=FIXED_PROVIDER_ENDPOINT_POLICY_ID,
        state="enabled",
        revision=3,
        legacy_default_provider=provider,
    )
    db.add(connection)
    db.flush()
    return connection


def _add_deployment(
    db: Session,
    *,
    connection: LLMInferenceConnection,
    wire_model_id: str,
) -> LLMModelDeployment:
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id=wire_model_id,
        canonical_model_id="gpt-5.2",
        display_name="Legacy proof deployment",
        discovery_source="test",
        lifecycle_state="active",
        enabled=True,
        revision=7,
    )
    db.add(deployment)
    db.flush()
    return deployment


def _resolved_target() -> ResolvedLLMTarget:
    operation_target = ConnectionOperationRegistry().resolve(
        LLMConnectionOperation.INFERENCE,
        provider="openai",
    )
    profile = require_model_profile(ProviderModelRef("openai", "gpt-5.2"))
    return ResolvedLLMTarget(
        connection=ResolvedConnectionTarget(
            connection_id=str(uuid4()),
            connection_revision=7,
            connection_preset_id="openai",
            runtime_family_id="openai_native",
            serving_operator_id="openai",
            transport_origin="backend",
            endpoint_policy_id="fixed_provider_v1",
            endpoint=operation_target.url,
            operation_target=operation_target,
            resolved_auth=ResolvedAuth.with_secret(
                mode=LLMAuthMode.API_KEY,
                provider="openai",
                secret=ProviderSecret(provider="openai", value="sk-mapped"),
            ),
        ),
        deployment_id=str(uuid4()),
        deployment_revision=7,
        route_id=None,
        adapter_id="openai_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
        canonical_model_id="gpt-5.2",
        exact_wire_model_id="gpt-5.2",
        effective_profile=profile,
    )


def _selection(owner: User, *, model: str = "gpt-5.2") -> LLMRuntimeSelection:
    return LLMRuntimeSelection(
        provider="openai",
        model=model,
        credential_ref=LLMCredentialRef(user_id=owner.id, provider="openai"),
        reasoning_effort="high",
    )


def test_legacy_user_authorization_runs_before_mapping_or_credentials(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Mismatched legacy users fail before lookup delegation or secret work."""

    owner, other = identity_users
    events: list[str] = []
    credential_service = _RecordingCredentialService(
        db=llm_identity_db,
        events=events,
    )
    live_resolver = _RecordingLiveResolver(_resolved_target())
    resolver = LegacyLLMTargetResolver(
        credential_service,  # type: ignore[arg-type]
        db=llm_identity_db,
        live_resolver=live_resolver,  # type: ignore[arg-type]
    )

    with pytest.raises(LLMConfigurationError, match="Legacy selection user"):
        resolver.resolve(
            _selection(owner),
            call_ref=ProviderModelRef("openai", "gpt-5.2"),
            access_context=LLMRuntimeAccessContext(runtime_user_id=other.id),
            purpose="legacy-proof",
        )

    assert events == []
    assert live_resolver.calls == []


def test_mapped_legacy_selection_emits_metric_and_delegates_to_live_resolver(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped legacy identity synthesizes the same V2 payload for live policy."""

    calls = _capture_metric_calls(monkeypatch)
    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    deployment = _add_deployment(
        llm_identity_db,
        connection=connection,
        wire_model_id="Org/Model-Case:Exact",
    )
    live_result = _resolved_target()
    live_resolver = _RecordingLiveResolver(live_result)
    resolver = LegacyLLMTargetResolver(
        _RecordingCredentialService(db=llm_identity_db, events=[]),  # type: ignore[arg-type]
        db=llm_identity_db,
        live_resolver=live_resolver,  # type: ignore[arg-type]
    )
    access = LLMRuntimeAccessContext(runtime_user_id=owner.id)
    target = LLMCallTarget(
        provider="openai",
        model="Org/Model-Case:Exact",
        role="chat",
    )

    result = resolver.resolve(
        _selection(owner, model="Org/Model-Case:Exact"),
        call_ref=ProviderModelRef("openai", "Org/Model-Case:Exact"),
        access_context=access,
        purpose="legacy-proof",
        target=target,
    )

    assert result is live_result
    assert len(live_resolver.calls) == 1
    delegated = live_resolver.calls[0]
    delegated_selection = delegated["selection"]
    assert delegated_selection.deployment_ref == DeploymentRef(
        str(deployment.id),
        int(deployment.revision),
    )
    assert delegated_selection.reasoning_effort == "high"
    assert delegated_selection.legacy_provider == "openai"
    assert delegated_selection.legacy_model == "Org/Model-Case:Exact"
    assert delegated["access_context"] is access
    assert delegated["purpose"] == "legacy-proof"
    assert delegated["target"] is target
    assert calls == [
        (
            "llm_provider.legacy_identity_read.total",
            {"status": "mapped", "deployment_id": str(deployment.id)},
            1,
        )
    ]


def test_live_unmapped_legacy_target_preserves_auth_route_credential_and_ids(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live-unmapped compatibility authorizes before credential resolution."""

    calls = _capture_metric_calls(monkeypatch)
    owner, _ = identity_users
    connection = _add_connection(llm_identity_db, owner=owner)
    events: list[str] = []
    credential_service = _RecordingCredentialService(
        db=llm_identity_db,
        events=events,
    )
    authorizer = _RecordingAuthorizer(events=events)
    profile_service = _RecordingProfileService(events=events)
    resolver = LegacyLLMTargetResolver(
        credential_service,  # type: ignore[arg-type]
        db=llm_identity_db,
        live_resolver=_RecordingLiveResolver(_resolved_target()),  # type: ignore[arg-type]
        connection_authorizer=authorizer,  # type: ignore[arg-type]
        effective_profile_service=profile_service,  # type: ignore[arg-type]
    )
    access = LLMRuntimeAccessContext(
        runtime_user_id=owner.id,
        task_id=11,
        tenant_id=22,
    )

    result = resolver.resolve(
        _selection(owner, model="gpt-5.2"),
        call_ref=ProviderModelRef("openai", "gpt-5.2"),
        access_context=access,
        purpose="legacy-live",
    )

    assert events == [
        "profiles.contract",
        "authorizer.authorize",
        "credentials.resolve_connection_auth",
    ]
    assert authorizer.calls == [
        {
            "access_context": LLMConnectionAccessContext(
                authenticated_user_id=owner.id,
                task_id=11,
                tenant_id=22,
            ),
            "connection_id": str(connection.id),
            "expected_revision": 3,
            "operation": "inference",
        }
    ]
    assert credential_service.connection_auth_calls[0] == {
        "connection_ref": LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=3,
        ),
        "runtime_user_id": owner.id,
        "task_id": 11,
        "purpose": "legacy-live",
        "auth_mode": LLMAuthMode.API_KEY,
    }
    assert result.deployment_id == str(
        uuid5(_LEGACY_NORMALIZATION_NAMESPACE, f"live:{connection.id}:gpt-5.2")
    )
    assert result.deployment_revision == 1
    assert result.route_id is None
    assert result.adapter_id == "openai_responses"
    assert result.dialect_policy_id == "openai_responses.native_v1"
    assert result.canonical_model_id == "gpt-5.2"
    assert result.exact_wire_model_id == "gpt-5.2"
    assert result.connection.connection_id == str(connection.id)
    assert result.connection.endpoint == "https://api.openai.com/v1/chat/completions"
    assert result.connection.resolved_auth.secret is not None
    assert result.connection.resolved_auth.secret.value == "sk-live-legacy"
    assert calls == [
        (
            "llm_provider.legacy_identity_read.total",
            {"status": "live_unmapped", "connection_id": str(connection.id)},
            1,
        )
    ]
    assert "sk-live-legacy" not in repr(calls)


def test_detached_legacy_target_uses_provider_fallback_unresolved_route_and_ids(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached compatibility keeps provider fallback and deterministic UUIDs."""

    calls = _capture_metric_calls(monkeypatch)
    owner, _ = identity_users
    events: list[str] = []
    credential_service = _RecordingCredentialService(
        db=llm_identity_db,
        events=events,
    )
    resolver = LegacyLLMTargetResolver(
        credential_service,  # type: ignore[arg-type]
        db=llm_identity_db,
        live_resolver=_RecordingLiveResolver(_resolved_target()),  # type: ignore[arg-type]
    )

    result = resolver.resolve(
        _selection(owner),
        call_ref=ProviderModelRef("anthropic", "claude-custom"),
        access_context=LLMRuntimeAccessContext(
            runtime_user_id=owner.id,
            task_id=31,
            tenant_id=32,
        ),
        purpose="legacy-detached",
    )

    connection_id = uuid5(
        _LEGACY_NORMALIZATION_NAMESPACE,
        f"detached-connection:{owner.id}:anthropic",
    )
    deployment_id = uuid5(
        _LEGACY_NORMALIZATION_NAMESPACE,
        f"detached-deployment:{connection_id}:claude-custom",
    )
    assert events == [
        "credentials.get_credential_ref",
        "credentials.resolve_secret",
    ]
    assert credential_service.credential_ref_calls == [
        {"user_id": owner.id, "provider": "anthropic"}
    ]
    assert credential_service.secret_calls == [
        {
            "credential_ref": LLMCredentialRef(
                user_id=owner.id,
                provider="anthropic",
            ),
            "runtime_user_id": owner.id,
            "task_id": 31,
            "purpose": "legacy-detached",
        }
    ]
    assert result.connection.connection_id == str(connection_id)
    assert result.deployment_id == str(deployment_id)
    assert result.connection.connection_preset_id == "anthropic"
    assert result.connection.runtime_family_id == "anthropic_native"
    assert result.connection.endpoint_policy_id == "fixed_provider_v1"
    assert result.connection.endpoint == "https://api.anthropic.com/v1/messages"
    assert result.connection.resolved_auth.secret is not None
    assert result.connection.resolved_auth.secret.provider == "anthropic"
    assert result.connection.resolved_auth.secret.value == "sk-detached"
    assert result.adapter_id == "anthropic_unresolved"
    assert result.adapter_version == "legacy"
    assert result.api_surface == "unknown"
    assert result.dialect_policy_id == "anthropic_unresolved.legacy"
    assert result.canonical_model_id is None
    assert result.exact_wire_model_id == "claude-custom"
    assert result.effective_profile is None
    assert calls == [
        (
            "llm_provider.legacy_identity_read.total",
            {"status": "detached"},
            1,
        )
    ]
    assert "sk-detached" not in repr(calls)
