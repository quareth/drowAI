"""Non-secret provider metadata construction for task containers.

This service exposes selected provider/model identifiers for diagnostics while
keeping all connection credentials in the backend LLM client boundary.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .credential_service import LLMCredentialService
from .runtime_config_service import LLMRuntimeConfigService


class LLMProviderEnvironmentService:
    """Build provider environment variables for runtime containers."""

    def __init__(
        self,
        db: Session,
        *,
        credential_service: LLMCredentialService | None = None,
        runtime_config_service: LLMRuntimeConfigService | None = None,
    ) -> None:
        self._db = db
        self._credential_service = credential_service or LLMCredentialService(db)
        self._runtime_config_service = runtime_config_service or LLMRuntimeConfigService(
            db,
            credential_service=self._credential_service,
        )

    def build_environment(self, *, user_id: int, task_id: int | None = None) -> dict[str, str]:
        """Return non-secret provider metadata for a task container."""

        _ = task_id  # Compatibility parameter; secrets are never resolved per task.
        selection = self._runtime_config_service.build_runtime_selection(
            user_id=user_id,
            require_enabled_credential=False,
        )
        return {
            "LLM_PROVIDER": selection.provider,
            "LLM_MODEL": selection.model,
        }


__all__ = ["LLMProviderEnvironmentService"]
