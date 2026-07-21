"""Compatibility tests for deployment-aware checkpoint runtime payloads."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import LLMModelDeployment, User
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMConnectionService,
    LLMDeploymentService,
    LLMDeploymentNotFoundError,
    LLMProviderMigrationService,
    LLMProviderSelectionService,
    LLMRuntimeAccessContext,
    LLMRuntimeClientResolver,
    LLMRuntimeConfigService,
)
from backend.services.llm_provider.operation_registry import (
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    LLMRuntimeSelectionV2,
    ProviderConfigurationError,
)


def _create_user(db, prefix: str) -> User:
    user = User(
        username=f"{prefix}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_legacy_deployment(db, *, user_id: int, model: str) -> str:
    credential_service = LLMCredentialService(db)
    credential_service.upsert_api_key(
        user_id=user_id,
        provider=OPENAI_PROVIDER_ID,
        api_key=f"sk-{uuid4().hex}",
    )
    selection_service = LLMProviderSelectionService(
        db,
        credential_service=credential_service,
    )
    selection = selection_service.set_selection(
        user_id=user_id,
        provider=OPENAI_PROVIDER_ID,
        model=model,
    )
    LLMProviderMigrationService(db).backfill_deployment_identity_for_user(user_id)
    db.commit()
    db.refresh(selection)
    assert selection.deployment_id is not None
    return str(selection.deployment_id)


def _create_compatible_deployment(
    db,
    *,
    user_id: int,
    preset_id: str,
    operator_id: str,
    wire_model_id: str,
) -> str:
    connection = LLMConnectionService(db).create_draft(
        user_id=user_id,
        display_name=f"Compatible endpoint {preset_id}",
        connection_preset_id=preset_id,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id=operator_id,
    )
    deployment, _route = LLMDeploymentService(db).create_preset_deployment(
        user_id=user_id,
        connection_id=connection.id,
        expected_connection_revision=connection.revision,
        wire_model_id=wire_model_id,
        canonical_model_id="openai/gpt-oss-20b",
        display_name="Compatible GPT-OSS deployment",
    )
    db.commit()
    return str(deployment.id)


def test_legacy_checkpoint_hint_normalizes_to_checkpoint_safe_deployment_ref() -> None:
    """Old provider/model hints map to deployment identity without live material."""

    db = SessionLocal()
    try:
        user = _create_user(db, "legacy-checkpoint-compatible")
        deployment_id = _create_legacy_deployment(
            db,
            user_id=user.id,
            model="gpt-5.2",
        )
        LLMProviderSelectionService(db).set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5-mini",
        )
        db.commit()

        selection = LLMRuntimeConfigService(db).build_continuation_selection(
            user_id=user.id,
            checkpoint_hint={
                "provider": OPENAI_PROVIDER_ID,
                "model": "gpt-5.2",
                "reasoning_effort": "low",
                "credential_ref": {"user_id": 999999, "provider": "openai"},
                "endpoint": "https://checkpoint.example.invalid/v1",
                "api_key": "sk-should-not-survive",
            },
        )
        payload = selection.to_dict()

        assert isinstance(selection, LLMRuntimeSelectionV2)
        assert payload["deployment_ref"]["deployment_id"] == deployment_id
        assert payload["legacy_provider"] == OPENAI_PROVIDER_ID
        assert payload["legacy_model"] == "gpt-5.2"
        assert payload["reasoning_effort"] == "low"
        serialized = repr(payload).lower()
        for forbidden in (
            "credential_ref",
            "endpoint",
            "api_key",
            "secret",
            "sk-should-not-survive",
        ):
            assert forbidden not in serialized
    finally:
        db.close()


def test_v2_checkpoint_hint_extraction_keeps_only_safe_identity_fields() -> None:
    """Checkpoint scanning preserves V2 identity while dropping live facts."""

    deployment_id = str(uuid4())
    values = {
        "facts": {
            "metadata": {
                "llm_runtime_selection": {
                    "schema_version": 2,
                    "deployment_ref": {
                        "deployment_id": deployment_id,
                        "expected_revision": 3,
                        "endpoint": "https://checkpoint.example.invalid/v1",
                    },
                    "legacy_provider": OPENAI_PROVIDER_ID,
                    "legacy_model": "gpt-5.2",
                    "api_key": "sk-should-not-survive",
                }
            }
        }
    }

    hint = CheckpointContinuationService._extract_checkpoint_runtime_hint(values)

    assert hint == {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 3,
        },
        "legacy_provider": OPENAI_PROVIDER_ID,
        "legacy_model": "gpt-5.2",
    }
    assert "sk-should-not-survive" not in repr(hint)
    assert "endpoint" not in repr(hint)


def test_v2_checkpoint_identity_wins_over_outer_legacy_diagnostics() -> None:
    """A modern nested deployment ref is authoritative over old outer fields."""

    deployment_id = str(uuid4())
    values = {
        "provider": OPENAI_PROVIDER_ID,
        "model": "legacy-display-model",
        "facts": {
            "metadata": {
                "llm_runtime_selection": {
                    "schema_version": 2,
                    "deployment_ref": {
                        "deployment_id": deployment_id,
                        "expected_revision": 4,
                    },
                    "reasoning_effort": "medium",
                    "legacy_provider": OPENAI_PROVIDER_ID,
                    "legacy_model": "canonical-display-model",
                }
            }
        },
    }

    hint = CheckpointContinuationService._extract_checkpoint_runtime_hint(values)

    assert hint == {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 4,
        },
        "reasoning_effort": "medium",
        "legacy_provider": OPENAI_PROVIDER_ID,
        "legacy_model": "canonical-display-model",
    }


def test_mixed_checkpoint_resumes_its_deployment_not_outer_legacy_model() -> None:
    """The production failure shape reaches continuation with its deployment ref."""

    db = SessionLocal()
    try:
        user = _create_user(db, "mixed-checkpoint-resume")
        deployment_id = _create_legacy_deployment(
            db,
            user_id=user.id,
            model="gpt-5.2",
        )
        deployment = db.get(LLMModelDeployment, UUID(deployment_id))
        assert deployment is not None
        values = {
            "provider": OPENAI_PROVIDER_ID,
            "model": "unmapped-outer-model",
            "facts": {
                "metadata": {
                    "llm_runtime_selection": {
                        "schema_version": 2,
                        "deployment_ref": {
                            "deployment_id": deployment_id,
                            "expected_revision": int(deployment.revision),
                        },
                        "legacy_provider": OPENAI_PROVIDER_ID,
                        "legacy_model": "gpt-5.2",
                    }
                }
            },
        }

        hint = CheckpointContinuationService._extract_checkpoint_runtime_hint(values)
        selection = LLMRuntimeConfigService(db).build_continuation_selection(
            user_id=user.id,
            checkpoint_hint=hint,
        )

        assert selection.deployment_ref.deployment_id == deployment_id
        assert selection.legacy_model == "gpt-5.2"
    finally:
        db.close()


def test_identical_v2_checkpoint_mirrors_are_accepted() -> None:
    """Historical duplicate mirrors are accepted when routing identity agrees."""

    deployment_id = str(uuid4())
    selection = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 2,
        },
    }
    values = {
        "llm_runtime_selection": selection,
        "facts": {"metadata": {"llm_runtime_selection": dict(selection)}},
    }

    assert (
        CheckpointContinuationService._extract_checkpoint_runtime_hint(values)
        == selection
    )


def test_conflicting_v2_checkpoint_mirrors_fail_closed() -> None:
    """Conflicting durable deployment identities never depend on scan order."""

    values = {
        "llm_runtime_selection": {
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": str(uuid4()),
                "expected_revision": 2,
            },
        },
        "facts": {
            "metadata": {
                "llm_runtime_selection": {
                    "schema_version": 2,
                    "deployment_ref": {
                        "deployment_id": str(uuid4()),
                        "expected_revision": 2,
                    },
                }
            }
        },
    }

    with pytest.raises(ProviderConfigurationError, match="conflicting"):
        CheckpointContinuationService._extract_checkpoint_runtime_hint(values)


def test_malformed_v2_checkpoint_does_not_downgrade_to_legacy() -> None:
    """A claimed modern identity fails closed even when legacy labels are valid."""

    values = {
        "provider": OPENAI_PROVIDER_ID,
        "model": "gpt-5.2",
        "facts": {
            "metadata": {
                "llm_runtime_selection": {
                    "schema_version": 2,
                    "deployment_ref": {
                        "deployment_id": "not-a-uuid",
                        "expected_revision": 1,
                    },
                }
            }
        },
    }

    with pytest.raises(ProviderConfigurationError, match="invalid"):
        CheckpointContinuationService._extract_checkpoint_runtime_hint(values)


def test_unsupported_checkpoint_schema_does_not_downgrade_to_legacy() -> None:
    """Unknown versioned identity is an explicit unrunnable checkpoint."""

    values = {
        "provider": OPENAI_PROVIDER_ID,
        "model": "gpt-5.2",
        "facts": {
            "metadata": {
                "llm_runtime_selection": {
                    "schema_version": 3,
                    "deployment_ref": {
                        "deployment_id": str(uuid4()),
                        "expected_revision": 1,
                    },
                }
            }
        },
    }

    with pytest.raises(ProviderConfigurationError, match="invalid"):
        CheckpointContinuationService._extract_checkpoint_runtime_hint(values)


def test_v2_checkpoint_continuation_preserves_checkpoint_revision_until_resolver() -> None:
    """Resume keeps checkpointed deployment revision so stale revisions fail closed."""

    db = SessionLocal()
    try:
        user = _create_user(db, "v2-checkpoint-stale-revision")
        deployment_id = _create_legacy_deployment(
            db,
            user_id=user.id,
            model="gpt-5.2",
        )
        deployment = db.get(LLMModelDeployment, UUID(deployment_id))
        assert deployment is not None
        checkpoint_revision = int(deployment.revision)
        deployment.revision = checkpoint_revision + 1
        db.commit()

        selection = LLMRuntimeConfigService(db).build_continuation_selection(
            user_id=user.id,
            checkpoint_hint={
                "schema_version": 2,
                "deployment_ref": {
                    "deployment_id": deployment_id,
                    "expected_revision": checkpoint_revision,
                },
                "legacy_provider": OPENAI_PROVIDER_ID,
                "legacy_model": "gpt-5.2",
            },
        )

        assert isinstance(selection, LLMRuntimeSelectionV2)
        assert selection.deployment_ref.expected_revision == checkpoint_revision
        with pytest.raises(LLMDeploymentNotFoundError):
            LLMRuntimeClientResolver(LLMCredentialService(db), db=db).resolve_target(
                selection,
                access_context=LLMRuntimeAccessContext(runtime_user_id=user.id),
                purpose="checkpoint-resume",
            )
    finally:
        db.close()


def test_unmapped_legacy_checkpoint_hint_fails_without_current_selection_fallback() -> None:
    """Unmapped historical selections are actionable unrunnable resumes."""

    db = SessionLocal()
    try:
        user = _create_user(db, "legacy-checkpoint-unmapped")
        _create_legacy_deployment(db, user_id=user.id, model="gpt-5-mini")

        with pytest.raises(ProviderConfigurationError, match="unmapped.*reselect"):
            LLMRuntimeConfigService(db).build_continuation_selection(
                user_id=user.id,
                checkpoint_hint={
                    "provider": OPENAI_PROVIDER_ID,
                    "model": "legacy-unmapped-wire-model",
                },
            )
    finally:
        db.close()


def test_legacy_compatibility_identity_maps_across_wire_model_alias() -> None:
    """Legacy conversion compares effective identity, not one exact wire string."""

    db = SessionLocal()
    try:
        user = _create_user(db, "legacy-compatible-alias")
        deployment_id = _create_compatible_deployment(
            db,
            user_id=user.id,
            preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            operator_id="nvidia_nim",
            wire_model_id="openai/gpt-oss-20b",
        )

        selection = LLMRuntimeConfigService(db).build_continuation_selection(
            user_id=user.id,
            checkpoint_hint={
                "provider": OPENAI_PROVIDER_ID,
                "model": "gpt-oss-20b",
            },
        )

        assert selection.deployment_ref.deployment_id == deployment_id
    finally:
        db.close()


def test_legacy_compatibility_identity_fails_when_multiple_deployments_match() -> None:
    """Legacy labels never choose arbitrarily between serving deployments."""

    db = SessionLocal()
    try:
        user = _create_user(db, "legacy-compatible-ambiguous")
        _create_compatible_deployment(
            db,
            user_id=user.id,
            preset_id=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            operator_id="nvidia_nim",
            wire_model_id="openai/gpt-oss-20b",
        )
        _create_compatible_deployment(
            db,
            user_id=user.id,
            preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            operator_id="huggingface",
            wire_model_id="openai/gpt-oss-20b:compatible-route",
        )

        with pytest.raises(ProviderConfigurationError, match="ambiguous"):
            LLMRuntimeConfigService(db).build_continuation_selection(
                user_id=user.id,
                checkpoint_hint={
                    "provider": OPENAI_PROVIDER_ID,
                    "model": "gpt-oss-20b",
                },
            )
    finally:
        db.close()
