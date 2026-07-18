"""Provider-managed lifecycle through registered guarded operations.

This service owns guarded remote conversation create/delete orchestration.
Local conversation row persistence remains with route/runtime owners until the
route layer is moved in the next phase.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, normalize_provider_id

from .catalog_service import LLMProviderCatalogService
from .credential_service import LLMCredentialService
from .guarded_transport import GuardedTransport, GuardedTransportError
from .types import (
    CredentialNotFoundError,
    LLMCredentialRef,
    LLMConnectionOperation,
    ProviderConfigurationError,
    ProviderSecret,
)


class LLMConversationLifecycleService:
    """Run provider-managed remote conversation lifecycle calls."""

    def __init__(
        self,
        db: Session,
        *,
        catalog_service: LLMProviderCatalogService | None = None,
        credential_service: LLMCredentialService | None = None,
        guarded_transport: GuardedTransport | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog_service or LLMProviderCatalogService()
        self._credential_service = credential_service or LLMCredentialService(
            db,
            catalog_service=self._catalog,
        )
        self._guarded_transport = guarded_transport or GuardedTransport()

    def create_remote_conversation(
        self,
        *,
        credential_ref: LLMCredentialRef,
        runtime_user_id: int,
        task_id: int | None,
    ) -> str:
        """Create a provider-managed remote conversation and return its id."""

        provider = self._require_remote_lifecycle_provider(credential_ref.provider)
        secret = self._credential_service.resolve_secret(
            credential_ref,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose="remote_conversation_create",
        )
        if provider == OPENAI_PROVIDER_ID:
            return self._create_openai_conversation(secret.value)
        raise ProviderConfigurationError(
            "Remote conversation create is not implemented for provider "
            f"{provider}"
        )

    def delete_remote_conversation(
        self,
        *,
        credential_ref: LLMCredentialRef,
        runtime_user_id: int,
        task_id: int | None,
        conversation_id: str,
    ) -> None:
        """Delete a provider-managed remote conversation when supported."""

        provider = self._require_remote_lifecycle_provider(credential_ref.provider)
        if not conversation_id:
            return
        secret = self._credential_service.resolve_secret(
            credential_ref,
            runtime_user_id=runtime_user_id,
            task_id=task_id,
            purpose="remote_conversation_delete",
        )
        if provider == OPENAI_PROVIDER_ID:
            self._delete_openai_conversation(secret.value, conversation_id)
            return
        raise ProviderConfigurationError(
            "Remote conversation delete is not implemented for provider "
            f"{provider}"
        )

    def require_remote_conversation_lifecycle(self, provider: str) -> str:
        """Validate remote lifecycle support without performing SDK side effects."""

        return self._require_remote_lifecycle_provider(provider)

    def _require_remote_lifecycle_provider(self, provider: str) -> str:
        normalized_provider = normalize_provider_id(provider)
        provider_profile = self._catalog.require_provider(normalized_provider)
        try:
            provider_profile.require_capability(
                LLMCapability.REMOTE_CONVERSATION_LIFECYCLE
            )
        except Exception as exc:
            raise ProviderConfigurationError(
                f"Provider {normalized_provider} does not support remote "
                "conversation lifecycle"
            ) from exc
        return normalized_provider

    def _create_openai_conversation(self, api_key: str) -> str:
        try:
            response = self._guarded_transport.execute(
                LLMConnectionOperation.LIFECYCLE_CREATE,
                provider=OPENAI_PROVIDER_ID,
                secret=ProviderSecret(
                    provider=OPENAI_PROVIDER_ID,
                    value=api_key,
                ),
                json_body={},
            )
        except GuardedTransportError as exc:
            raise ProviderConfigurationError(
                f"OpenAI conversation create failed: {exc}"
            ) from None
        try:
            payload = json.loads(response.body)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise ProviderConfigurationError(
                "OpenAI conversation create failed: invalid provider response"
            ) from None
        conversation_id = payload.get("id") if isinstance(payload, dict) else None
        if not conversation_id:
            raise CredentialNotFoundError(
                "OpenAI did not return a conversation id"
            )
        return str(conversation_id)

    def _delete_openai_conversation(
        self,
        api_key: str,
        conversation_id: str,
    ) -> None:
        try:
            self._guarded_transport.execute(
                LLMConnectionOperation.LIFECYCLE_DELETE,
                provider=OPENAI_PROVIDER_ID,
                secret=ProviderSecret(
                    provider=OPENAI_PROVIDER_ID,
                    value=api_key,
                ),
                resource_id=conversation_id,
            )
        except GuardedTransportError as exc:
            raise ProviderConfigurationError(
                f"OpenAI conversation delete failed: {exc}"
            ) from None


__all__ = ["LLMConversationLifecycleService"]
