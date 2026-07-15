"""Legacy OpenAI settings reconciliation for provider-neutral LLM rows.

This service is the only backend LLM provider service that treats legacy
`UserSettings.openai_*` fields as migration inputs. It copies encrypted
OpenAI key ciphertext directly and never calls plaintext credential writers
with legacy ciphertext.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import (
    OPENAI_API_SURFACE_RESPONSES,
    OPENAI_DEFAULT_MODEL_ID,
    require_model_profile,
)

from backend.models import UserLLMProviderCredential, UserLLMSelection, UserSettings


class LLMProviderMigrationService:
    """Idempotent legacy OpenAI to provider-neutral row reconciliation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def backfill_legacy_openai_for_user(self, user_id: int) -> None:
        """Create missing provider-neutral OpenAI rows from legacy settings."""

        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        ).scalar_one_or_none()
        if settings is None:
            return

        self._backfill_credential(settings)
        self._backfill_selection(settings)
        self._db.flush()

    def backfill_all_legacy_openai(self) -> int:
        """Backfill all legacy settings rows and return the number inspected."""

        settings_rows = self._db.execute(select(UserSettings)).scalars().all()
        for settings in settings_rows:
            self._backfill_credential(settings)
            self._backfill_selection(settings)
        self._db.flush()
        return len(settings_rows)

    def _backfill_credential(self, settings: UserSettings) -> None:
        encrypted_key = (getattr(settings, "openai_api_key", None) or "").strip()
        if not encrypted_key:
            return

        existing = self._db.execute(
            select(UserLLMProviderCredential).where(
                UserLLMProviderCredential.user_id == settings.user_id,
                UserLLMProviderCredential.provider == OPENAI_PROVIDER_ID,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        self._db.add(
            UserLLMProviderCredential(
                user_id=settings.user_id,
                provider=OPENAI_PROVIDER_ID,
                encrypted_api_key=encrypted_key,
                enabled=True,
            )
        )

    def _backfill_selection(self, settings: UserSettings) -> None:
        model = (getattr(settings, "openai_model", None) or "").strip()
        if not model:
            return

        existing = self._db.execute(
            select(UserLLMSelection).where(UserLLMSelection.user_id == settings.user_id)
        ).scalar_one_or_none()
        if existing is not None:
            return

        self._db.add(
            UserLLMSelection(
                user_id=settings.user_id,
                provider=OPENAI_PROVIDER_ID,
                model=_normalize_legacy_openai_selection_model(model),
            )
        )


def _normalize_legacy_openai_selection_model(model: str) -> str:
    """Normalize legacy OpenAI model settings during explicit backfill."""

    normalized = model.strip().lower()
    try:
        profile = require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, normalized))
    except LLMProfileNotFoundError:
        return OPENAI_DEFAULT_MODEL_ID
    if profile.api_surface != OPENAI_API_SURFACE_RESPONSES:
        return OPENAI_DEFAULT_MODEL_ID
    return profile.ref.model


__all__ = ["LLMProviderMigrationService"]
