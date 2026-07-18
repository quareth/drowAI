"""Tests for remote conversation lifecycle through guarded operations."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import backend.services.llm_provider.conversation_lifecycle_service as lifecycle_module
from backend.services.llm_provider.conversation_lifecycle_service import (
    LLMConversationLifecycleService,
)
from backend.services.llm_provider.guarded_transport import GuardedTransportError
from backend.services.llm_provider.types import (
    CredentialNotFoundError,
    GuardedHTTPResponse,
    LLMCredentialRef,
    LLMConnectionOperation,
    ProviderConfigurationError,
    ProviderSecret,
)


class _CredentialService:
    """Credential double returning one provider-bound short-lived secret."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resolve_secret(self, ref: LLMCredentialRef, **kwargs: Any) -> ProviderSecret:
        self.calls.append({"ref": ref, **kwargs})
        return ProviderSecret(provider=ref.provider, value="sk-life")


class _LifecycleTransport:
    """Guarded transport double for create/delete operation assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.create_body = b'{"id":"conv-created"}'
        self.error: Exception | None = None

    def execute(self, operation: Any, **kwargs: Any) -> GuardedHTTPResponse:
        self.calls.append({"operation": operation, **kwargs})
        if self.error is not None:
            raise self.error
        body = (
            self.create_body
            if operation == LLMConnectionOperation.LIFECYCLE_CREATE
            else b"{}"
        )
        return GuardedHTTPResponse(
            status_code=200,
            body=body,
            audit_id="lifecycle-audit",
        )


def _service(
    transport: _LifecycleTransport,
    credentials: _CredentialService,
) -> LLMConversationLifecycleService:
    """Build a lifecycle service with deterministic secret and transport seams."""

    return LLMConversationLifecycleService(
        object(),  # type: ignore[arg-type]
        credential_service=credentials,  # type: ignore[arg-type]
        guarded_transport=transport,  # type: ignore[arg-type]
    )


def test_openai_create_and_delete_use_registered_guarded_operations() -> None:
    """OpenAI lifecycle preserves IDs while using only registered operations."""

    transport = _LifecycleTransport()
    credentials = _CredentialService()
    service = _service(transport, credentials)

    created_id = service._create_openai_conversation("sk-life")
    service._delete_openai_conversation("sk-life", created_id)

    assert created_id == "conv-created"
    assert transport.calls == [
        {
            "operation": LLMConnectionOperation.LIFECYCLE_CREATE,
            "provider": "openai",
            "secret": ProviderSecret(provider="openai", value="sk-life"),
            "json_body": {},
        },
        {
            "operation": LLMConnectionOperation.LIFECYCLE_DELETE,
            "provider": "openai",
            "secret": ProviderSecret(provider="openai", value="sk-life"),
            "resource_id": "conv-created",
        },
    ]
    assert credentials.calls == []


def test_lifecycle_remains_openai_only_and_fails_before_secret_resolution() -> None:
    """Anthropic cannot acquire a secret or invoke an unregistered lifecycle call."""

    transport = _LifecycleTransport()
    credentials = _CredentialService()
    service = _service(transport, credentials)

    with pytest.raises(ProviderConfigurationError, match="does not support"):
        service.require_remote_conversation_lifecycle("anthropic")

    assert credentials.calls == []
    assert transport.calls == []


def test_create_preserves_missing_remote_id_error() -> None:
    """A successful response without an ID retains the existing service error."""

    transport = _LifecycleTransport()
    transport.create_body = b'{"object":"conversation"}'
    service = _service(transport, _CredentialService())

    with pytest.raises(
        CredentialNotFoundError,
        match="OpenAI did not return a conversation id",
    ):
        service._create_openai_conversation("sk-life")


def test_create_preserves_provider_configuration_error_mapping() -> None:
    """Guarded failures retain the route's conversation-create error category."""

    transport = _LifecycleTransport()
    transport.error = GuardedTransportError(
        "Guarded upstream response rejected",
        audit_id="lifecycle-audit",
        status_code=503,
    )
    service = _service(transport, _CredentialService())

    with pytest.raises(
        ProviderConfigurationError,
        match="OpenAI conversation create failed",
    ):
        service._create_openai_conversation("sk-life")


def test_lifecycle_service_has_no_direct_provider_sdk_construction() -> None:
    """Lifecycle cannot bypass guarded egress with a direct OpenAI SDK client."""

    source = inspect.getsource(lifecycle_module)

    assert "openai.OpenAI" not in source
    assert ".conversations.create(" not in source
    assert ".conversations.delete(" not in source
