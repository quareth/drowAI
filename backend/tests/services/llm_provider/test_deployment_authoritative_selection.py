"""Phase 6 tests for deployment-authoritative text LLM selections."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
)
from backend.services.embeddings.selection_service import (
    EmbeddingRuntimeSelectionService,
)
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionService,
)
from backend.services.llm_provider.runtime_config_service import (
    LLMRuntimeConfigService,
)
from backend.services.llm_provider.selection_service import LLMProviderSelectionService
from backend.services.llm_provider.types import (
    LLMCredentialRef,
    LLMRuntimeSelectionV2,
    ProviderConfigurationError,
)


def _legacy_default_deployment(
    db: Session,
    *,
    user_id: int,
    model: str = "gpt-5.2",
    legacy_default: bool = True,
) -> LLMModelDeployment:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=user_id,
        display_name=f"OpenAI {model}",
        connection_preset_id=OPENAI_PROVIDER_ID,
        runtime_family_id="openai_native",
        serving_operator_id=OPENAI_PROVIDER_ID,
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=1,
        legacy_default_provider=OPENAI_PROVIDER_ID if legacy_default else None,
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
    db.add_all(
        [
            UserLLMProviderCredential(
                user_id=user_id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            connection,
            deployment,
        ]
    )
    db.flush()
    return deployment


def test_provider_model_conversation_write_resolves_authoritative_deployment_ref(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Compatibility writes no longer create active legacy-only selections."""

    owner, _ = identity_users
    deployment = _legacy_default_deployment(llm_identity_db, user_id=owner.id)

    saved = LLMProviderSelectionService(llm_identity_db).set_selection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
    )

    assert saved.deployment_id == deployment.id
    assert saved.provider == OPENAI_PROVIDER_ID
    assert saved.model == "gpt-5.2"


def test_provider_model_reporting_write_resolves_authoritative_deployment_ref(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Reporting compatibility writes retain snapshots behind deployment refs."""

    owner, _ = identity_users
    deployment = _legacy_default_deployment(llm_identity_db, user_id=owner.id)

    saved = ReportingLLMSelectionService(llm_identity_db).set_selection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5.2",
        reasoning_effort="high",
    )

    assert saved.deployment_id == deployment.id
    assert saved.provider == OPENAI_PROVIDER_ID
    assert saved.model == "gpt-5.2"
    assert saved.reasoning_effort == "high"


def test_legacy_conversation_row_is_readable_but_unrunnable(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Historical rows without deployment refs stay visible but cannot execute."""

    owner, _ = identity_users
    llm_identity_db.add(
        UserLLMSelection(
            user_id=owner.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
    )
    llm_identity_db.flush()

    service = LLMProviderSelectionService(llm_identity_db)
    read = service.get_selection_read(owner.id)

    assert read.selection.provider == OPENAI_PROVIDER_ID
    assert read.selection.model == "gpt-5.2"
    assert read.selection.deployment_id is None
    assert read.status.selectable is True
    assert read.status.runnable is False
    assert read.status.status == "deployment_unmapped"
    with pytest.raises(ProviderConfigurationError, match="deployment binding"):
        LLMRuntimeConfigService(llm_identity_db).build_conversation_runtime_selection(
            user_id=owner.id,
        )


def test_conversation_runtime_uses_saved_deployment_ref_even_before_readiness(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Active deployment refs are authoritative without legacy readiness fallback."""

    owner, _ = identity_users
    deployment = _legacy_default_deployment(
        llm_identity_db,
        user_id=owner.id,
        legacy_default=False,
    )
    llm_identity_db.add(
        UserLLMSelection(
            user_id=owner.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
            deployment_id=deployment.id,
        )
    )
    llm_identity_db.flush()

    runtime = LLMRuntimeConfigService(
        llm_identity_db
    ).build_conversation_runtime_selection(user_id=owner.id)

    assert isinstance(runtime, LLMRuntimeSelectionV2)
    assert runtime.deployment_ref.deployment_id == str(deployment.id)
    assert runtime.legacy_provider == OPENAI_PROVIDER_ID
    assert runtime.legacy_model == "gpt-5.2"


def test_legacy_reporting_row_is_readable_but_unrunnable(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Historical reporting rows without deployment refs cannot start reports."""

    owner, _ = identity_users
    llm_identity_db.add(
        UserReportingLLMSelection(
            user_id=owner.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
    )
    llm_identity_db.flush()

    service = ReportingLLMSelectionService(llm_identity_db)
    read = service.get_selection_read(owner.id)

    assert read.selection is not None
    assert read.selection.deployment_id is None
    assert read.status.selectable is True
    assert read.status.runnable is False
    assert read.status.status == "deployment_unmapped"
    with pytest.raises(ProviderConfigurationError, match="deployment binding"):
        service.build_runtime_selection(user_id=owner.id)


def test_memory_llm_runtime_requires_deployment_refs_for_active_text_selection(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Memory LLM execution no longer runs legacy provider/model-only rows."""

    owner, _ = identity_users
    llm_identity_db.add(
        UserMemoryLLMSelection(
            user_id=owner.id,
            provider=OPENAI_PROVIDER_ID,
            gate_model="gpt-5-nano",
            extraction_model="gpt-5-mini",
        )
    )
    llm_identity_db.flush()
    service = EmbeddingRuntimeSelectionService(
        credential_ref_resolver=lambda user_id, provider: LLMCredentialRef(
            user_id=user_id,
            provider=provider,
        ),
        db=llm_identity_db,
    )

    with pytest.raises(ProviderConfigurationError, match="deployment binding"):
        service.resolve_memory_llm_selection(user_id=owner.id)
