"""Tests for dual-written LLM selection deployment refs and legacy snapshots."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
    UserLLMSelection,
    UserReportingLLMSelection,
    UserSettings,
)
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionService,
)
from backend.services.llm_provider.selection_service import LLMProviderSelectionService


def _active_openai_deployment(
    db: Session,
    *,
    user_id: int,
    model: str = "gpt-5.2",
) -> LLMModelDeployment:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=user_id,
        display_name=f"OpenAI {model}",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
        serving_operator_id="openai",
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=1,
    )
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id=model,
        canonical_model_id=model,
        display_name=model,
        discovery_source="test",
        lifecycle_state="active",
        availability_state="available",
        enabled=True,
        revision=1,
    )
    db.add_all([connection, deployment])
    db.flush()
    return deployment


def test_conversation_read_prefers_deployment_ref_and_updates_legacy_snapshot(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Conversation reads repair stale compatibility fields from deployment refs."""

    owner, _ = identity_users
    deployment = _active_openai_deployment(llm_identity_db, user_id=owner.id)
    llm_identity_db.add_all(
        [
            UserSettings(user_id=owner.id, openai_model="gpt-5-mini"),
            UserLLMSelection(
                user_id=owner.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                deployment_id=deployment.id,
            ),
        ]
    )
    llm_identity_db.flush()

    selection = LLMProviderSelectionService(llm_identity_db).get_selection(owner.id)

    assert selection.deployment_id == deployment.id
    assert selection.provider == "openai"
    assert selection.model == "gpt-5.2"
    settings = (
        llm_identity_db.query(UserSettings)
        .filter(UserSettings.user_id == owner.id)
        .one()
    )
    assert settings.openai_model == "gpt-5.2"


def test_selection_read_prefers_deployment_ref_for_compatibility_fields(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Product reads repair stale provider/model fields from deployment refs."""

    owner, _ = identity_users
    deployment = _active_openai_deployment(llm_identity_db, user_id=owner.id)
    llm_identity_db.add(
        UserLLMSelection(
            user_id=owner.id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            deployment_id=deployment.id,
        )
    )
    llm_identity_db.flush()

    read = LLMProviderSelectionService(llm_identity_db).get_selection_read(owner.id)

    assert read.selection.deployment_id == deployment.id
    assert read.selection.provider == "openai"
    assert read.selection.model == "gpt-5.2"
    assert read.status.status == "selectable"


def test_reporting_read_prefers_deployment_ref_and_updates_legacy_snapshot(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Reporting reads repair stale compatibility fields from deployment refs."""

    owner, _ = identity_users
    deployment = _active_openai_deployment(llm_identity_db, user_id=owner.id)
    llm_identity_db.add(
        UserReportingLLMSelection(
            user_id=owner.id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            deployment_id=deployment.id,
            reasoning_effort="high",
        )
    )
    llm_identity_db.flush()

    service = ReportingLLMSelectionService(llm_identity_db)
    read = service.get_selection_read(owner.id)
    runtime = service.build_deployment_runtime_selection(user_id=owner.id)

    assert read.selection is not None
    assert read.selection.deployment_id == deployment.id
    assert read.selection.provider == "openai"
    assert read.selection.model == "gpt-5.2"
    assert runtime.legacy_provider == "openai"
    assert runtime.legacy_model == "gpt-5.2"
