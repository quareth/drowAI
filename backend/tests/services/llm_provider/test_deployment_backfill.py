"""Tests for deterministic legacy-default deployment identity backfill."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
)
from agent.providers.llm.profiles import ANTHROPIC_LISTABLE_MODEL_IDS
from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
    UserSettings,
)
from backend.services.llm_provider.migration_service import (
    LLMProviderMigrationService,
    deterministic_legacy_connection_id,
    deterministic_legacy_deployment_id,
)
from backend.services.llm_provider.runtime_config_service import (
    LLMRuntimeConfigService,
)
from backend.services.llm_provider.selection_service import (
    LLMProviderSelectionService,
)
from backend.services.llm_provider.types import LLMRuntimeSelectionV2


def test_backfill_is_deterministic_idempotent_and_preserves_exact_models(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Credentials and distinct exact models map once to stable identity rows."""

    owner, _ = identity_users
    openai_ciphertext = "encrypted-openai-ciphertext"
    anthropic_ciphertext = "encrypted-anthropic-ciphertext"
    conversation = UserLLMSelection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model="Org/Model-Case:Exact",
    )
    reporting = UserReportingLLMSelection(
        user_id=owner.id,
        provider=ANTHROPIC_PROVIDER_ID,
        model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
    )
    memory = UserMemoryLLMSelection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        gate_model="Org/Model-Case:Exact",
        extraction_model="gpt-5-mini",
    )
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key=openai_ciphertext,
                enabled=True,
            ),
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=ANTHROPIC_PROVIDER_ID,
                encrypted_api_key=anthropic_ciphertext,
                enabled=True,
            ),
            conversation,
            reporting,
            memory,
        ]
    )
    llm_identity_db.flush()

    service = LLMProviderMigrationService(llm_identity_db)
    first = service.backfill_deployment_identity_for_user(owner.id)

    openai_connection_id = deterministic_legacy_connection_id(
        owner.id,
        OPENAI_PROVIDER_ID,
    )
    anthropic_connection_id = deterministic_legacy_connection_id(
        owner.id,
        ANTHROPIC_PROVIDER_ID,
    )
    connections = tuple(
        llm_identity_db.execute(
            select(LLMInferenceConnection).order_by(
                LLMInferenceConnection.legacy_default_provider
            )
        ).scalars()
    )
    assert {connection.id for connection in connections} == {
        openai_connection_id,
        anthropic_connection_id,
    }
    assert {connection.state for connection in connections} == {"enabled"}
    assert {connection.revision for connection in connections} == {1}

    exact_deployment_id = deterministic_legacy_deployment_id(
        openai_connection_id,
        "Org/Model-Case:Exact",
    )
    assert conversation.deployment_id == exact_deployment_id
    assert memory.gate_deployment_id == exact_deployment_id
    assert memory.extraction_deployment_id == deterministic_legacy_deployment_id(
        openai_connection_id,
        "gpt-5-mini",
    )
    assert reporting.deployment_id == deterministic_legacy_deployment_id(
        anthropic_connection_id,
        ANTHROPIC_LISTABLE_MODEL_IDS[0],
    )
    exact_deployment = llm_identity_db.get(
        LLMModelDeployment,
        exact_deployment_id,
    )
    assert exact_deployment.wire_model_id == "Org/Model-Case:Exact"

    credentials = tuple(
        llm_identity_db.execute(
            select(UserLLMProviderCredential).order_by(
                UserLLMProviderCredential.provider
            )
        ).scalars()
    )
    assert [row.encrypted_api_key for row in credentials] == [
        anthropic_ciphertext,
        openai_ciphertext,
    ]
    assert first.created_connections == 2
    assert first.created_deployments == 3
    assert first.mapped_selection_refs == 4

    second = service.backfill_deployment_identity_for_user(owner.id)
    assert second.created == 0
    assert second.mapped_selection_refs == 0
    assert second.failed == 0
    assert tuple(
        llm_identity_db.execute(select(LLMInferenceConnection)).scalars()
    ) == connections


