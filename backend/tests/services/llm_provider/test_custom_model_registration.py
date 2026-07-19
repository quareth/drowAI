"""Tests for conservative custom LLM model registration."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from backend.models import LLMCapabilityObservation, User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.effective_profile_service import EffectiveProfileService
from backend.services.llm_provider.inventory_service import LLMInventoryService
from backend.services.llm_provider.operation_registry import CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
from backend.services.llm_provider.types import LLMDeploymentValidationError


def test_custom_model_registration_rejects_capabilities_outside_route_adapter(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Team vLLM",
        connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="organization_managed",
        non_secret_config={
            "base_url": "https://llm.example.test",
            "auth_mode": "bearer",
        },
    )

    with pytest.raises(LLMDeploymentValidationError):
        LLMInventoryService(llm_identity_db).register_custom_model(
            user_id=owner.id,
            connection_id=connection.id,
            expected_connection_revision=1,
            wire_model_id="local/tool-model",
            display_name="Tool Model",
            requested_capabilities=(LLMCapability.TOOLS,),
        )


def test_custom_model_registration_requires_observation_evidence_for_runnability(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Team vLLM",
        connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="organization_managed",
        non_secret_config={
            "base_url": "https://llm.example.test",
            "auth_mode": "bearer",
        },
    )
    deployment, route = LLMInventoryService(llm_identity_db).register_custom_model(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="local/chat-model",
        display_name="Local Chat Model",
        requested_capabilities=(LLMCapability.CHAT,),
    )

    assert deployment.discovery_source == "custom"
    assert deployment.availability_state == "unknown"
    assert route.adapter_id == "openai_compatible_chat"

    profile_service = EffectiveProfileService(llm_identity_db)
    unrunnable = profile_service.classify_runnability(
        deployment=deployment,
        route=route,
        required_capabilities=(LLMCapability.CHAT,),
        connection_id=str(connection.id),
        connection_revision=1,
    )
    assert unrunnable.runnable is False
    assert unrunnable.status == "capability_unknown"
    assert unrunnable.missing_capabilities == ("chat",)

    llm_identity_db.add(
        LLMCapabilityObservation(
            id=uuid4(),
            deployment_id=deployment.id,
            route_id=route.id,
            capability=LLMCapability.CHAT.value,
            support_state="supported",
            constraints={
                "connection_id": str(connection.id),
                "connection_revision": 1,
            },
            source="capability_probe",
            revision=1,
            fingerprint="chat-supported",
        )
    )
    llm_identity_db.flush()

    runnable = profile_service.classify_runnability(
        deployment=deployment,
        route=route,
        required_capabilities=(LLMCapability.CHAT,),
        connection_id=str(connection.id),
        connection_revision=1,
    )
    assert runnable.runnable is True
    assert runnable.status == "runnable"
