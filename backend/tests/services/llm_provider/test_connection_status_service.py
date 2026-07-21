"""Direct contract tests for shared LLM connection status composition.

These tests prove immutable provider-layer outcomes after router helper removal.
They do not exercise lifecycle mutation, transactions, credential disclosure,
or guarded egress.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from backend.models import (
    LLMCapabilityObservation,
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
)
from backend.services.llm_provider import LLMConnectionStatusService
from backend.services.llm_provider.application_contracts import (
    RunnabilityOutcome,
    VerificationOutcome,
)
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.inventory_service import (
    GptOssProvingVerificationResult,
)
from backend.services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    LLMConnectionCredentialRef,
    LLMConnectionState,
    LLMDeploymentNotFoundError,
    LLMDeploymentValidationError,
)


_SECRET = "status-service-secret"


def _managed_rows(
    db: Session,
    *,
    user_id: int,
) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
    connection = LLMConnectionService(db).create_draft(
        user_id=user_id,
        display_name="HF Router",
        connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="huggingface",
    )
    deployment, route = LLMDeploymentService(db).create_preset_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="openai/gpt-oss-20b:fireworks-ai",
        canonical_model_id="openai/gpt-oss-20b",
        display_name="GPT-OSS 20B via HF",
    )
    return connection, deployment, route


def _proving_rows(
    db: Session,
    *,
    user_id: int,
) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
    connection = LLMConnectionService(db).create_gpt_oss_20b_proving_draft(
        user_id=user_id,
    )
    deployment, route = LLMDeploymentService(db).create_gpt_oss_20b_proving_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=1,
    )
    return connection, deployment, route


def _store_connection_credential(
    db: Session,
    *,
    user_id: int,
    connection: LLMInferenceConnection,
    provider: str,
) -> str:
    credentials = LLMCredentialService(db)
    credentials.upsert_connection_api_key(
        user_id=user_id,
        connection_ref=LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        ),
        provider=provider,
        api_key=_SECRET,
    )
    return credentials.connection_credential_fingerprint(
        user_id=user_id,
        connection_ref=LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=int(connection.revision),
        ),
        provider=provider,
    )


def _record_proving_evidence(
    db: Session,
    *,
    connection: LLMInferenceConnection,
    deployment: LLMModelDeployment,
    route: LLMDeploymentRoute,
    credential_fingerprint: str,
) -> None:
    observed_at = datetime.now(timezone.utc)
    for revision, capability in enumerate(
        (LLMCapability.CHAT, LLMCapability.USAGE_REPORTING),
        start=1,
    ):
        db.add(
            LLMCapabilityObservation(
                id=uuid4(),
                deployment_id=deployment.id,
                route_id=route.id,
                capability=capability.value,
                support_state="supported",
                constraints={
                    "connection_id": str(connection.id),
                    "connection_revision": int(connection.revision),
                    "credential_fingerprint": credential_fingerprint,
                },
                source="gpt_oss_proving_probe",
                observed_at=observed_at,
                expires_at=observed_at + timedelta(hours=1),
                revision=revision,
                fingerprint=f"status-service-{revision}",
            )
        )
    db.flush()


def _connection_runnability(
    service: LLMConnectionStatusService,
    *,
    user_id: int,
    connection: LLMInferenceConnection | None,
    deployment: LLMModelDeployment | None,
    route: LLMDeploymentRoute | None,
) -> RunnabilityOutcome:
    return service.connection_runnability(
        user_id=user_id,
        connection=connection,
        deployment=deployment,
        route=route,
    )


def _proving_runnability(
    service: LLMConnectionStatusService,
    *,
    connection: LLMInferenceConnection,
    deployment: LLMModelDeployment,
    route: LLMDeploymentRoute,
) -> RunnabilityOutcome:
    return service.proving_runnability(
        connection=connection,
        deployment=deployment,
        route=route,
    )


def test_refs_and_verification_preserve_public_contract_fields(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Refs, no-evidence status, and provider evidence preserve every field."""

    owner, _ = identity_users
    connection, deployment, _ = _proving_rows(
        llm_identity_db,
        user_id=owner.id,
    )
    service = LLMConnectionStatusService(llm_identity_db)
    observed_at = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    result = GptOssProvingVerificationResult(
        status="passed",
        code="verified",
        message="GPT-OSS proving endpoint verified",
        retryable=False,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=1),
        model_present=True,
        usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    )

    assert asdict(service.connection_ref(connection)) == {
        "connection_id": str(connection.id),
        "expected_revision": int(connection.revision),
    }
    assert asdict(service.deployment_ref(deployment)) == {
        "deployment_id": str(deployment.id),
        "expected_revision": int(deployment.revision),
    }
    assert asdict(service.not_tested_verification()) == {
        "status": "failed",
        "code": "not_tested",
        "message": "Verification has not run.",
        "retryable": False,
        "observed_at": None,
        "expires_at": None,
        "model_present": None,
        "usage": None,
    }
    assert asdict(service.verification(result)) == {
        "status": "passed",
        "code": "verified",
        "message": "GPT-OSS proving endpoint verified",
        "retryable": False,
        "observed_at": observed_at,
        "expires_at": observed_at + timedelta(hours=1),
        "model_present": True,
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        },
    }
    assert _SECRET not in repr(service.verification(result))


