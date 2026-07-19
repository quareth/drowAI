"""Provider-neutral user LLM selection service.

This service owns durable conversation deployment refs and provider/model
compatibility snapshots, and keeps legacy OpenAI model mirrors synchronized.
Runtime reads revalidate deployment ownership, compatibility, and runnability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import (
    OPENAI_PROVIDER_ID,
    normalize_model_id,
    normalize_provider_id,
)
from agent.providers.llm.profiles.registry import OPENAI_DEFAULT_MODEL_ID

from backend.config import E2E_DETERMINISTIC_MODE
from backend.models import UserLLMSelection, UserSettings
from backend.services.metrics.utils import safe_inc_labeled

from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .migration_service import LLMProviderMigrationService
from .operation_registry import GPT_OSS_20B_PROVING_PRESET_ID
from .selection_deployment_resolver import (
    LLMSelectionDeploymentResolver,
    SelectionDeploymentTarget,
)
from .types import (
    CredentialNotFoundError,
    DeploymentRef,
    LLMRuntimeSelectionV2,
    LLMSelectionStatus,
    ProviderConfigurationError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LLMSelectionRead:
    """Saved conversation selection plus non-mutating status metadata."""

    selection: UserLLMSelection
    status: LLMSelectionStatus


class LLMProviderSelectionService:
    """Read, write, and reconcile user conversation LLM selection."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
        migration_service: LLMProviderMigrationService | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._migration = migration_service or LLMProviderMigrationService(db)
        self._credential_service = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
            migration_service=self._migration,
        )
        self._deployment_resolver = LLMSelectionDeploymentResolver(db)

    def get_selection(self, user_id: int) -> UserLLMSelection:
        """Return the canonical provider-neutral selection for a user."""

        self._migration.backfill_legacy_openai_for_user(user_id)
        selection = self._get_selection_row(user_id)
        if selection is None:
            selection = UserLLMSelection(
                user_id=user_id,
                provider=OPENAI_PROVIDER_ID,
                model=OPENAI_DEFAULT_MODEL_ID,
            )
            self._db.add(selection)
            self._sync_legacy_openai_model(user_id, OPENAI_DEFAULT_MODEL_ID)
            self._db.flush()
            return selection

        selection = self._reconcile_selection(selection)
        self._sync_legacy_openai_model_mirror_from_selection(selection)
        return selection

    def get_selection_read(self, user_id: int) -> LLMSelectionRead:
        """Return a saved selection with descriptive status for product reads."""

        self._migration.backfill_legacy_openai_for_user(user_id)
        selection = self._get_selection_row(user_id)
        if selection is None:
            selection = UserLLMSelection(
                user_id=user_id,
                provider=OPENAI_PROVIDER_ID,
                model=OPENAI_DEFAULT_MODEL_ID,
            )
            self._db.add(selection)
            self._sync_legacy_openai_model(user_id, OPENAI_DEFAULT_MODEL_ID)
            self._db.flush()
        else:
            selection = self._reconcile_selection(selection)
        status = self._classify_selection(selection)
        self._emit_selection_status(status.status)
        self._emit_legacy_identity_status(
            "mapped" if selection.deployment_id is not None else "unmapped"
        )
        if status.status in {"selectable", "credential_missing"}:
            self._sync_legacy_openai_model_mirror_from_selection(selection)
        return LLMSelectionRead(selection=selection, status=status)

    def set_selection(
        self,
        *,
        user_id: int,
        provider: str,
        model: str,
        require_enabled_credential: bool = True,
    ) -> UserLLMSelection:
        """Persist a provider-neutral provider/model selection."""

        normalized_provider = normalize_provider_id(provider)
        profile = self._catalog.require_selectable_model(normalized_provider, model)
        normalized_model = profile.ref.model

        if require_enabled_credential and not self._credential_service.has_enabled_credential(
            user_id,
            normalized_provider,
        ):
            raise CredentialNotFoundError(f"{normalized_provider} credential is required")

        selection = self._get_selection_row(user_id)
        if selection is None:
            selection = UserLLMSelection(
                user_id=user_id,
                provider=normalized_provider,
                model=normalized_model,
            )
            self._db.add(selection)
        else:
            selection.provider = normalized_provider
            selection.model = normalized_model
        selection.deployment_id = None

        if normalized_provider == OPENAI_PROVIDER_ID:
            self._sync_legacy_openai_model(user_id, normalized_model)

        self._db.flush()
        return selection

    def set_deployment_selection(
        self,
        *,
        user_id: int,
        deployment_id: str,
        expected_deployment_revision: int,
    ) -> UserLLMSelection:
        """Persist an owner-scoped conversation deployment and legacy snapshot."""

        target = self._deployment_resolver.resolve(
            user_id=user_id,
            deployment_id=deployment_id,
            expected_revision=expected_deployment_revision,
            role="conversation",
        )
        status = self._classify_deployment_target(user_id=user_id, target=target)
        if not status.runnable:
            raise ProviderConfigurationError(
                status.reason or "Conversation deployment is not runnable"
            )
        selection = self._get_selection_row(user_id)
        if selection is None:
            selection = UserLLMSelection(user_id=user_id)
            self._db.add(selection)
        selection.provider = target.provider
        selection.model = target.model
        selection.deployment_id = target.deployment.id
        if target.provider == OPENAI_PROVIDER_ID:
            self._sync_legacy_openai_model(user_id, target.model)
        self._db.flush()
        return selection

    def build_deployment_runtime_selection(
        self,
        *,
        user_id: int,
        reasoning_effort: str | None = None,
    ) -> LLMRuntimeSelectionV2:
        """Build V2 identity while revalidating a saved deployment binding."""

        selection = self.get_selection(user_id)
        if selection.deployment_id is None:
            self._emit_legacy_identity_status("unmapped")
            raise ProviderConfigurationError(
                "Conversation selection has no deployment binding"
            )
        target = self._deployment_resolver.resolve(
            user_id=user_id,
            deployment_id=selection.deployment_id,
            role="conversation",
        )
        status = self._classify_deployment_selection(selection, target=target)
        if not status.runnable:
            raise ProviderConfigurationError(
                status.reason or "Conversation deployment is not runnable"
            )
        return LLMRuntimeSelectionV2(
            deployment_ref=DeploymentRef(
                deployment_id=str(target.deployment.id),
                expected_revision=int(target.deployment.revision),
            ),
            reasoning_effort=reasoning_effort,
            legacy_provider=selection.provider,
            legacy_model=selection.model,
        )

    def get_openai_model_compat(self, user_id: int) -> str:
        """Compatibility helper for old OpenAI model callers."""

        try:
            selection = self.get_selection(user_id)
            if selection.provider != OPENAI_PROVIDER_ID:
                self._emit_legacy_compat_status("default_provider")
                return OPENAI_DEFAULT_MODEL_ID
            self._emit_legacy_compat_status("selected")
            return selection.model
        except Exception as exc:
            logger.error("Failed to resolve OpenAI model for user %s: %s", user_id, exc)
            self._emit_legacy_compat_status("failure")
            return OPENAI_DEFAULT_MODEL_ID

    def _get_selection_row(self, user_id: int) -> UserLLMSelection | None:
        return self._db.execute(
            select(UserLLMSelection).where(UserLLMSelection.user_id == user_id)
        ).scalar_one_or_none()

    def _reconcile_selection(self, selection: UserLLMSelection) -> UserLLMSelection:
        if selection.deployment_id is not None:
            target = self._deployment_resolver.resolve(
                user_id=selection.user_id,
                deployment_id=selection.deployment_id,
                role="conversation",
            )
            if selection.provider != target.provider or selection.model != target.model:
                selection.provider = target.provider
                selection.model = target.model
                self._db.flush()
            return selection
        provider = normalize_provider_id(selection.provider or OPENAI_PROVIDER_ID)
        model = (selection.model or "").strip()
        profile = self._catalog.require_selectable_model(provider, model)
        normalized_model = profile.ref.model

        if selection.provider != provider or selection.model != normalized_model:
            selection.provider = provider
            selection.model = normalized_model
            if provider == OPENAI_PROVIDER_ID:
                self._sync_legacy_openai_model(selection.user_id, normalized_model)
            self._db.flush()
        return selection

    def _classify_selection(self, selection: UserLLMSelection) -> LLMSelectionStatus:
        if selection.deployment_id is not None:
            try:
                target = self._deployment_resolver.resolve(
                    user_id=selection.user_id,
                    deployment_id=selection.deployment_id,
                    role="conversation",
                )
            except Exception as exc:
                return LLMSelectionStatus(
                    status="model_unavailable",
                    selectable=False,
                    runnable=False,
                    reason=str(exc),
                )
            return self._classify_deployment_selection(selection, target=target)
        provider_raw = selection.provider or OPENAI_PROVIDER_ID
        model_raw = selection.model or ""
        try:
            provider = normalize_provider_id(provider_raw)
            model = normalize_model_id(model_raw)
        except (TypeError, ValueError) as exc:
            return LLMSelectionStatus(
                status="invalid_selection",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )

        try:
            self._catalog.require_provider(provider)
        except Exception as exc:
            return LLMSelectionStatus(
                status="invalid_selection",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )

        if not self._catalog.is_provider_adapter_available(provider):
            return LLMSelectionStatus(
                status="adapter_unavailable",
                selectable=False,
                runnable=False,
                reason=f"LLM provider adapter is not registered: {provider}",
            )

        try:
            profile = self._catalog.require_model(provider, model)
        except Exception as exc:
            return LLMSelectionStatus(
                status="model_unavailable",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )

        if not profile.supports(LLMCapability.CHAT):
            return LLMSelectionStatus(
                status="model_unavailable",
                selectable=False,
                runnable=False,
                reason=f"Model '{profile.ref}' is not selectable because it does not support chat",
            )

        try:
            self._catalog.require_selectable_model(provider, model)
        except Exception as exc:
            return LLMSelectionStatus(
                status="model_unavailable",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )

        if E2E_DETERMINISTIC_MODE:
            return LLMSelectionStatus(
                status="deterministic_e2e",
                selectable=True,
                runnable=True,
            )

        if not self._credential_service.has_enabled_credential(user_id=selection.user_id, provider=provider):
            return LLMSelectionStatus(
                status="credential_missing",
                selectable=True,
                runnable=False,
                reason=f"{provider} credential is required to run {provider} model",
            )

        return LLMSelectionStatus(
            status="selectable",
            selectable=True,
            runnable=True,
        )

    def _classify_deployment_selection(
        self,
        selection: UserLLMSelection,
        *,
        target: SelectionDeploymentTarget,
    ) -> LLMSelectionStatus:
        return self._classify_deployment_target(
            user_id=int(selection.user_id),
            target=target,
        )

    def _classify_deployment_target(
        self,
        *,
        user_id: int,
        target: SelectionDeploymentTarget,
    ) -> LLMSelectionStatus:
        unavailable = self._deployment_resolver.classify_runnability(
            user_id=user_id,
            target=target,
            credential_available=self._credential_service.has_enabled_credential,
            credential_fingerprint=(
                self._credential_service.connection_credential_fingerprint
            ),
            missing_credential_reason="Deployment credential is required",
            required_capabilities=(
                (
                    LLMCapability.CHAT,
                    LLMCapability.USAGE_REPORTING,
                )
                if target.connection.connection_preset_id == GPT_OSS_20B_PROVING_PRESET_ID
                else (LLMCapability.CHAT,)
            ),
            capability_missing_reason=(
                "Successful proving verification is required"
                if target.connection.connection_preset_id == GPT_OSS_20B_PROVING_PRESET_ID
                else "Capability evidence is required"
            ),
        )
        if unavailable is not None:
            return unavailable
        return LLMSelectionStatus(status="selectable", selectable=True, runnable=True)

    @staticmethod
    def _emit_selection_status(status: str) -> None:
        safe_inc_labeled(
            "llm_provider.selection_status.total",
            {"status": status},
        )

    @staticmethod
    def _emit_legacy_identity_status(status: str) -> None:
        safe_inc_labeled(
            "llm_provider.legacy_identity_read.total",
            {"status": status},
        )

    @staticmethod
    def _emit_legacy_compat_status(status: str) -> None:
        safe_inc_labeled(
            "llm_provider.legacy_compat_read.total",
            {"status": status},
        )

    def _sync_legacy_openai_model_mirror_from_selection(self, selection: UserLLMSelection) -> None:
        if normalize_provider_id(selection.provider or OPENAI_PROVIDER_ID) != OPENAI_PROVIDER_ID:
            return
        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == selection.user_id)
        ).scalar_one_or_none()
        if settings is None:
            settings = UserSettings(user_id=selection.user_id)
            self._db.add(settings)
        if settings.openai_model != selection.model:
            settings.openai_model = selection.model
            self._db.flush()

    def _sync_legacy_openai_model(self, user_id: int, model: str) -> None:
        settings = self._db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        ).scalar_one_or_none()
        if settings is None:
            settings = UserSettings(user_id=user_id)
            self._db.add(settings)
        settings.openai_model = normalize_model_id(model)


__all__ = ["LLMProviderSelectionService", "LLMSelectionRead"]
