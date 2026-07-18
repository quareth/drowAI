"""Tests for fixed-target LLM endpoint and DNS egress validation."""

from __future__ import annotations

import pytest

from backend.services.llm_provider.egress_policy import (
    EgressPolicyError,
    FixedProviderEgressPolicy,
)


def _policy(*addresses: str) -> FixedProviderEgressPolicy:
    """Build a policy with deterministic DNS answers."""

    resolved = addresses or ("93.184.216.34",)
    return FixedProviderEgressPolicy(
        dns_resolver=lambda _host, _port: resolved,
    )


def test_fixed_endpoint_validates_exact_origin_path_and_public_dns() -> None:
    """A code-owned HTTPS target resolves to a validated immutable target."""

    target = _policy().validate_endpoint(
        "https://api.openai.com/v1/models",
        expected_host="api.openai.com",
        allowed_ports=frozenset({443}),
        allowed_path_prefixes=("/v1/",),
    )

    assert target.scheme == "https"
    assert target.host == "api.openai.com"
    assert target.port == 443
    assert target.path == "/v1/models"
    assert target.resolved_addresses == ("93.184.216.34",)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.openai.com/v1/models",
        "https://user:password@api.openai.com/v1/models",
        "https://api.openai.com:8443/v1/models",
        "https://other.example/v1/models",
        "https://api.openai.com/v2/models",
        "https://api.openai.com/v1/../admin",
        "https://api.openai.com/v1/%2fadmin",
        "https://api.openai.com/v1/models?token=secret",
        "https://api.openai.com/v1/models#fragment",
        "https://api.openai.com/v1/mo dels",
    ],
)
def test_endpoint_policy_rejects_unsafe_url_forms(endpoint: str) -> None:
    """Scheme, origin, path, userinfo, query, fragment, and whitespace fail closed."""

    with pytest.raises(EgressPolicyError):
        _policy().validate_endpoint(
            endpoint,
            expected_host="api.openai.com",
            allowed_ports=frozenset({443}),
            allowed_path_prefixes=("/v1/",),
        )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "::1",
        "fc00::1",
    ],
)
def test_dns_policy_rejects_non_public_addresses(address: str) -> None:
    """Loopback, private, metadata/link-local, multicast, and local IPv6 are blocked."""

    with pytest.raises(EgressPolicyError, match="DNS"):
        _policy(address).validate_endpoint(
            "https://api.openai.com/v1/models",
            expected_host="api.openai.com",
            allowed_ports=frozenset({443}),
            allowed_path_prefixes=("/v1/",),
        )


def test_dns_revalidation_rejects_changed_address_set() -> None:
    """A target whose DNS answers change before send is rejected."""

    answers = iter(
        [
            ("93.184.216.34",),
            ("1.1.1.1",),
        ]
    )
    policy = FixedProviderEgressPolicy(
        dns_resolver=lambda _host, _port: next(answers),
    )
    target = policy.validate_endpoint(
        "https://api.openai.com/v1/models",
        expected_host="api.openai.com",
        allowed_ports=frozenset({443}),
        allowed_path_prefixes=("/v1/",),
    )

    with pytest.raises(EgressPolicyError, match="changed"):
        policy.revalidate(target)


def test_dns_resolution_failure_is_sanitized() -> None:
    """Resolver details and endpoint data are not reflected in policy errors."""

    def fail(_host: str, _port: int) -> tuple[str, ...]:
        raise OSError("resolver leaked https://internal.invalid?token=secret")

    policy = FixedProviderEgressPolicy(dns_resolver=fail)
    with pytest.raises(EgressPolicyError) as exc_info:
        policy.validate_endpoint(
            "https://api.openai.com/v1/models",
            expected_host="api.openai.com",
            allowed_ports=frozenset({443}),
            allowed_path_prefixes=("/v1/",),
        )

    assert "internal.invalid" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
