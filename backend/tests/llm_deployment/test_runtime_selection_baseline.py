"""Deployment baseline tests for runtime selection and continuation contracts."""

from __future__ import annotations

from uuid import uuid4

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import User
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.langgraph_chat.checkpoint.execution_config import (
    build_checkpoint_execution_config,
)
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMProviderMigrationService,
    LLMProviderSelectionService,
    LLMRuntimeConfigService,
)


def _create_user(db, username_prefix: str = "deployment-runtime-selection") -> User:
    user = User(
        username=f"{username_prefix}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_runtime_selection_snapshots_are_plain_values_across_selection_changes() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-runtime-selection",
        )
        selection_service = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        )
        selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        db.commit()
        runtime_config = LLMRuntimeConfigService(
            db,
            credential_service=credential_service,
            selection_service=selection_service,
        )

        first = runtime_config.build_runtime_selection(user_id=user.id).to_dict()
        selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5-mini",
        )
        db.commit()
        second = runtime_config.build_runtime_selection(user_id=user.id).to_dict()

        assert first == {
            "provider": OPENAI_PROVIDER_ID,
            "model": "gpt-5.2",
            "credential_ref": {
                "user_id": user.id,
                "provider": OPENAI_PROVIDER_ID,
            },
            "reasoning_effort": None,
        }
        assert second["model"] == "gpt-5-mini"
        assert first["model"] == "gpt-5.2"
        assert "sk-runtime-selection" not in repr(first)
    finally:
        db.close()


def test_continuation_selection_prefers_checkpoint_hint_over_current_selection() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-continuation-hint")
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-continuation",
        )
        selection_service = LLMProviderSelectionService(
            db,
            credential_service=credential_service,
        )
        mapped_selection = selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5.2",
        )
        LLMProviderMigrationService(db).backfill_deployment_identity_for_user(user.id)
        db.commit()
        db.refresh(mapped_selection)
        assert mapped_selection.deployment_id is not None
        deployment_id = str(mapped_selection.deployment_id)

        selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5-mini",
        )
        db.commit()
        runtime_config = LLMRuntimeConfigService(
            db,
            credential_service=credential_service,
            selection_service=selection_service,
        )

        selection = runtime_config.build_continuation_selection(
            user_id=user.id,
            checkpoint_hint={
                "provider": OPENAI_PROVIDER_ID,
                "model": "gpt-5.2",
                "reasoning_effort": "low",
            },
        )

        assert selection.deployment_ref.deployment_id == deployment_id
        assert selection.legacy_provider == OPENAI_PROVIDER_ID
        assert selection.legacy_model == "gpt-5.2"
        assert selection.reasoning_effort == "low"
        assert "credential_ref" not in repr(selection.to_dict())
    finally:
        db.close()


def test_checkpoint_runtime_hint_and_execution_config_remain_non_secret() -> None:
    values = {
        "facts": {
            "metadata": {
                "llm_runtime_selection": {
                    "provider": OPENAI_PROVIDER_ID,
                    "model": "gpt-5.2",
                    "reasoning_effort": "medium",
                    "api_key": "sk-should-not-copy",
                }
            }
        }
    }

    hint = CheckpointContinuationService._extract_checkpoint_runtime_hint(values)
    selection = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": "11111111-1111-4111-8111-111111111111",
            "expected_revision": 1,
        },
        "reasoning_effort": "medium",
        "legacy_provider": OPENAI_PROVIDER_ID,
        "legacy_model": "gpt-5.2",
    }
    config = build_checkpoint_execution_config(
        task_id=7,
        graph_name="simple_tool",
        graph_thread_id="a" * 32,
        user_id=11,
        llm_runtime_selection=selection,
    )

    assert hint == {
        "provider": OPENAI_PROVIDER_ID,
        "model": "gpt-5.2",
        "reasoning_effort": "medium",
    }
    assert config["configurable"]["llm_runtime_selection"] == selection
    assert config["configurable"]["runtime_projection"] == {
        "task_id": 7,
        "graph_thread_id": "a" * 32,
        "reasoning_effort": "medium",
        "user_id": 11,
    }
    assert "credential_ref" not in repr(config)
    assert "sk-should-not-copy" not in repr(hint)
    assert "sk-should-not-copy" not in repr(config)