def test_first_route_is_owner_scoped_and_preserves_missing_route_detail(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Route lookup reuses deployment ownership and reports the stable detail."""

    owner, other = identity_users
    connection, deployment, route = _proving_rows(
        llm_identity_db,
        user_id=owner.id,
    )
    service = LLMConnectionStatusService(llm_identity_db)

    assert service.first_route_for_deployment(
        user_id=owner.id,
        deployment_id=deployment.id,
    ).id == route.id
    with pytest.raises(LLMDeploymentNotFoundError, match="Deployment was not found"):
        service.first_route_for_deployment(
            user_id=other.id,
            deployment_id=deployment.id,
        )

    route_only_deployment = LLMDeploymentService(llm_identity_db).create_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="route-missing",
        display_name="Route missing",
        discovery_source="test",
    )
    with pytest.raises(
        LLMDeploymentValidationError,
        match="Deployment route is unavailable",
    ):
        service.first_route_for_deployment(
            user_id=owner.id,
            deployment_id=route_only_deployment.id,
        )


def test_managed_runnability_matches_missing_invalid_and_runnable_cases(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Generic connection decisions remain field-identical to router behavior."""

    owner, _ = identity_users
    service = LLMConnectionStatusService(llm_identity_db)

    assert _connection_runnability(
        service,
        user_id=owner.id,
        connection=None,
        deployment=None,
        route=None,
    ).status == "not_created"

    managed, deployment, route = _managed_rows(
        llm_identity_db,
        user_id=owner.id,
    )
    assert _connection_runnability(
        service,
        user_id=owner.id,
        connection=managed,
        deployment=None,
        route=None,
    ).status == "deployment_missing"
    assert _connection_runnability(
        service,
        user_id=owner.id,
        connection=managed,
        deployment=deployment,
        route=None,
    ).status == "capability_unknown"
    assert _connection_runnability(
        service,
        user_id=owner.id,
        connection=managed,
        deployment=deployment,
        route=route,
    ).status == "credential_missing"

    invalid_connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Invalid native route",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )
    invalid_deployment = LLMDeploymentService(llm_identity_db).create_deployment(
        user_id=owner.id,
        connection_id=invalid_connection.id,
        expected_connection_revision=1,
        wire_model_id="gpt-5.2",
        display_name="GPT 5.2",
        discovery_source="test",
    )
    invalid_route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=invalid_deployment.id,
        adapter_id="wrong_adapter",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
        enabled=True,
    )
    llm_identity_db.add(invalid_route)
    llm_identity_db.flush()
    invalid = _connection_runnability(
        service,
        user_id=owner.id,
        connection=invalid_connection,
        deployment=invalid_deployment,
        route=invalid_route,
    )
    assert invalid.status == "invalid_selection"
    assert invalid.selectable is False

    _store_connection_credential(
        llm_identity_db,
        user_id=owner.id,
        connection=managed,
        provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    )
    connections = LLMConnectionService(llm_identity_db)
    connections.transition_state(
        user_id=owner.id,
        connection_id=managed.id,
        expected_revision=2,
        target_state=LLMConnectionState.DISABLED,
    )
    connections.transition_state(
        user_id=owner.id,
        connection_id=managed.id,
        expected_revision=3,
        target_state=LLMConnectionState.ENABLED,
    )
    runnable = _connection_runnability(
        service,
        user_id=owner.id,
        connection=managed,
        deployment=deployment,
        route=route,
    )
    assert runnable == RunnabilityOutcome(
        status="runnable",
        selectable=True,
        runnable=True,
        reason=None,
    )


