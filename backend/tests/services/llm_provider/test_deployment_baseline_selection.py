"""Deployment baseline tests for durable LLM selection services."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID
from agent.providers.llm.profiles import OPENAI_DEFAULT_MODEL_ID
from backend.database import SessionLocal
from backend.models import (
    User,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserSettings,
)
from backend.services.embeddings.selection_service import (
    DEFAULT_MEMORY_EXTRACTION_MODEL,
    DEFAULT_MEMORY_GATE_MODEL,
    EmbeddingRuntimeSelectionService,
)
from backend.services.llm_provider import (
    CredentialNotFoundError,
    LLMCredentialRef,
    LLMCredentialService,
    LLMProviderSelectionService,
    LLMRuntimeConfigService,
    ProviderConfigurationError,
    ReportingLLMSelectionService,
)


def _create_user(db, username_prefix: str = "deployment-selection") -> User:
    user = User(
        username=f"{username_prefix}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_legacy_conversation_selection_writes_openai_mirror_without_credential() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
        service = LLMProviderSelectionService(db)

        selection = service.set_selection(
            user_id=user.id,
            provider="OpenAI",
            model="GPT-5-MINI",
            require_enabled_credential=False,
        )
        db.commit()

        read = service.get_selection_read(user.id)
        settings = db.query(UserSettings).filter_by(user_id=user.id).one()

        assert selection.provider == OPENAI_PROVIDER_ID
        assert selection.model == "gpt-5-mini"
        assert read.selection.provider == OPENAI_PROVIDER_ID
        assert read.selection.model == "gpt-5-mini"
        assert read.status.status == "credential_missing"
        assert read.status.selectable is True
        assert read.status.runnable is False
        assert settings.openai_model == "gpt-5-mini"
        assert service.get_openai_model_compat(user.id) == "gpt-5-mini"
    finally:
        db.close()


def test_selection_change_affects_later_runtime_reads_not_existing_snapshot() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-selection-snapshot")
        credential_service = LLMCredentialService(db)
        credential_service.upsert_api_key(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            api_key="sk-runtime",
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
        runtime_service = LLMRuntimeConfigService(
            db,
            credential_service=credential_service,
            selection_service=selection_service,
        )

        first_snapshot = runtime_service.build_runtime_selection(user_id=user.id)
        selection_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model="gpt-5-mini",
        )
        db.commit()
        later_snapshot = runtime_service.build_runtime_selection(user_id=user.id)

        assert first_snapshot.provider == OPENAI_PROVIDER_ID
        assert first_snapshot.model == "gpt-5.2"
        assert first_snapshot.credential_ref == LLMCredentialRef(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
        )
        assert later_snapshot.model == "gpt-5-mini"
        assert first_snapshot.model == "gpt-5.2"
    finally:
        db.close()


def test_missing_credentials_are_selectable_but_unrunnable_for_chat_and_reporting() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-selection-missing-credential")
        db.add(
            UserLLMSelection(
                user_id=user.id,
                provider=OPENAI_PROVIDER_ID,
                model=OPENAI_DEFAULT_MODEL_ID,
            )
        )
        db.commit()

        chat_read = LLMProviderSelectionService(db).get_selection_read(user.id)
        reporting_service = ReportingLLMSelectionService(db)
        reporting_service.set_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            model=OPENAI_DEFAULT_MODEL_ID,
        )
        db.commit()
        reporting_read = reporting_service.get_selection_read(user.id)

        assert chat_read.status.status == "credential_missing"
        assert chat_read.status.selectable is True
        assert chat_read.status.runnable is False
        with pytest.raises(CredentialNotFoundError):
            LLMRuntimeConfigService(db).build_runtime_selection(user_id=user.id)

        assert reporting_read.status.status == "credential_missing"
        assert reporting_read.status.selectable is True
        assert reporting_read.status.runnable is False
        with pytest.raises(ProviderConfigurationError):
            reporting_service.build_runtime_selection(user_id=user.id)
    finally:
        db.close()


def test_reporting_and_memory_llm_selections_remain_provider_model_rows() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-selection-reporting-memory")
        credential_refs: list[tuple[int, str]] = []

        reporting = ReportingLLMSelectionService(db).set_selection(
            user_id=user.id,
            provider=ANTHROPIC_PROVIDER_ID,
            model="claude-sonnet-5",
            reasoning_effort="high",
        )
        memory_service = EmbeddingRuntimeSelectionService(
            credential_ref_resolver=lambda user_id, provider: (
                credential_refs.append((user_id, provider))
                or LLMCredentialRef(user_id=user_id, provider=provider)
            ),
            db=db,
        )
        memory = memory_service.set_memory_llm_selection(
            user_id=user.id,
            provider=OPENAI_PROVIDER_ID,
            gate_model=DEFAULT_MEMORY_GATE_MODEL,
            extraction_model=DEFAULT_MEMORY_EXTRACTION_MODEL,
        )
        db.commit()

        resolved_memory = memory_service.resolve_memory_llm_selection(
            user_id=user.id,
        )
        stored_memory = db.query(UserMemoryLLMSelection).filter_by(
            user_id=user.id,
        ).one()

        assert reporting.provider == ANTHROPIC_PROVIDER_ID
        assert reporting.model == "claude-sonnet-5"
        assert reporting.reasoning_effort == "high"
        assert memory.provider == OPENAI_PROVIDER_ID
        assert stored_memory.gate_model == DEFAULT_MEMORY_GATE_MODEL
        assert resolved_memory.provider == OPENAI_PROVIDER_ID
        assert resolved_memory.gate_model == DEFAULT_MEMORY_GATE_MODEL
        assert resolved_memory.extraction_model == DEFAULT_MEMORY_EXTRACTION_MODEL
        assert credential_refs == [(user.id, OPENAI_PROVIDER_ID)]
    finally:
        db.close()
