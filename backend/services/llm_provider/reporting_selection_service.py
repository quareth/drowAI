"""Reporting LLM selection service for task memos and reports.

This module owns user-level reporting deployment persistence and runtime
selection resolution. Provider/model fields remain compatibility snapshots;
credentials remain owned by the provider credential service.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import normalize_provider_id
from agent.providers.llm.core.reasoning_policy import (
    validate_reasoning_effort_for_provider_model,
)
from backend.models import UserReportingLLMSelection
from backend.config import E2E_DETERMINISTIC_MODE

from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .migration_service import LLMProviderMigrationService
from .selection_deployment_resolver import LLMSelectionDeploymentResolver
from .types import (
    DeploymentRef,
    LLMCredentialRef,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    LLMSelectionStatus,
    ProviderConfigurationError,
)


class ReportingLLMSelectionMissingError(ProviderConfigurationError):
    """Raised when reporting generation is requested without a configured model."""


@dataclass(frozen=True, slots=True)
class ReportingLLMSelectionRead:
    """Reporting model selection plus descriptive status."""

    selection: UserReportingLLMSelection | None
    status: LLMSelectionStatus


class ReportingLLMSelectionService:
    """Read, write, classify, and resolve the configured reporting model."""

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

    def get_selection_read(self, user_id: int) -> ReportingLLMSelectionRead:
        """Return the configured reporting selection and runnability status."""

        self._migration.backfill_deployment_identity_for_user(user_id)
        selection = self._get_selection_row(user_id)
        if selection is None:
            if E2E_DETERMINISTIC_MODE:
                return ReportingLLMSelectionRead(
                    selection=None,
                    status=LLMSelectionStatus(
                        status="deterministic_e2e",
                        selectable=True,
                        runnable=True,
                    ),
                )
            return ReportingLLMSelectionRead(
                selection=None,
                status=LLMSelectionStatus(
                    status="unset",
                    selectable=False,
                    runnable=False,
                    reason="Reporting model is not configured.",
                ),
            )
        return ReportingLLMSelectionRead(
            selection=selection,
            status=self._classify_selection(selection),
        )

    def set_selection(
        self,
        *,
        user_id: int,
        provider: str,
        model: str,
        reasoning_effort: str | None = None,
    ) -> UserReportingLLMSelection:
        """Persist a reporting provider/model selection after catalog validation."""

        profile = self._require_reporting_capable_model(provider=provider, model=model)
        normalized_provider = profile.ref.provider
        normalized_model = profile.ref.model
        normalized_effort = _normalize_reasoning_effort(
            provider=normalized_provider,
            model=normalized_model,
            reasoning_effort=reasoning_effort,
        )

        selection = self._get_selection_row(user_id)
        if selection is None:
            selection = UserReportingLLMSelection(
                user_id=int(user_id),
                provider=normalized_provider,
                model=normalized_model,
                reasoning_effort=normalized_effort,
            )
            self._db.add(selection)
        else:
            selection.provider = normalized_provider
            selection.model = normalized_model
            selection.reasoning_effort = normalized_effort
        selection.deployment_id = None
        self._db.flush()
        return selection

    def set_deployment_selection(
        self,
        *,
        user_id: int,
        deployment_id: str,
        expected_deployment_revision: int,
        reasoning_effort: str | None = None,
    ) -> UserReportingLLMSelection:
        """Persist a reporting deployment with compatibility snapshots."""

        target = self._deployment_resolver.resolve(
            user_id=user_id,
            deployment_id=deployment_id,
            expected_revision=expected_deployment_revision,
            role="reporting",
            require_structured_output=True,
        )
        normalized_effort = _normalize_reasoning_effort(
            provider=target.profile.ref.provider,
            model=target.profile.ref.model,
            reasoning_effort=reasoning_effort,
        )
        selection = self._get_selection_row(user_id)
        if selection is None:
            selection = UserReportingLLMSelection(user_id=int(user_id))
            self._db.add(selection)
        selection.provider = target.provider
        selection.model = target.model
        selection.deployment_id = target.deployment.id
        selection.reasoning_effort = normalized_effort
        self._db.flush()
        return selection

    def build_runtime_selection(self, *, user_id: int) -> LLMRuntimeSelection:
        """Return a runnable reporting runtime selection or raise a safe error."""

        if E2E_DETERMINISTIC_MODE:
            return LLMRuntimeSelection(
                provider="deterministic_e2e",
                model="offline-report-section-v1",
                credential_ref=LLMCredentialRef(
                    user_id=int(user_id),
                    provider="deterministic_e2e",
                ),
            )
        selection = self._get_selection_row(user_id)
        if selection is None:
            raise ReportingLLMSelectionMissingError(
                "Reporting model is not configured."
            )
        status = self._classify_selection(selection)
        if not status.runnable:
            raise ProviderConfigurationError(
                status.reason or "Reporting model is not runnable."
            )
        credential_ref = self._credential_service.get_credential_ref(
            int(user_id),
            selection.provider,
        )
        return LLMRuntimeSelection(
            provider=str(selection.provider),
            model=str(selection.model),
            credential_ref=credential_ref,
            reasoning_effort=selection.reasoning_effort,
        )

    def build_current_runtime_selection(
        self,
        *,
        user_id: int,
    ) -> LLMRuntimeSelection | LLMRuntimeSelectionV2:
        """Return the saved reporting runtime identity, preferring deployments."""

        if not E2E_DETERMINISTIC_MODE:
            selection = self._get_selection_row(user_id)
            if selection is not None and selection.deployment_id is not None:
                return self.build_deployment_runtime_selection(user_id=user_id)
        return self.build_runtime_selection(user_id=user_id)

    def build_deployment_runtime_selection(
        self,
        *,
        user_id: int,
    ) -> LLMRuntimeSelectionV2:
        """Build V2 reporting identity after current role/auth validation."""

        selection = self._get_selection_row(user_id)
        if selection is None or selection.deployment_id is None:
            raise ReportingLLMSelectionMissingError(
                "Reporting model has no deployment binding."
            )
        status = self._classify_selection(selection)
        if not status.runnable:
            raise ProviderConfigurationError(
                status.reason or "Reporting deployment is not runnable."
            )
        target = self._deployment_resolver.resolve(
            user_id=user_id,
            deployment_id=selection.deployment_id,
            role="reporting",
            require_structured_output=True,
        )
        return LLMRuntimeSelectionV2(
            deployment_ref=DeploymentRef(
                deployment_id=str(target.deployment.id),
                expected_revision=int(target.deployment.revision),
            ),
            reasoning_effort=selection.reasoning_effort,
            legacy_provider=selection.provider,
            legacy_model=selection.model,
        )

    def _get_selection_row(
        self,
        user_id: int,
    ) -> UserReportingLLMSelection | None:
        return self._db.execute(
            select(UserReportingLLMSelection).where(
                UserReportingLLMSelection.user_id == int(user_id)
            )
        ).scalar_one_or_none()

    def _classify_selection(
        self,
        selection: UserReportingLLMSelection,
    ) -> LLMSelectionStatus:
        try:
            target = None
            if selection.deployment_id is not None:
                target = self._deployment_resolver.resolve(
                    user_id=int(selection.user_id),
                    deployment_id=selection.deployment_id,
                    role="reporting",
                    require_structured_output=True,
                )
                profile = target.profile
            else:
                profile = self._require_reporting_capable_model(
                    provider=selection.provider,
                    model=selection.model,
                )
            _normalize_reasoning_effort(
                provider=profile.ref.provider,
                model=profile.ref.model,
                reasoning_effort=selection.reasoning_effort,
            )
        except Exception as exc:
            return LLMSelectionStatus(
                status="model_unavailable",
                selectable=False,
                runnable=False,
                reason=str(exc),
            )

        if target is not None:
            deployment_status = self._deployment_resolver.classify_runnability(
                user_id=int(selection.user_id),
                target=target,
                credential_available=self._credential_service.has_enabled_credential,
                missing_credential_reason=(
                    "Deployment credential is required for reporting generation"
                ),
            )
            if deployment_status is not None:
                return deployment_status
        elif not self._credential_service.has_enabled_credential(
            user_id=int(selection.user_id), provider=profile.ref.provider
        ):
            return LLMSelectionStatus(
                status="credential_missing",
                selectable=True,
                runnable=False,
                reason=f"{profile.ref.provider} credential is required for reporting generation",
            )

        return LLMSelectionStatus(
            status="selectable",
            selectable=True,
            runnable=True,
        )

    def _require_reporting_capable_model(self, *, provider: str, model: str):
        normalized_provider = normalize_provider_id(provider)
        profile = self._catalog.require_selectable_model(normalized_provider, model)
        if not profile.structured_output_strategies:
            raise ProviderConfigurationError(
                f"Model '{profile.ref}' does not support reporting structured output"
            )
        return profile


def _normalize_reasoning_effort(
    *,
    provider: str,
    model: str,
    reasoning_effort: str | None,
) -> str | None:
    if reasoning_effort is None:
        return None
    effort = str(reasoning_effort).strip()
    if not effort:
        return None
    try:
        return validate_reasoning_effort_for_provider_model(
            provider=provider,
            model=model,
            effort=effort,
        )
    except ValueError as exc:
        raise ProviderConfigurationError(str(exc)) from exc


__all__ = [
    "ReportingLLMSelectionMissingError",
    "ReportingLLMSelectionRead",
    "ReportingLLMSelectionService",
]
