"""Provider runtime environment construction for container compatibility.

This service centralizes provider credential environment variables used by
task containers. It preserves current OpenAI `OPENAI_API_KEY` behavior while
keeping Docker configuration code out of credential storage details.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID

from .credential_service import LLMCredentialService
from .runtime_config_service import LLMRuntimeConfigService
logger = logging.getLogger(__name__)


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
        """Return provider-specific container environment variables."""

        selection = self._runtime_config_service.build_runtime_selection(user_id=user_id)
        environment = {
            "LLM_PROVIDER": selection.provider,
            "LLM_MODEL": selection.model,
        }
        if selection.provider == OPENAI_PROVIDER_ID:
            secret = self._credential_service.resolve_secret(
                selection.credential_ref,
                runtime_user_id=user_id,
                task_id=task_id,
                purpose="container_environment",
            )
            environment["OPENAI_API_KEY"] = secret.value

        return environment


__all__ = ["LLMProviderEnvironmentService"]
