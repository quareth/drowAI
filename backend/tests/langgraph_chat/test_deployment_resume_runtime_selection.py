"""Resume-time runtime selection tests for deployment-aware checkpoints."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import User
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMProviderMigrationService,
    LLMProviderSelectionService,
)
from backend.services.llm_provider.types import ProviderConfigurationError


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


def _build_service() -> CheckpointContinuationService:
    return CheckpointContinuationService(
        checkpointer_service=None,
        executor=None,
        streaming_adapter=None,
        build_checkpoint_execution_config=lambda **_kwargs: {},
        hydrate_container_from_checkpoint_state=lambda *_args, **_kwargs: None,
        extract_resume_conversation_id=lambda _state: "",
        resolve_resume_turn_number=lambda **_kwargs: 0,
        persist_chat_message_from_container=lambda **_kwargs: None,
        build_result=lambda **_kwargs: None,
    )


def test_resume_rebuilds_runtime_selection_from_authenticated_user_context() -> None:
    """Checkpoint-supplied credential/user facts do not control resume identity."""

    db = SessionLocal()
    try:
        owner = _create_user(db, "deployment-resume-owner")
        deployment_id = _create_legacy_deployment(
            db,
            user_id=owner.id,
            model="gpt-5.2",
        )
        owner_id = owner.id
        checkpoint_user_id = 987654321
    finally:
        db.close()

    selection: Any = None
    cleanup = None
    try:
        service = _build_service()
        selection, runtime_services, cleanup = service._prepare_runtime_dependencies(
            user_id=owner_id,
            llm_runtime_selection=None,
            runtime_services=None,
            checkpoint_hint={
                "provider": OPENAI_PROVIDER_ID,
                "model": "gpt-5.2",
                "credential_ref": {
                    "user_id": checkpoint_user_id,
                    "provider": "openai",
                },
                "endpoint": "https://checkpoint.example.invalid/v1",
                "api_key": "sk-should-not-survive",
            },
        )

        assert runtime_services is not None
        assert selection["deployment_ref"]["deployment_id"] == deployment_id
        assert selection["legacy_provider"] == OPENAI_PROVIDER_ID
        assert selection["legacy_model"] == "gpt-5.2"
        serialized = repr(selection).lower()
        assert str(checkpoint_user_id) not in serialized
        for forbidden in (
            "credential_ref",
            "endpoint",
            "api_key",
            "sk-should-not-survive",
        ):
            assert forbidden not in serialized
    finally:
        if cleanup is not None:
            cleanup()


def test_resume_unmapped_legacy_checkpoint_hint_does_not_fallback_to_current() -> None:
    """Continuation reports an unrunnable checkpoint instead of swapping models."""

    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-resume-unmapped")
        _create_legacy_deployment(db, user_id=user.id, model="gpt-5-mini")
        user_id = user.id
    finally:
        db.close()

    service = _build_service()
    with pytest.raises(ProviderConfigurationError, match="unmapped.*reselect"):
        service._prepare_runtime_dependencies(
            user_id=user_id,
            llm_runtime_selection=None,
            runtime_services=None,
            checkpoint_hint={
                "provider": OPENAI_PROVIDER_ID,
                "model": "legacy-unmapped-wire-model",
            },
        )
