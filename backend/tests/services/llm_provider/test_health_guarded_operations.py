"""Tests for provider health checks through registered guarded operations."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import backend.services.llm_provider.health_service as health_service_module
from backend.services.llm_provider.guarded_transport import GuardedTransportError
from backend.services.llm_provider.health_service import LLMProviderHealthService
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionOperation,
    ProviderConfigurationError,
    ProviderSecret,
)


class _UnusedCredentialService:
    """Credential double proving supplied health keys need no storage lookup."""

    def get_credential_ref(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("credential lookup was not expected")


class _RecordingTransport:
    """Guarded transport double recording typed operation calls."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def execute(self, operation: Any, **kwargs: Any) -> GuardedHTTPResponse:
        self.calls.append({"operation": operation, **kwargs})
        if self.error is not None:
            raise self.error
        return GuardedHTTPResponse(
            status_code=200,
            body=self.body,
            audit_id="health-audit",
        )


@pytest.mark.parametrize(
    ("provider", "body", "expected_count", "expected_message"),
    [
        (
            "openai",
            b'{"data":[{"id":"gpt-a"},{"id":"gpt-b"}]}',
            2,
            "OpenAI API key is valid",
        ),
        (
            "anthropic",
            b'{"data":[{"id":"claude-a"}]}',
            1,
            "Anthropic API key is valid",
        ),
    ],
)
def test_health_uses_registered_guarded_operation(
    provider: str,
    body: bytes,
    expected_count: int,
    expected_message: str,
) -> None:
    """Fixed-provider health probes use only the registered health operation."""

    transport = _RecordingTransport(body)
    service = LLMProviderHealthService(
        object(),  # type: ignore[arg-type]
        credential_service=_UnusedCredentialService(),  # type: ignore[arg-type]
        guarded_transport=transport,  # type: ignore[arg-type]
    )

    result = service.test_credential(
        user_id=7,
        provider=provider,
        api_key="sk-health",
    )

    assert result.provider == provider
    assert result.status == "success"
    assert result.message == expected_message
    assert result.model_count == expected_count
    assert transport.calls == [
        {
            "operation": LLMConnectionOperation.HEALTH,
            "provider": provider,
            "secret": ProviderSecret(provider=provider, value="sk-health"),
        }
    ]


@pytest.mark.parametrize(
    ("status_code", "expected_message"),
    [
        (401, "Invalid OpenAI API key"),
        (403, "OpenAI API key lacks necessary permissions"),
        (429, "OpenAI API rate limit exceeded"),
    ],
)
def test_health_preserves_provider_error_mapping(
    status_code: int,
    expected_message: str,
) -> None:
    """Guarded status categories retain existing route-visible error messages."""

    transport = _RecordingTransport(b"")
    transport.error = GuardedTransportError(
        "Guarded upstream response rejected",
        audit_id="health-audit",
        status_code=status_code,
    )
    service = LLMProviderHealthService(
        object(),  # type: ignore[arg-type]
        credential_service=_UnusedCredentialService(),  # type: ignore[arg-type]
        guarded_transport=transport,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderConfigurationError, match=expected_message):
        service.test_credential(
            user_id=7,
            provider="openai",
            api_key="sk-health",
        )


def test_health_service_has_no_direct_provider_sdk_construction() -> None:
    """Health cannot bypass guarded egress by constructing provider SDK clients."""

    source = inspect.getsource(health_service_module)

    assert "openai.OpenAI" not in source
    assert "anthropic.Anthropic" not in source
    assert ".models.list(" not in source
