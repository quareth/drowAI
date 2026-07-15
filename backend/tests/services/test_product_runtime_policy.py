"""Product runtime policy contract tests for runner-only task execution.

Responsibilities:
- Define the product runtime placement contract before the policy service exists.
- Keep runner-only product placement separate from explicit dev/test/diagnostic local use.
- Prove the policy decision API is structured and Docker-free.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


_POLICY_MODULE = "backend.services.runtime_provider.product_policy"
_LOCAL_PROVIDER_MODULE = "backend.services.runtime_provider.local_docker_provider"


def _load_policy_module() -> ModuleType:
    return importlib.import_module(_POLICY_MODULE)


def _policy(
    *,
    profile: str,
    product_runtime_placement: str = "runner",
):
    module = _load_policy_module()
    return module.ProductRuntimePolicy(
        profile=profile,
        product_runtime_placement=product_runtime_placement,
        cloud_runner_control_enabled=True,
        runner_tool_command_enabled=True,
        source="test_contract",
    )


def _decide(
    *,
    profile: str,
    scope: str,
    requested_placement: str | None,
):
    module = _load_policy_module()
    return module.decide_runtime_placement(
        policy=_policy(profile=profile),
        scope=scope,
        requested_placement=requested_placement,
    )


def test_policy_contract_types_are_structured_and_scoped() -> None:
    module = _load_policy_module()

    assert hasattr(module, "ProductRuntimePolicy")
    assert hasattr(module, "RuntimePlacementDecision")
    assert set(module.ProductRuntimePolicy.__dataclass_fields__) >= {
        "profile",
        "product_runtime_placement",
        "cloud_runner_control_enabled",
        "runner_tool_command_enabled",
        "source",
    }
    assert set(module.RuntimePlacementDecision.__dataclass_fields__) >= {
        "allowed",
        "placement",
        "reason_code",
        "message",
        "scope",
    }


@pytest.mark.parametrize("profile", ("single_host", "distributed"))
def test_product_profiles_resolve_runner_for_product_scope(profile: str) -> None:
    decision = _decide(
        profile=profile,
        scope="product",
        requested_placement=None,
    )

    assert decision.allowed is True
    assert decision.placement == "runner"
    assert decision.reason_code is None
    assert decision.scope == "product"


@pytest.mark.parametrize("profile", ("single_host", "distributed", "dev_local"))
def test_product_scope_rejects_local_without_constructing_local_provider(
    profile: str,
) -> None:
    sys.modules.pop(_LOCAL_PROVIDER_MODULE, None)

    decision = _decide(
        profile=profile,
        scope="product",
        requested_placement="local",
    )

    assert decision.allowed is False
    assert decision.placement is None
    assert decision.reason_code == "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"
    assert decision.scope == "product"
    assert _LOCAL_PROVIDER_MODULE not in sys.modules


@pytest.mark.parametrize("scope", ("diagnostic", "test", "dev_override"))
def test_dev_local_allows_explicit_local_for_non_product_scope(scope: str) -> None:
    decision = _decide(
        profile="dev_local",
        scope=scope,
        requested_placement="local",
    )

    assert decision.allowed is True
    assert decision.placement == "local"
    assert decision.reason_code is None
    assert decision.scope == scope


@pytest.mark.parametrize("requested_placement", ("", "unexpected"))
def test_unknown_or_empty_placement_fails_closed(
    requested_placement: str,
) -> None:
    decision = _decide(
        profile="single_host",
        scope="product",
        requested_placement=requested_placement,
    )

    assert decision.allowed is False
    assert decision.placement is None
    assert decision.reason_code == "INVALID_RUNTIME_PLACEMENT"
    assert decision.scope == "product"
