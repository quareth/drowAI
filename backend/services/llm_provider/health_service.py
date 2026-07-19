"""Provider credential health checks through registered guarded operations.

This service maps compatibility-route health requests and guarded transport
results to provider-neutral responses without exposing raw credentials.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    normalize_provider_id,
)

from .catalog_service import LLMProviderCatalogService
from .connection_authorization import LLMConnectionAuthorizer
from .connection_service import LLMConnectionService
from .credential_service import LLMCredentialService
from .guarded_transport import GuardedTransport, GuardedTransportError
from .inventory_service import GptOssProvingVerificationResult, LLMProviderInventoryService
from .types import (
    CredentialNotFoundError,
    LLMConnectionAccessContext,
    LLMConnectionOperation,
    ProviderConfigurationError,
    ProviderHealthCheckResult,
    ProviderSecret,
)


class LLMProviderHealthService:
    """Run provider credential health checks."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
        guarded_transport: GuardedTransport | None = None,
        connection_authorizer: LLMConnectionAuthorizer | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._credential_service = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
        )
        self._guarded_transport = guarded_transport or GuardedTransport()
        self._connection_authorizer = (
            connection_authorizer or LLMConnectionAuthorizer(db)
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
            self._authorize_stored_health_operation(
                user_id=user_id,
                provider=normalized_provider,
            )
            ref = self._credential_service.get_credential_ref(
                user_id,
                normalized_provider,
            )
            resolved_key = self._credential_service.resolve_secret(
                ref,
                runtime_user_id=user_id,
                task_id=None,
                purpose="provider_health_check",
            ).value
        if not resolved_key:
            raise CredentialNotFoundError(
                f"{normalized_provider} credential is not configured"
            )

        if normalized_provider == ANTHROPIC_PROVIDER_ID:
            return self._test_anthropic_key(resolved_key)
        if normalized_provider != OPENAI_PROVIDER_ID:
            raise ProviderConfigurationError(
                "Health check is not implemented for provider "
                f"{normalized_provider}"
            )
        return self._test_openai_key(resolved_key)

    def _authorize_stored_health_operation(
        self,
        *,
        user_id: int,
        provider: str,
    ) -> None:
        """Authorize the designated default connection before stored-key tests."""

        status = self._credential_service.get_masked_status(user_id, provider)
        if status.connection_id is None:
            raise CredentialNotFoundError(
                f"{provider} credential is not configured"
            )
        connection = LLMConnectionService(self._db).get_owned(
            user_id=user_id,
            connection_id=status.connection_id,
        )

    def verify_gpt_oss_20b_proving_connection(
        self,
        **kwargs,
    ) -> GptOssProvingVerificationResult:
        """Run the bounded GPT-OSS proving verification flow."""

        return LLMProviderInventoryService(
            self._db,
            guarded_transport=self._guarded_transport,
            connection_authorizer=self._connection_authorizer,
        ).verify_gpt_oss_20b_proving_connection(**kwargs)
        self._connection_authorizer.authorize(
            access_context=LLMConnectionAccessContext(
                authenticated_user_id=user_id,
            ),
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            operation=LLMConnectionOperation.HEALTH,
        )

    def _test_openai_key(self, api_key: str) -> ProviderHealthCheckResult:
        try:
            response = self._guarded_transport.execute(
                LLMConnectionOperation.HEALTH,
                provider=OPENAI_PROVIDER_ID,
                secret=ProviderSecret(
                    provider=OPENAI_PROVIDER_ID,
                    value=api_key,
                ),
            )
            model_count = _model_count(response.body)
            return ProviderHealthCheckResult(
                provider=OPENAI_PROVIDER_ID,
                status="success",
                message="OpenAI API key is valid",
                model_count=model_count,
            )
        except GuardedTransportError as exc:
            raise _health_error("OpenAI", exc) from None
        except (TypeError, ValueError, UnicodeDecodeError):
            raise ProviderConfigurationError(
                "OpenAI API error: invalid provider response"
            ) from None

    def _test_anthropic_key(self, api_key: str) -> ProviderHealthCheckResult:
        try:
            response = self._guarded_transport.execute(
                LLMConnectionOperation.HEALTH,
                provider=ANTHROPIC_PROVIDER_ID,
                secret=ProviderSecret(
                    provider=ANTHROPIC_PROVIDER_ID,
                    value=api_key,
                ),
            )
            model_count = min(_model_count(response.body), 1)
            return ProviderHealthCheckResult(
                provider=ANTHROPIC_PROVIDER_ID,
                status="success",
                message="Anthropic API key is valid",
                model_count=model_count,
            )
        except GuardedTransportError as exc:
            raise _health_error("Anthropic", exc) from None
        except (TypeError, ValueError, UnicodeDecodeError):
            raise ProviderConfigurationError(
                "Anthropic API error: invalid provider response"
            ) from None


def _model_count(body: bytes) -> int:
    """Count models in one bounded provider inventory response."""

    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("provider response has no model inventory")
    return len(payload["data"])


def _health_error(
    provider_label: str,
    exc: GuardedTransportError,
) -> ProviderConfigurationError:
    """Map safe guarded status metadata to stable provider health errors."""

    if exc.status_code == 401:
        return ProviderConfigurationError(f"Invalid {provider_label} API key")
    if exc.status_code == 403:
        return ProviderConfigurationError(
            f"{provider_label} API key lacks necessary permissions"
        )
    if exc.status_code == 429:
        return ProviderConfigurationError(
            f"{provider_label} API rate limit exceeded"
        )
    return ProviderConfigurationError(f"{provider_label} API error: {exc}")


__all__ = ["LLMProviderHealthService"]
