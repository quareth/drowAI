"""Tests for deterministic-E2E-only LLM selection readiness."""

from __future__ import annotations

import uuid

from backend.database import SessionLocal
from backend.models import User
from backend.services.llm_provider import runtime_config_service as runtime_config_module
from backend.services.llm_provider import selection_service as selection_module
from backend.services.llm_provider.runtime_config_service import LLMRuntimeConfigService
from backend.services.llm_provider.selection_service import LLMProviderSelectionService


def test_deterministic_e2e_selection_is_ui_runnable_without_credential(monkeypatch) -> None:
    monkeypatch.setattr(selection_module, "E2E_DETERMINISTIC_MODE", True)
    db = SessionLocal()
    try:
        user = User(
            username=f"e2e-selection-{uuid.uuid4().hex}",
            password="unused-test-password-hash",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        read = LLMProviderSelectionService(db).get_selection_read(user.id)

        assert read.status.status == "deterministic_e2e"
        assert read.status.selectable is True
        assert read.status.runnable is True
    finally:
        db.close()


def test_deterministic_e2e_continuation_reuses_selection_without_credential(monkeypatch) -> None:
    """Offline resume/retry paths must reach their deterministic graph without credentials."""
    monkeypatch.setattr(runtime_config_module, "E2E_DETERMINISTIC_MODE", True)
    db = SessionLocal()
    try:
        user = User(
            username=f"e2e-continuation-{uuid.uuid4().hex}",
            password="unused-test-password-hash",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        selection = LLMRuntimeConfigService(db).build_continuation_selection(
            user_id=user.id,
        )

        assert selection.provider == "openai"
        assert selection.credential_ref.user_id == user.id
    finally:
        db.close()