def test_backfill_copies_legacy_ciphertext_without_plaintext_round_trip(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Legacy settings ciphertext is copied unchanged before connection mapping."""

    owner, _ = identity_users
    ciphertext = "gAAAAAB-existing-fernet-ciphertext"
    exact_model = "Org/Legacy-Wire-Model:Exact"
    settings = UserSettings(user_id=owner.id)
    settings.openai_api_key = ciphertext
    settings.openai_model = exact_model
    llm_identity_db.add(settings)
    llm_identity_db.flush()

    stats = LLMProviderMigrationService(
        llm_identity_db
    ).backfill_deployment_identity_for_user(owner.id)

    credential = llm_identity_db.execute(
        select(UserLLMProviderCredential).where(
            UserLLMProviderCredential.user_id == owner.id
        )
    ).scalar_one()
    selection = llm_identity_db.execute(
        select(UserLLMSelection).where(UserLLMSelection.user_id == owner.id)
    ).scalar_one()
    assert credential.encrypted_api_key == ciphertext
    assert selection.model == exact_model
    assert selection.deployment_id is not None
    deployment = llm_identity_db.get(LLMModelDeployment, selection.deployment_id)
    assert deployment is not None
    assert deployment.wire_model_id == exact_model
    assert stats.copied_credentials == 1


def test_selection_without_credential_remains_unmapped_and_not_runnable(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Backfill does not invent credentials or substitute another deployment."""

    owner, _ = identity_users
    selection = UserLLMSelection(
        user_id=owner.id,
        provider=ANTHROPIC_PROVIDER_ID,
        model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
    )
    llm_identity_db.add(selection)
    llm_identity_db.flush()

    stats = LLMProviderMigrationService(
        llm_identity_db
    ).backfill_deployment_identity_for_user(owner.id)

    assert selection.deployment_id is None
    assert stats.unmapped == 1
    assert llm_identity_db.execute(
        select(UserLLMProviderCredential).where(
            UserLLMProviderCredential.user_id == owner.id
        )
    ).scalar_one_or_none() is None
    assert llm_identity_db.execute(
        select(LLMInferenceConnection).where(
            LLMInferenceConnection.user_id == owner.id
        )
    ).scalar_one_or_none() is None
    status = LLMProviderSelectionService(llm_identity_db).get_selection_read(
        owner.id
    ).status
    assert status.selectable is True
    assert status.runnable is False
    assert status.status == "deployment_unmapped"


def test_existing_explicit_legacy_default_is_never_replaced(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Backfill maps through an existing designation without implicit takeover."""

    owner, _ = identity_users
    existing_id = uuid4()
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            LLMInferenceConnection(
                id=existing_id,
                user_id=owner.id,
                display_name="Existing Default",
                connection_preset_id=OPENAI_PROVIDER_ID,
                runtime_family_id="openai_native",
                transport_origin="backend",
                endpoint_policy_id="fixed_provider_v1",
                state="enabled",
                revision=7,
                legacy_default_provider=OPENAI_PROVIDER_ID,
            ),
            UserLLMSelection(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                model="gpt-5-mini",
            ),
        ]
    )
    llm_identity_db.flush()

    LLMProviderMigrationService(
        llm_identity_db
    ).backfill_deployment_identity_for_user(owner.id)

    connections = tuple(
        llm_identity_db.execute(
            select(LLMInferenceConnection).where(
                LLMInferenceConnection.user_id == owner.id
            )
        ).scalars()
    )
    assert len(connections) == 1
    assert connections[0].id == existing_id
    assert connections[0].revision == 7


def test_readiness_blocks_until_deterministic_mapping_is_complete(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Rollout cannot prefer deployment refs before repairable rows are mapped."""

    owner, _ = identity_users
    selection = UserLLMSelection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5-mini",
    )
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            selection,
        ]
    )
    llm_identity_db.flush()
    service = LLMProviderMigrationService(llm_identity_db)

    before = service.assess_deployment_backfill_readiness()

    assert before.ready is False
    assert before.missing_legacy_connections == 1
    assert before.mapping_required == 1

    first = service.prepare_deployment_backfill_readiness()
    second = service.prepare_deployment_backfill_readiness()

    assert first.ready is True
    assert first.created > 0
    assert selection.deployment_id is not None
    assert second.ready is True
    assert second.created == 0
    assert second.failed == 0


