"""Tests for deployment-aware reporting LLM selection persistence."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from backend.models import User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.operation_registry import (
    NVIDIA_NIM_BASE_URL_ENV,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionService,
)
from backend.services.llm_provider.types import (
    LLMConnectionState,
    LLMConnectionCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeSelectionV2,
    ProviderConfigurationError,
)


def _reporting_deployment(db: Session, *, user_id: int, model: str):
    connections = LLMConnectionService(db)
    connection = connections.get_owned_for_preset(
        user_id=user_id,
        connection_preset_id="openai",
    )
    if connection is None:
        connection = connections.create_draft(
            user_id=user_id,
            display_name="Reporting endpoint",
            connection_preset_id="openai",
            runtime_family_id="openai_native",
        )
        connection = connections.transition_state(
            user_id=user_id,
            connection_id=connection.id,
            expected_revision=1,
            target_state=LLMConnectionState.DISABLED,
        )
        connection = connections.transition_state(
            user_id=user_id,
            connection_id=connection.id,
            expected_revision=connection.revision,
            target_state=LLMConnectionState.ENABLED,
        )
    return LLMDeploymentService(db).create_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=int(connection.revision),
        wire_model_id=model,
        canonical_model_id=model,
        display_name=model,
        discovery_source="operator",
    )


def test_reporting_selection_persists_deployment_effort_and_compatibility_fields(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Reporting deployment selection retains legacy fields and runtime options."""

    owner, _ = identity_users
    deployment = _reporting_deployment(
        llm_identity_db,
        user_id=owner.id,
        model="gpt-5.2",
    )
    service = ReportingLLMSelectionService(llm_identity_db)

    saved = service.set_deployment_selection(
        user_id=owner.id,
        deployment_id=str(deployment.id),
        expected_deployment_revision=1,
        reasoning_effort="high",
    )
    runtime = service.build_deployment_runtime_selection(user_id=owner.id)

    assert saved.deployment_id == deployment.id
    assert saved.provider == "openai"
    assert saved.model == "gpt-5.2"
    assert saved.reasoning_effort == "high"
    assert isinstance(runtime, LLMRuntimeSelectionV2)
    assert runtime.deployment_ref.deployment_id == str(deployment.id)
    assert runtime.reasoning_effort == "high"
    assert runtime.legacy_provider == "openai"
    assert runtime.legacy_model == "gpt-5.2"


def test_reporting_selection_rejects_incompatible_or_stale_deployment(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Reporting needs structured output and a current deployment revision."""

    owner, _ = identity_users
    incompatible = _reporting_deployment(
        llm_identity_db,
        user_id=owner.id,
        model="gpt-5.4-pro",
    )
    service = ReportingLLMSelectionService(llm_identity_db)

    with pytest.raises(ProviderConfigurationError):
        service.set_deployment_selection(
            user_id=owner.id,
            deployment_id=str(incompatible.id),
            expected_deployment_revision=1,
        )
    compatible = _reporting_deployment(
        llm_identity_db,
        user_id=owner.id,
        model="gpt-5.2",
    )
    with pytest.raises(LLMDeploymentNotFoundError):
        service.set_deployment_selection(
            user_id=owner.id,
            deployment_id=str(compatible.id),
            expected_deployment_revision=2,
        )


def test_reporting_selection_accepts_agent_capable_gpt_oss_deployment(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """GPT-OSS can serve reporting's structured-output contract."""

    monkeypatch.setenv(
        NVIDIA_NIM_BASE_URL_ENV,
        "https://integrate.api.nvidia.com/v1",
    )
    owner, _ = identity_users
    connections = LLMConnectionService(llm_identity_db)
    connection = connections.create_draft(
        user_id=owner.id,
        display_name="GPT-OSS 20B via NVIDIA NIM",
        connection_preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="nvidia_nim",
        non_secret_config={"auth_mode": "bearer"},
    )
    LLMCredentialService(llm_identity_db).upsert_connection_api_key(
        user_id=owner.id,
        connection_ref=LLMConnectionCredentialRef(
            connection_id=str(connection.id),
            expected_revision=connection.revision,
        ),
        provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        api_key="test-nvidia-key",
    )
    llm_identity_db.refresh(connection)
    deployment, route = LLMDeploymentService(
        llm_identity_db
    ).create_preset_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=connection.revision,
        wire_model_id="openai/gpt-oss-20b",
        canonical_model_id="openai/gpt-oss-20b",
        display_name="GPT-OSS 20B via NVIDIA NIM",
    )
    connection = connections.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=connection.revision,
        target_state=LLMConnectionState.DISABLED,
    )
    connections.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=connection.revision,
        target_state=LLMConnectionState.ENABLED,
    )

    service = ReportingLLMSelectionService(llm_identity_db)
    saved = service.set_deployment_selection(
        user_id=owner.id,
        deployment_id=str(deployment.id),
        expected_deployment_revision=deployment.revision,
    )

    assert saved.deployment_id == deployment.id
    assert saved.provider == "openai"
    assert saved.model == "gpt-oss-20b"
    runtime = service.build_deployment_runtime_selection(user_id=owner.id)
    assert runtime.preferred_route_id == str(route.id)
