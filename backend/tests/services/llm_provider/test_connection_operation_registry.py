"""Tests for the code-owned LLM connection operation registry."""

from __future__ import annotations

import inspect

import pytest

from backend.services.llm_provider.guarded_transport import GuardedTransport
from backend.services.llm_provider.operation_registry import (
    ConnectionOperationRegistry,
    OperationRegistryError,
)
from backend.services.llm_provider.types import LLMConnectionOperation


def test_registry_exposes_only_code_owned_operation_ids() -> None:
    """The Phase 1 operation vocabulary is fixed and complete."""

    registry = ConnectionOperationRegistry()

    assert registry.list_operation_ids() == (
        "capability_probe",
        "health",
        "inference",
        "inventory",
        "lifecycle_create",
        "lifecycle_delete",
    )
    assert set(LLMConnectionOperation) == {
        LLMConnectionOperation.HEALTH,
        LLMConnectionOperation.INVENTORY,
        LLMConnectionOperation.CAPABILITY_PROBE,
        LLMConnectionOperation.LIFECYCLE_CREATE,
        LLMConnectionOperation.LIFECYCLE_DELETE,
        LLMConnectionOperation.INFERENCE,
    }


@pytest.mark.parametrize(
    ("provider", "operation", "expected_url", "expected_method"),
    [
        ("openai", "health", "https://api.openai.com/v1/models", "GET"),
        ("anthropic", "inventory", "https://api.anthropic.com/v1/models", "GET"),
        (
            "openai",
            "capability_probe",
            "https://api.openai.com/v1/chat/completions",
            "POST",
        ),
        (
            "anthropic",
            "inference",
            "https://api.anthropic.com/v1/messages",
            "POST",
        ),
        (
            "openai",
            "lifecycle_create",
            "https://api.openai.com/v1/conversations",
            "POST",
        ),
    ],
)
def test_registry_resolves_fixed_provider_targets(
    provider: str,
    operation: str,
    expected_url: str,
    expected_method: str,
) -> None:
    """Provider and operation resolve to code-owned method and endpoint data."""

    target = ConnectionOperationRegistry().resolve(operation, provider=provider)

    assert target.url == expected_url
    assert target.method == expected_method
    assert target.provider == provider


def test_registry_validates_lifecycle_resource_id_as_one_path_segment() -> None:
    """Lifecycle deletion accepts an opaque segment but no path injection."""

    registry = ConnectionOperationRegistry()
    target = registry.resolve(
        "lifecycle_delete",
        provider="openai",
        resource_id="conv_ABC-123",
    )
    assert target.url == "https://api.openai.com/v1/conversations/conv_ABC-123"

    for resource_id in ("../admin", "a/b", "", "conv?id=1", "conv%2fother"):
        with pytest.raises(OperationRegistryError):
            registry.resolve(
                "lifecycle_delete",
                provider="openai",
                resource_id=resource_id,
            )


def test_registry_rejects_unknown_operations_and_unsupported_provider_matrix() -> None:
    """Unknown IDs and unsupported provider operations cannot become side paths."""

    registry = ConnectionOperationRegistry()
    with pytest.raises(OperationRegistryError):
        registry.resolve("arbitrary_fetch", provider="openai")
    with pytest.raises(OperationRegistryError):
        registry.resolve("health", provider="custom")
    with pytest.raises(OperationRegistryError):
        registry.resolve("lifecycle_create", provider="anthropic")


def test_registry_and_transport_have_no_raw_url_or_header_inputs() -> None:
    """Services cannot feed arbitrary destinations or headers through the seam."""

    resolve_parameters = inspect.signature(
        ConnectionOperationRegistry.resolve
    ).parameters
    execute_parameters = inspect.signature(GuardedTransport.execute).parameters

    for forbidden in ("url", "endpoint", "headers", "follow_redirects", "proxies"):
        assert forbidden not in resolve_parameters
        assert forbidden not in execute_parameters

    registry = ConnectionOperationRegistry()
    with pytest.raises(TypeError):
        registry.resolve(
            "health",
            provider="openai",
            endpoint="https://attacker.invalid",  # type: ignore[call-arg]
        )
