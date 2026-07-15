"""Build non-secret LLM runtime selections and live runtime service bags.

This service is the backend boundary used by chat, queue, continuation, and
other runtime paths to obtain provider/model/credential-ref metadata without
carrying decrypted provider secrets.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.config import E2E_DETERMINISTIC_MODE
from backend.config.feature_flags import is_semantic_memory_runtime_enabled
from core.llm.role_policy import ModelRoleRegistry, RoleKey

from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .migration_service import LLMProviderMigrationService
from .runtime_client_resolver import LLMRuntimeClientResolver
from .runtime_services import LLMRuntimeServices
from .selection_service import LLMProviderSelectionService
from .types import LLMCallTarget, LLMCredentialRef, LLMRuntimeSelection


class LLMRuntimeConfigService:
    """Build runtime-safe provider/model/credential-ref payloads."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
        selection_service: LLMProviderSelectionService | None = None,
        migration_service: LLMProviderMigrationService | None = None,
        role_registry: ModelRoleRegistry | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._migration = migration_service or LLMProviderMigrationService(db)
        self._credential_service = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
            migration_service=self._migration,
        )
        self._selection_service = selection_service or LLMProviderSelectionService(
            db,
            catalog_service=self._catalog,
            credential_service=self._credential_service,
            migration_service=self._migration,
        )
        self._role_registry = role_registry or ModelRoleRegistry()

    def build_runtime_selection(
        self,
        *,
        user_id: int,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        require_enabled_credential: bool = True,
    ) -> LLMRuntimeSelection:
        """Return provider/model/credential-ref runtime metadata."""

        if provider is not None and model is not None:
            resolved_provider = provider
            resolved_model = model
            profile = self._catalog.require_selectable_model(resolved_provider, resolved_model)
            provider_id = profile.ref.provider
            model_id = profile.ref.model
        elif provider is not None or model is not None:
            current = self._selection_service.get_selection(user_id)
            resolved_provider = provider or current.provider
            resolved_model = model or current.model
            profile = self._catalog.require_selectable_model(resolved_provider, resolved_model)
            provider_id = profile.ref.provider
            model_id = profile.ref.model
        else:
            current = self._selection_service.get_selection(user_id)
            provider_id = current.provider
            model_id = current.model

        credential_ref = (
            self._credential_service.get_credential_ref(user_id, provider_id)
            if require_enabled_credential
            else LLMCredentialRef(user_id=user_id, provider=provider_id)
        )

        return LLMRuntimeSelection(
            provider=provider_id,
            model=model_id,
            credential_ref=credential_ref,
            reasoning_effort=reasoning_effort,
        )

    def build_runtime_services(self) -> LLMRuntimeServices:
        """Return live runtime dependencies for one invocation."""

        client_resolver = LLMRuntimeClientResolver(self._credential_service)
        memory_runtime_service = None
        if is_semantic_memory_runtime_enabled():
            from backend.services.memory.runtime_service import MemoryRuntimeService

            memory_runtime_service = MemoryRuntimeService(
                client_resolver=client_resolver,
            )

        return LLMRuntimeServices(
            client_resolver=client_resolver,
            memory_runtime_service=memory_runtime_service,
        )

    def resolve_role_target(
        self,
        selection: LLMRuntimeSelection,
        role: RoleKey,
        *,
        reasoning_model: str | None = None,
        reasoning_provider: str | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMCallTarget:
        """Resolve provider/model target for a role-owned LLM call."""

        settings = self._role_registry.resolve_call_settings(
            role,
            conversation_model=selection.model,
            conversation_provider=selection.provider,
            reasoning_model=reasoning_model,
            reasoning_provider=reasoning_provider,
            reasoning_effort=reasoning_effort or selection.reasoning_effort,
        )
        return LLMCallTarget(
            provider=settings.provider,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
            role=role,
        )

    def build_continuation_selection(
        self,
        *,
        user_id: int,
        checkpoint_hint: dict | None = None,
    ) -> LLMRuntimeSelection:
        """Rebuild runtime selection for resume/retry from authorized user context."""

        hint = checkpoint_hint or {}
        provider = hint.get("provider") if isinstance(hint.get("provider"), str) else None
        model = hint.get("model") if isinstance(hint.get("model"), str) else None
        reasoning_effort = (
            hint.get("reasoning_effort") if isinstance(hint.get("reasoning_effort"), str) else None
        )
        return self.build_runtime_selection(
            user_id=user_id,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            require_enabled_credential=not E2E_DETERMINISTIC_MODE,
        )


__all__ = ["LLMRuntimeConfigService"]