def test_readiness_blocks_selection_refs_outside_legacy_default_connection(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """A matching deployment is not ready unless it is on the legacy default."""

    owner, _ = identity_users
    default_connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name="Existing Legacy Default",
        connection_preset_id=OPENAI_PROVIDER_ID,
        runtime_family_id="openai_native",
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=1,
        legacy_default_provider=OPENAI_PROVIDER_ID,
    )
    other_connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name="Other OpenAI Connection",
        connection_preset_id=OPENAI_PROVIDER_ID,
        runtime_family_id="openai_native",
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=1,
        legacy_default_provider=None,
    )
    other_deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=other_connection.id,
        wire_model_id="gpt-5-mini",
        display_name="gpt-5-mini",
        discovery_source="test",
        lifecycle_state="active",
        availability_state="unknown",
        enabled=True,
        revision=1,
    )
    selection = UserLLMSelection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5-mini",
        deployment_id=other_deployment.id,
    )
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            default_connection,
            other_connection,
            other_deployment,
            selection,
        ]
    )
    llm_identity_db.flush()

    report = LLMProviderMigrationService(
        llm_identity_db
    ).prepare_deployment_backfill_readiness()

    assert report.ready is False
    assert report.mapping_required == 1
    assert report.missing_legacy_connections == 0
    assert selection.deployment_id == other_deployment.id


def test_conversation_runtime_prefers_deployment_ref_after_authority_switch(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Runtime reads use saved deployment refs after the authority switch."""

    owner, _ = identity_users
    default_connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name="Existing Legacy Default",
        connection_preset_id=OPENAI_PROVIDER_ID,
        runtime_family_id="openai_native",
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=1,
        legacy_default_provider=OPENAI_PROVIDER_ID,
    )
    other_connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=owner.id,
        display_name="Other OpenAI Connection",
        connection_preset_id=OPENAI_PROVIDER_ID,
        runtime_family_id="openai_native",
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=1,
        legacy_default_provider=None,
    )
    other_deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=other_connection.id,
        wire_model_id="gpt-5-mini",
        display_name="gpt-5-mini",
        discovery_source="test",
        lifecycle_state="active",
        availability_state="unknown",
        enabled=True,
        revision=1,
    )
    selection = UserLLMSelection(
        user_id=owner.id,
        provider=OPENAI_PROVIDER_ID,
        model="gpt-5-mini",
        deployment_id=other_deployment.id,
    )
    llm_identity_db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key="ciphertext",
                enabled=True,
            ),
            default_connection,
            other_connection,
            other_deployment,
            selection,
        ]
    )
    llm_identity_db.flush()

    runtime = LLMRuntimeConfigService(
        llm_identity_db
    ).build_conversation_runtime_selection(user_id=owner.id)

    assert isinstance(runtime, LLMRuntimeSelectionV2)
    assert runtime.deployment_ref.deployment_id == str(other_deployment.id)
    assert runtime.legacy_provider == OPENAI_PROVIDER_ID
    assert runtime.legacy_model == "gpt-5-mini"
    assert selection.deployment_id == other_deployment.id


def test_readiness_records_auth_missing_selection_without_blocking_rollout(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """An auth-missing selection is explicit, selectable, and still unrunnable."""

    owner, _ = identity_users
    selection = UserLLMSelection(
        user_id=owner.id,
        provider=ANTHROPIC_PROVIDER_ID,
        model=ANTHROPIC_LISTABLE_MODEL_IDS[0],
    )
    llm_identity_db.add(selection)
    llm_identity_db.flush()

    report = LLMProviderMigrationService(
        llm_identity_db
    ).prepare_deployment_backfill_readiness()

    assert report.ready is True
    assert report.auth_missing == 1
    assert report.mapping_required == 0
    assert selection.deployment_id is None
    status = LLMProviderSelectionService(llm_identity_db).get_selection_read(
        owner.id
    ).status
    assert status.selectable is True
    assert status.runnable is False
    assert status.status == "deployment_unmapped"
