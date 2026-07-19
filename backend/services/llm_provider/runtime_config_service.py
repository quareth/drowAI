"""Build non-secret LLM runtime selections and live runtime service bags.

This service is the backend boundary used by chat, queue, continuation, and
other runtime paths to obtain provider/model/credential-ref metadata without
carrying decrypted provider secrets.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import E2E_DETERMINISTIC_MODE
from backend.config.feature_flags import is_semantic_memory_runtime_enabled
from backend.models import LLMInferenceConnection, LLMModelDeployment
from core.llm.role_policy import ModelRoleRegistry, RoleKey

from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .deployment_service import LLMDeploymentService
from .migration_service import LLMProviderMigrationService
from .runtime_client_resolver import LLMRuntimeClientResolver
from .runtime_services import LLMRuntimeServices
from .selection_service import LLMProviderSelectionService
from .types import (
    DeploymentRef,
    LLMCallTarget,
    LLMCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    ProviderConfigurationError,
)


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

    def build_conversation_runtime_selection(
        self,
        *,
        user_id: int,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        require_enabled_credential: bool = True,
    ) -> LLMRuntimeSelection | LLMRuntimeSelectionV2:
        """Return the current conversation runtime identity, preferring deployments."""

        if provider is None and model is None:
            if E2E_DETERMINISTIC_MODE:
                return self.build_runtime_selection(
                    user_id=user_id,
                    reasoning_effort=reasoning_effort,
                    require_enabled_credential=False,
                )
            return self._selection_service.build_deployment_runtime_selection(
                user_id=user_id,
                reasoning_effort=reasoning_effort,
            )
        if E2E_DETERMINISTIC_MODE:
            return self.build_runtime_selection(
                user_id=user_id,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
                require_enabled_credential=False,
            )
        return self._build_provider_model_conversation_runtime_selection(
            user_id=user_id,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            require_enabled_credential=require_enabled_credential,
        )

    def _build_provider_model_conversation_runtime_selection(
        self,
        *,
        user_id: int,
        provider: str | None,
        model: str | None,
        reasoning_effort: str | None,
        require_enabled_credential: bool,
    ) -> LLMRuntimeSelectionV2:
        """Resolve explicit provider/model conversation inputs to V2 identity."""

        if provider is not None and model is not None:
            resolved_provider = provider
            resolved_model = model
        else:
            current = self._selection_service.get_selection(user_id)
            resolved_provider = provider or current.provider
            resolved_model = model or current.model
        profile = self._catalog.require_selectable_model(
            resolved_provider,
            resolved_model,
        )
        provider_id = profile.ref.provider
        model_id = profile.ref.model
        if require_enabled_credential:
            self._credential_service.get_credential_ref(user_id, provider_id)
        deployment = self._migration.ensure_legacy_default_deployment_for_model(
            user_id=user_id,
            provider=provider_id,
            wire_model_id=model_id,
        )
        if deployment is None:
            raise ProviderConfigurationError(
                "Conversation runtime selection requires a deployment binding"
            )
        return self.build_deployment_runtime_selection(
            user_id=user_id,
            deployment_id=str(deployment.id),
            reasoning_effort=reasoning_effort,
            legacy_provider=provider_id,
            legacy_model=model_id,
        )

    def build_runtime_services(self) -> LLMRuntimeServices:
        """Return live runtime dependencies for one invocation."""

        client_resolver = LLMRuntimeClientResolver(
            self._credential_service,
            db=self._db,
        )
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

    def build_deployment_runtime_selection(
        self,
        *,
        user_id: int,
        deployment_id: str,
        preferred_route_id: str | None = None,
        reasoning_effort: str | None = None,
        legacy_provider: str | None = None,
        legacy_model: str | None = None,
    ) -> LLMRuntimeSelectionV2:
        """Build checkpoint-safe V2 identity from current owner-scoped rows."""

        deployments = LLMDeploymentService(self._db)
        deployment = deployments.get_deployment(
            user_id=user_id,
            deployment_id=deployment_id,
        )
        if preferred_route_id is not None:
            route = deployments.get_route(
                user_id=user_id,
                route_id=preferred_route_id,
            )
            if route.deployment_id != deployment.id or not route.enabled:
                raise LLMDeploymentNotFoundError(
                    "Preferred deployment route is unavailable"
                )
        return LLMRuntimeSelectionV2(
            deployment_ref=DeploymentRef(
                deployment_id=str(deployment.id),
                expected_revision=int(deployment.revision),
            ),
            preferred_route_id=preferred_route_id,
            reasoning_effort=reasoning_effort,
            legacy_provider=legacy_provider,
            legacy_model=legacy_model,
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
    ) -> LLMRuntimeSelection | LLMRuntimeSelectionV2:
        """Rebuild runtime selection for resume/retry from authorized user context."""

        if checkpoint_hint is None:
            return self.build_conversation_runtime_selection(
                user_id=user_id,
                require_enabled_credential=not E2E_DETERMINISTIC_MODE,
            )

        hint = checkpoint_hint
        if self._is_deployment_runtime_hint(hint):
            return self._build_deployment_continuation_selection(
                user_id=user_id,
                checkpoint_hint=hint,
            )

        provider = self._hint_string(hint, "provider")
        model = self._hint_string(hint, "model")
        reasoning_effort = (
            hint.get("reasoning_effort") if isinstance(hint.get("reasoning_effort"), str) else None
        )
        if provider is None or model is None:
            raise ProviderConfigurationError(
                "Legacy checkpoint runtime selection is incomplete and cannot run; "
                "reselect an available LLM deployment, then retry resume."
            )

        deployment = self._find_legacy_checkpoint_deployment(
            user_id=user_id,
            provider=provider,
            model=model,
        )
        if deployment is None:
            raise ProviderConfigurationError(
                "Legacy checkpoint runtime selection is unmapped and cannot run; "
                "reselect an available LLM deployment, then retry resume."
            )
        return self.build_deployment_runtime_selection(
            user_id=user_id,
            deployment_id=str(deployment.id),
            reasoning_effort=reasoning_effort,
            legacy_provider=provider,
            legacy_model=model,
        )

    @staticmethod
    def _hint_string(hint: dict, key: str) -> str | None:
        value = hint.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _is_deployment_runtime_hint(hint: dict) -> bool:
        return hint.get("schema_version") == 2 or "deployment_ref" in hint

    def _build_deployment_continuation_selection(
        self,
        *,
        user_id: int,
        checkpoint_hint: dict,
    ) -> LLMRuntimeSelectionV2:
        try:
            selection = LLMRuntimeSelectionV2.from_mapping(checkpoint_hint)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderConfigurationError(
                "Checkpoint deployment runtime selection is invalid and cannot run; "
                "reselect an available LLM deployment, then retry resume."
            ) from exc
        deployments = LLMDeploymentService(self._db)
        deployment = deployments.get_deployment(
            user_id=user_id,
            deployment_id=selection.deployment_ref.deployment_id,
        )
        if selection.preferred_route_id is not None:
            route = deployments.get_route(
                user_id=user_id,
                route_id=selection.preferred_route_id,
            )
            if route.deployment_id != deployment.id or not route.enabled:
                raise LLMDeploymentNotFoundError(
                    "Preferred deployment route is unavailable"
                )
        return selection

    def _find_legacy_checkpoint_deployment(
        self,
        *,
        user_id: int,
        provider: str,
        model: str,
    ) -> LLMModelDeployment | None:
        self._migration.backfill_deployment_identity_for_user(user_id)
        provider_id = provider.strip().lower()
        model_id = model.strip()
        if not provider_id or not model_id:
            return None
        return self._db.execute(
            select(LLMModelDeployment)
            .join(
                LLMInferenceConnection,
                LLMInferenceConnection.id == LLMModelDeployment.connection_id,
            )
            .where(
                LLMInferenceConnection.user_id == user_id,
                LLMInferenceConnection.legacy_default_provider == provider_id,
                LLMModelDeployment.wire_model_id == model_id,
            )
            .order_by(LLMModelDeployment.created_at.asc())
        ).scalars().first()


__all__ = ["LLMRuntimeConfigService"]