def test_proving_runnability_preserves_credential_and_evidence_cases(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Proving decisions retain credential fingerprint and evidence policy."""

    owner, _ = identity_users
    connection, deployment, route = _proving_rows(
        llm_identity_db,
        user_id=owner.id,
    )
    service = LLMConnectionStatusService(llm_identity_db)

    missing_credential = _proving_runnability(
        service,
        connection=connection,
        deployment=deployment,
        route=route,
    )
    assert missing_credential.status == "credential_missing"

    credential_fingerprint = _store_connection_credential(
        llm_identity_db,
        user_id=owner.id,
        connection=connection,
        provider=GPT_OSS_20B_PROVING_PRESET_ID,
    )
    missing_evidence = _proving_runnability(
        service,
        connection=connection,
        deployment=deployment,
        route=route,
    )
    assert missing_evidence == RunnabilityOutcome(
        status="capability_unknown",
        selectable=True,
        runnable=False,
        reason="Usage evidence is required.",
    )

    _record_proving_evidence(
        llm_identity_db,
        connection=connection,
        deployment=deployment,
        route=route,
        credential_fingerprint=credential_fingerprint,
    )
    runnable = _proving_runnability(
        service,
        connection=connection,
        deployment=deployment,
        route=route,
    )
    assert runnable.status == "runnable"
    assert runnable.runnable is True
    assert _SECRET not in repr(runnable)


def test_managed_and_proving_statuses_preserve_contract_without_transactions(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Complete status outcomes match legacy fields and own no transaction."""

    owner, _ = identity_users
    managed, managed_deployment, _ = _managed_rows(
        llm_identity_db,
        user_id=owner.id,
    )
    proving, proving_deployment, _ = _proving_rows(
        llm_identity_db,
        user_id=owner.id,
    )
    service = LLMConnectionStatusService(llm_identity_db)
    observed_at = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    verification_result = GptOssProvingVerificationResult(
        status="passed",
        code="verified",
        message="GPT-OSS proving endpoint verified",
        retryable=False,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=1),
        model_present=True,
        usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    )
    verification = service.verification(verification_result)

    transaction_calls: list[str] = []
    monkeypatch.setattr(
        llm_identity_db,
        "commit",
        lambda: transaction_calls.append("commit"),
    )
    monkeypatch.setattr(
        llm_identity_db,
        "rollback",
        lambda: transaction_calls.append("rollback"),
    )

    managed_actual = service.managed_status(
        user_id=owner.id,
        connection=managed,
        deployment=managed_deployment,
    )
    managed_route = service.first_route_for_deployment(
        user_id=owner.id,
        deployment_id=managed_deployment.id,
    )
    assert managed_actual.lifecycle_state == managed.state
    assert asdict(managed_actual.connection_ref) == {
        "connection_id": str(managed.id),
        "expected_revision": int(managed.revision),
    }
    assert managed_actual.deployment_ref is not None
    assert asdict(managed_actual.deployment_ref) == {
        "deployment_id": str(managed_deployment.id),
        "expected_revision": int(managed_deployment.revision),
    }
    assert managed_actual.verification is not None
    assert managed_actual.verification.code == "not_tested"
    assert managed_actual.runnability is not None
    assert managed_actual.runnability == service.connection_runnability(
        user_id=owner.id,
        connection=managed,
        deployment=managed_deployment,
        route=managed_route,
    )

    proving_actual = service.proving_status(
        user_id=owner.id,
        connection=proving,
        deployment=proving_deployment,
        verification=verification,
    )
    assert proving_actual.lifecycle_state == proving.state
    assert proving_actual.connection_ref.connection_id == str(proving.id)
    assert proving_actual.connection_ref.expected_revision == int(proving.revision)
    assert proving_actual.deployment_ref is not None
    assert proving_actual.deployment_ref.deployment_id == str(proving_deployment.id)
    assert proving_actual.deployment_ref.expected_revision == int(
        proving_deployment.revision
    )
    assert proving_actual.verification == verification
    assert proving_actual.runnability is not None
    assert proving_actual.runnability.status == "credential_missing"
    assert transaction_calls == []
    rendered = f"{managed_actual!r}\n{proving_actual!r}"
    assert _SECRET not in rendered
    assert "ProviderSecret" not in rendered
    assert isinstance(proving_actual.verification, VerificationOutcome)
