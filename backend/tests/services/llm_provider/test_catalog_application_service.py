"""Direct equivalence tests for transport-neutral LLM catalog projection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
)
from backend.services.llm_provider import LLMCatalogApplicationService
from backend.services.llm_provider import (
    catalog_application_service as catalog_application_module,
)
from backend.services.llm_provider.application_contracts import (
    CatalogOutcome,
    ConnectionCatalogMetadataOutcome,
    ProvingCatalogMetadataOutcome,
    RunnabilityOutcome,
    VerificationOutcome,
)
from backend.services.llm_provider.catalog_projection_service import (
    LLMCatalogProjectionService,
)
from backend.services.llm_provider.catalog_service import LLMProviderCatalogService
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.credential_service import LLMCredentialService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.migration_service import LLMProviderMigrationService
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    MISTRAL_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    ProviderConfigurationError,
)

_OWNER_SECRET_MARKER = "owner-plaintext-must-not-appear"
_FOREIGN_SECRET_MARKER = "foreign-plaintext-must-not-appear"


def _ref_payload(ref: Any) -> dict[str, object] | None:
    if ref is None:
        return None
    identity = getattr(ref, "connection_id", None)
    key = "connection_id" if identity is not None else "deployment_id"
    return {
        key: identity if identity is not None else ref.deployment_id,
        "expected_revision": ref.expected_revision,
    }


def _verification_payload(
    verification: VerificationOutcome | None,
) -> dict[str, object] | None:
    if verification is None:
        return None
    usage = verification.usage
    return {
        "status": verification.status,
        "code": verification.code,
        "message": verification.message,
        "retryable": verification.retryable,
        "observed_at": verification.observed_at,
        "expires_at": verification.expires_at,
        "model_present": verification.model_present,
        "usage": (
            {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
            if usage is not None
            else None
        ),
    }


def _runnability_payload(
    runnability: RunnabilityOutcome | None,
) -> dict[str, object] | None:
    if runnability is None:
        return None
    return {
        "status": runnability.status,
        "selectable": runnability.selectable,
        "runnable": runnability.runnable,
        "reason": runnability.reason,
    }


def _connection_payload(
    metadata: ConnectionCatalogMetadataOutcome | None,
) -> dict[str, object] | None:
    if metadata is None:
        return None
    return {
        "presetId": metadata.preset_id,
        "displayName": metadata.display_name,
        "enabled": metadata.enabled,
        "authMode": metadata.auth_mode,
        "userConfigFields": list(metadata.user_config_fields),
        "configFields": [
            {
                "name": field.name,
                "label": field.label,
                "fieldType": field.field_type,
                "required": field.required,
                "secret": field.secret,
            }
            for field in metadata.config_fields
        ],
        "lifecycleState": metadata.lifecycle_state,
        "connectionRef": _ref_payload(metadata.connection_ref),
        "deploymentRef": _ref_payload(metadata.deployment_ref),
        "verification": _verification_payload(metadata.verification),
        "runnability": _runnability_payload(metadata.runnability),
    }


def _proving_payload(
    metadata: ProvingCatalogMetadataOutcome | None,
) -> dict[str, object] | None:
    if metadata is None:
        return None
    return {
        "presetId": metadata.preset_id,
        "displayName": metadata.display_name,
        "enabled": metadata.enabled,
        "authMode": metadata.auth_mode,
        "userConfigFields": list(metadata.user_config_fields),
        "lifecycleState": metadata.lifecycle_state,
        "connectionRef": _ref_payload(metadata.connection_ref),
        "deploymentRef": _ref_payload(metadata.deployment_ref),
        "verification": _verification_payload(metadata.verification),
        "runnability": _runnability_payload(metadata.runnability),
    }


def _outcome_payload(outcome: CatalogOutcome) -> dict[str, object]:
    return {
        "providers": [
            {
                "id": provider.id,
                "label": provider.label,
                "capabilities": list(provider.capabilities),
                "available": provider.available,
                "selectable": provider.selectable,
                "credential": {
                    "user_id": provider.credential.user_id,
                    "provider": provider.credential.provider,
                    "enabled": provider.credential.enabled,
                    "has_api_key": provider.credential.has_api_key,
                    "masked_api_key": provider.credential.masked_api_key,
                    "connection_ref": _ref_payload(
                        provider.credential.connection_ref
                    ),
                    "auth_mode": provider.credential.auth_mode,
                },
                "models": [
                    {
                        "id": model.id,
                        "canonicalModelId": model.canonical_model_id,
                        "exactWireModelId": model.exact_wire_model_id,
                        "label": model.label,
                        "apiSurface": model.api_surface,
                        "capabilities": list(model.capabilities),
                        "contextWindowTokens": model.context_window_tokens,
                        "maxOutputTokens": model.max_output_tokens,
                        "reasoningEfforts": list(model.reasoning_efforts),
                        "visibleReasoningEfforts": list(
                            model.visible_reasoning_efforts
                        ),
                        "defaultReasoningEffort": model.default_reasoning_effort,
                        "defaultVisibleReasoningEffort": (
                            model.default_visible_reasoning_effort
                        ),
                        "toolChoiceModes": list(model.tool_choice_modes),
                        "structuredOutputStrategies": list(
                            model.structured_output_strategies
                        ),
                        "pricingStatus": model.pricing_status,
                        "deploymentRef": _ref_payload(model.deployment_ref),
                        "runnable": model.runnable,
                        "connection": _connection_payload(model.connection),
                        "proving": _proving_payload(model.proving),
                    }
                    for model in provider.models
                ],
                "defaultModel": provider.default_model,
            }
            for provider in outcome.providers
        ]
    }


def _seed_projection_rows(
    db: Session,
    *,
    owner: User,
    other: User,
) -> tuple[str, str, str]:
    db.add_all(
        [
            UserLLMProviderCredential(
                user_id=owner.id,
                provider="openai",
                encrypted_api_key=f"encrypted:{_OWNER_SECRET_MARKER}",
                enabled=True,
            ),
            UserLLMSelection(
                user_id=owner.id,
                provider="openai",
                model="gpt-5.2",
            ),
            UserLLMProviderCredential(
                user_id=other.id,
                provider="openai",
                encrypted_api_key=f"encrypted:{_FOREIGN_SECRET_MARKER}",
                enabled=True,
            ),
            UserLLMSelection(
                user_id=other.id,
                provider="openai",
                model="gpt-5-mini",
            ),
        ]
    )
    db.flush()
    migrations = LLMProviderMigrationService(db)
    migrations.backfill_deployment_identity_for_user(owner.id)
    migrations.backfill_deployment_identity_for_user(other.id)

    connections = LLMConnectionService(db)
    deployments = LLMDeploymentService(db)
    hf_connection = connections.create_draft(
        user_id=owner.id,
        display_name="HF GPT-OSS",
        connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="huggingface",
    )
    hf_deployment, _ = deployments.create_preset_deployment(
        user_id=owner.id,
        connection_id=hf_connection.id,
        expected_connection_revision=1,
        wire_model_id="openai/gpt-oss-20b:fireworks-ai",
        canonical_model_id="openai/gpt-oss-20b",
        display_name="GPT-OSS 20B via HF",
    )
    custom_connection = connections.create_draft(
        user_id=owner.id,
        display_name="Hidden custom endpoint",
        connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="organization_managed",
        non_secret_config={"base_url": "https://llm.example.test/team"},
    )
    custom_deployment, _ = deployments.create_preset_deployment(
        user_id=owner.id,
        connection_id=custom_connection.id,
        expected_connection_revision=1,
        wire_model_id="team/chat-model",
        display_name="Team Chat Model",
    )
    proving_connection = connections.create_gpt_oss_20b_proving_draft(
        user_id=owner.id
    )
    proving_deployment, _ = deployments.create_gpt_oss_20b_proving_deployment(
        user_id=owner.id,
        connection_id=proving_connection.id,
        expected_connection_revision=1,
    )
    proving_connection.legacy_default_provider = GPT_OSS_20B_PROVING_PRESET_ID
    db.flush()
    foreign_connection = LLMConnectionService(db).list_for_user(user_id=other.id)[0]
    return (
        str(hf_deployment.id),
        str(custom_deployment.id),
        f"{foreign_connection.id}:{proving_deployment.id}",
    )


def test_catalog_projection_matches_active_router_order_and_fields(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Typed projection equals the active helpers for rich owner-scoped data."""

    owner, other = identity_users
    hf_deployment_id, custom_deployment_id, foreign_and_proving_ids = (
        _seed_projection_rows(llm_identity_db, owner=owner, other=other)
    )
    foreign_connection_id, proving_deployment_id = foreign_and_proving_ids.split(":")
    catalog = LLMProviderCatalogService()
    providers = catalog.list_providers()
    credentials = LLMCredentialService(llm_identity_db, catalog_service=catalog)
    statuses = {
        provider.id: credentials.get_masked_status(owner.id, provider.id)
        for provider in providers
    }
    transaction_events: list[str] = []
    monkeypatch.setattr(
        llm_identity_db,
        "commit",
        lambda: transaction_events.append("commit"),
    )
    monkeypatch.setattr(
        llm_identity_db,
        "rollback",
        lambda: transaction_events.append("rollback"),
    )
    monkeypatch.setattr(
        LLMProviderMigrationService,
        "backfill_deployment_identity_for_user",
        lambda *_args, **_kwargs: pytest.fail("projection attempted backfill"),
    )

    outcome = LLMCatalogProjectionService(llm_identity_db).project(
        user_id=owner.id,
        providers=providers,
        credential_statuses=statuses,
    )

    payload = _outcome_payload(outcome)
    assert transaction_events == []
    assert [provider["id"] for provider in payload["providers"]] == [
        "openai",
        "anthropic",
        MISTRAL_OPENAI_COMPATIBLE_PRESET_ID,
        "nvidia_nim_openai_compatible_chat",
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        "vllm_openai_compatible_chat",
    ]
    reviewed = {
        provider["id"]: provider for provider in payload["providers"][2:]
    }
    assert reviewed[HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID]["label"] == (
        "Hugging Face"
    )
    selected_hf = reviewed[HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID]["models"]
    assert len(selected_hf) == 1
    assert selected_hf[0]["deploymentRef"]["deployment_id"] == hf_deployment_id
    assert [
        field["name"]
        for field in reviewed[OLLAMA_OPENAI_COMPATIBLE_PRESET_ID]["models"][0][
            "connection"
        ]["configFields"]
    ] == ["base_url", "api_key", "wire_model_id"]
    openai = payload["providers"][0]
    proving = next(
        model for model in openai["models"] if model["id"] == "gpt-oss-20b"
    )["proving"]
    assert proving["deploymentRef"]["deployment_id"] == proving_deployment_id
    assert proving["runnability"]["status"] == "credential_missing"
    rendered = repr(outcome)
    assert custom_deployment_id not in rendered
    assert foreign_connection_id not in rendered
    assert _OWNER_SECRET_MARKER not in rendered
    assert _FOREIGN_SECRET_MARKER not in rendered


