"""Orchestrate the transaction-owning LLM model catalog use case.

This service owns deployment backfill, non-secret catalog input loading,
projection, outcome materialization, and the request transaction boundary. It
excludes HTTP adaptation, public schemas, raw credential access, and egress.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .application_contracts import CatalogOutcome
from .catalog_projection_service import LLMCatalogProjectionService
from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .migration_service import LLMProviderMigrationService
from .types import ProviderConfigurationError

_CATALOG_FAILURE_DETAIL = "LLM catalog application failed"


class LLMCatalogApplicationService:
    """Build and commit one owner-scoped transport-neutral catalog result."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
        migration_service: LLMProviderMigrationService | None = None,
        projection_service: LLMCatalogProjectionService | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._credentials = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
        )
        self._migration = migration_service or LLMProviderMigrationService(db)
        self._projection = projection_service or LLMCatalogProjectionService(db)

    def list_models(self, *, user_id: int) -> CatalogOutcome:
        """Return the committed model catalog for one owner."""

        try:
            self._migration.backfill_deployment_identity_for_user(user_id)
            providers = self._catalog.list_providers()
            credential_statuses = {
                provider.id: self._credentials.get_masked_status(
                    user_id,
                    provider.id,
                )
                for provider in providers
            }
            projected = self._projection.project(
                user_id=user_id,
                providers=providers,
                credential_statuses=credential_statuses,
            )
            outcome = CatalogOutcome(providers=tuple(projected.providers))
            self._db.commit()
            return outcome
        except Exception:
            try:
                self._db.rollback()
            finally:
                raise ProviderConfigurationError(_CATALOG_FAILURE_DETAIL) from None


__all__ = ["LLMCatalogApplicationService"]
