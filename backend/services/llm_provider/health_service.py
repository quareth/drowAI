"""Provider credential health checks behind a backend service boundary.

This service owns provider SDK probes used by compatibility routes. It maps
provider-specific failures to provider-neutral service errors without logging
raw credentials.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    normalize_provider_id,
)

from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .types import CredentialNotFoundError, ProviderConfigurationError, ProviderHealthCheckResult


class LLMProviderHealthService:
    """Run provider credential health checks."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._credential_service = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
        )

    def test_credential(
        self,
        *,
        user_id: int,
        provider: str,
        api_key: str | None = None,
    ) -> ProviderHealthCheckResult:
        """Test a supplied plaintext key or the stored provider credential."""

        normalized_provider = normalize_provider_id(provider)
        self._catalog.require_provider(normalized_provider)
        resolved_key = (api_key or "").strip()
        if not resolved_key:
            ref = self._credential_service.get_credential_ref(user_id, normalized_provider)
            resolved_key = self._credential_service.resolve_secret(
                ref,
                runtime_user_id=user_id,
                task_id=None,
                purpose="provider_health_check",
            ).value
        if not resolved_key:
            raise CredentialNotFoundError(f"{normalized_provider} credential is not configured")

        if normalized_provider == ANTHROPIC_PROVIDER_ID:
            return self._test_anthropic_key(resolved_key)
        if normalized_provider != OPENAI_PROVIDER_ID:
            raise ProviderConfigurationError(f"Health check is not implemented for provider {normalized_provider}")
        return self._test_openai_key(resolved_key)

    @staticmethod
    def _test_openai_key(api_key: str) -> ProviderHealthCheckResult:
        import openai

        try:
            client = openai.OpenAI(api_key=api_key)
            response = client.models.list()
            model_count = len(response.data) if hasattr(response, "data") else 0
            return ProviderHealthCheckResult(
                provider=OPENAI_PROVIDER_ID,
                status="success",
                message="OpenAI API key is valid",
                model_count=model_count,
            )
        except openai.AuthenticationError as exc:
            raise ProviderConfigurationError("Invalid OpenAI API key") from exc
        except openai.PermissionDeniedError as exc:
            raise ProviderConfigurationError("OpenAI API key lacks necessary permissions") from exc
        except openai.RateLimitError as exc:
            raise ProviderConfigurationError("OpenAI API rate limit exceeded") from exc
        except Exception as exc:
            raise ProviderConfigurationError(f"OpenAI API error: {exc}") from exc

    @staticmethod
    def _test_anthropic_key(api_key: str) -> ProviderHealthCheckResult:
        import anthropic

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.models.list(limit=1)
            model_count = len(response.data) if hasattr(response, "data") else 0
            return ProviderHealthCheckResult(
                provider=ANTHROPIC_PROVIDER_ID,
                status="success",
                message="Anthropic API key is valid",
                model_count=model_count,
            )
        except anthropic.AuthenticationError as exc:
            raise ProviderConfigurationError("Invalid Anthropic API key") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderConfigurationError("Anthropic API key lacks necessary permissions") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderConfigurationError("Anthropic API rate limit exceeded") from exc
        except Exception as exc:
            raise ProviderConfigurationError(f"Anthropic API error: {exc}") from exc


__all__ = ["LLMProviderHealthService"]