def test_catalog_projection_empty_state_has_no_workflow_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Projection owns no backfill, transaction, mutation, or secret resolution."""

    owner, _ = identity_users
    catalog = LLMProviderCatalogService()
    providers = catalog.list_providers()
    credentials = LLMCredentialService(llm_identity_db, catalog_service=catalog)
    statuses = {
        provider.id: credentials.get_masked_status(owner.id, provider.id)
        for provider in providers
    }
    events: list[str] = []
    monkeypatch.setattr(
        llm_identity_db,
        "commit",
        lambda: events.append("commit"),
    )
    monkeypatch.setattr(
        llm_identity_db,
        "rollback",
        lambda: events.append("rollback"),
    )
    monkeypatch.setattr(
        LLMProviderMigrationService,
        "backfill_deployment_identity_for_user",
        lambda *_args, **_kwargs: pytest.fail("projection attempted backfill"),
    )
    monkeypatch.setattr(
        LLMCredentialService,
        "resolve_connection_auth",
        lambda *_args, **_kwargs: pytest.fail("projection resolved a secret"),
    )

    outcome = LLMCatalogProjectionService(llm_identity_db).project(
        user_id=owner.id,
        providers=providers,
        credential_statuses=statuses,
    )

    payload = _outcome_payload(outcome)
    assert [provider["id"] for provider in payload["providers"]] == [
        "openai",
        "anthropic",
        MISTRAL_OPENAI_COMPATIBLE_PRESET_ID,
        "nvidia_nim_openai_compatible_chat",
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        "vllm_openai_compatible_chat",
    ]
    assert events == []
    assert all(
        provider.credential.masked_api_key in (None, "***")
        for provider in outcome.providers
    )

    unsafe_statuses = dict(statuses)
    unsafe_statuses["openai"] = replace(
        statuses["openai"],
        masked_api_key=_OWNER_SECRET_MARKER,
    )
    sanitized = LLMCatalogProjectionService(llm_identity_db).project(
        user_id=owner.id,
        providers=providers,
        credential_statuses=unsafe_statuses,
    )
    assert _OWNER_SECRET_MARKER not in repr(sanitized)

    foreign_statuses = dict(statuses)
    foreign_statuses["openai"] = replace(statuses["openai"], user_id=owner.id + 1)
    with pytest.raises(
        ProviderConfigurationError,
        match="Credential status owner does not match catalog owner",
    ):
        LLMCatalogProjectionService(llm_identity_db).project(
            user_id=owner.id,
            providers=providers,
            credential_statuses=foreign_statuses,
        )


def _seed_unmapped_legacy_catalog_rows(
    db: Session,
    *,
    owner: User,
    other: User | None = None,
) -> None:
    rows: list[object] = [
        UserLLMProviderCredential(
            user_id=owner.id,
            provider="openai",
            encrypted_api_key=f"encrypted:{_OWNER_SECRET_MARKER}",
            enabled=True,
        ),
        UserLLMSelection(
            user_id=owner.id,
            provider="openai",
            model="gpt-5.2",
        ),
    ]
    if other is not None:
        rows.extend(
            [
                UserLLMProviderCredential(
                    user_id=other.id,
                    provider="openai",
                    encrypted_api_key=f"encrypted:{_FOREIGN_SECRET_MARKER}",
                    enabled=True,
                ),
                UserLLMSelection(
                    user_id=other.id,
                    provider="openai",
                    model="gpt-5-mini",
                ),
            ]
        )
    db.add_all(rows)
    db.commit()


def _selection_for(db: Session, *, user_id: int) -> UserLLMSelection:
    return db.execute(
        select(UserLLMSelection).where(UserLLMSelection.user_id == user_id)
    ).scalar_one()


def test_catalog_application_commits_once_after_exact_owner_projection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Successful application projection persists only owner backfill once."""

    owner, other = identity_users
    _seed_unmapped_legacy_catalog_rows(
        llm_identity_db,
        owner=owner,
        other=other,
    )
    assert _selection_for(llm_identity_db, user_id=owner.id).deployment_id is None
    assert _selection_for(llm_identity_db, user_id=other.id).deployment_id is None

    transaction_events: list[str] = []
    original_commit = llm_identity_db.commit

    def recording_commit() -> None:
        transaction_events.append("commit")
        original_commit()

    monkeypatch.setattr(llm_identity_db, "commit", recording_commit)
    monkeypatch.setattr(
        llm_identity_db,
        "rollback",
        lambda: transaction_events.append("rollback"),
    )

    outcome = LLMCatalogApplicationService(llm_identity_db).list_models(
        user_id=owner.id
    )

    assert transaction_events == ["commit"]
    llm_identity_db.expire_all()
    owner_selection = _selection_for(llm_identity_db, user_id=owner.id)
    foreign_selection = _selection_for(llm_identity_db, user_id=other.id)
    assert owner_selection.deployment_id is not None
    assert foreign_selection.deployment_id is None

    catalog = LLMProviderCatalogService()
    providers = catalog.list_providers()
    credentials = LLMCredentialService(llm_identity_db, catalog_service=catalog)
    statuses = {
        provider.id: credentials.get_masked_status(owner.id, provider.id)
        for provider in providers
    }
    expected = LLMCatalogProjectionService(llm_identity_db).project(
        user_id=owner.id,
        providers=providers,
        credential_statuses=statuses,
    )
    assert outcome == expected
    assert transaction_events == ["commit"]
    rendered = f"{outcome!r}\n{caplog.text}"
    assert _OWNER_SECRET_MARKER not in rendered
    assert _FOREIGN_SECRET_MARKER not in rendered


@pytest.mark.parametrize(
    "failure_stage",
    ("backfill", "credential", "projection", "outcome", "commit"),
)
def test_catalog_application_rolls_back_every_failure_without_partial_backfill(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Every workflow failure rolls back and raises one sanitized provider error."""

    owner, _ = identity_users
    _seed_unmapped_legacy_catalog_rows(llm_identity_db, owner=owner)
    events: list[str] = []
    original_rollback = llm_identity_db.rollback

    def fail_stage(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"{_OWNER_SECRET_MARKER}:{failure_stage}")

    def recording_rollback() -> None:
        events.append("rollback")
        original_rollback()

    def unexpected_commit() -> None:
        events.append("commit")
        if failure_stage == "commit":
            fail_stage()
        pytest.fail("application committed before a failed workflow completed")

    monkeypatch.setattr(llm_identity_db, "commit", unexpected_commit)
    monkeypatch.setattr(llm_identity_db, "rollback", recording_rollback)
    if failure_stage == "backfill":
        monkeypatch.setattr(
            LLMProviderMigrationService,
            "backfill_deployment_identity_for_user",
            fail_stage,
        )
    elif failure_stage == "credential":
        monkeypatch.setattr(
            LLMCredentialService,
            "get_masked_status",
            fail_stage,
        )
    elif failure_stage == "projection":
        monkeypatch.setattr(
            LLMCatalogProjectionService,
            "project",
            fail_stage,
        )
    elif failure_stage == "outcome":
        monkeypatch.setattr(
            catalog_application_module,
            "CatalogOutcome",
            fail_stage,
        )

    with pytest.raises(
        ProviderConfigurationError,
        match="LLM catalog application failed",
    ) as captured:
        LLMCatalogApplicationService(llm_identity_db).list_models(
            user_id=owner.id
        )

    assert captured.value.__cause__ is None
    assert events == (["commit", "rollback"] if failure_stage == "commit" else ["rollback"])
    llm_identity_db.expire_all()
    assert _selection_for(llm_identity_db, user_id=owner.id).deployment_id is None
    rendered = f"{captured.value!s}\n{captured.value!r}\n{caplog.text}"
    assert _OWNER_SECRET_MARKER not in rendered
