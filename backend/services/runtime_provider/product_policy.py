"""Product runtime placement policy for runner-owned task execution.

Responsibilities:
- Resolve product runtime policy from generated deployment configuration.
- Decide whether a runtime placement is allowed for a product or non-product scope.
- Keep policy decisions independent of Docker provider construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.config.generated_config import (
    CLOUD_RUNNER_CONTROL_ENABLED_ENV,
    DEPLOYMENT_PROFILE_ENV,
    RUNNER_TOOL_COMMAND_ENABLED_ENV,
    TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
    resolve_config_bool,
    resolve_config_value,
)

from .contracts import RuntimeCallScope, RuntimePlacementMode


class ProductRuntimePolicyError(ValueError):
    """Raised when product runtime policy is invalid for product execution."""


@dataclass(frozen=True, slots=True)
class ProductRuntimePolicy:
    """Resolved product runtime policy values."""

    profile: str
    product_runtime_placement: str
    cloud_runner_control_enabled: bool
    runner_tool_command_enabled: bool
    source: str


@dataclass(frozen=True, slots=True)
class RuntimePlacementDecision:
    """Decision for a requested runtime placement in a call scope."""

    allowed: bool
    placement: str | None
    reason_code: str | None
    message: str
    scope: str


_KNOWN_PROFILES = frozenset({"dev_local", "single_host", "distributed"})
_PRODUCT_PROFILES = frozenset({"single_host", "distributed"})
_LOCAL_ALLOWED_SCOPES = frozenset(
    {
        RuntimeCallScope.DIAGNOSTIC.value,
        RuntimeCallScope.TEST.value,
        RuntimeCallScope.DEV_OVERRIDE.value,
    }
)


def normalize_deployment_profile(profile: str | Any) -> str:
    """Normalize a deployment profile and fail closed for unsupported values."""
    candidate = getattr(profile, "value", profile)
    normalized = str(candidate or "").strip().lower()
    if not normalized:
        raise ProductRuntimePolicyError(f"{DEPLOYMENT_PROFILE_ENV} must not be empty.")
    if normalized not in _KNOWN_PROFILES:
        raise ProductRuntimePolicyError(
            "Unsupported deployment profile: "
            f"`{normalized}`. Expected one of: `dev_local`, `single_host`, `distributed`."
        )
    return normalized


def resolve_product_runtime_policy(
    *,
    profile: str | Any | None = None,
    product_runtime_placement: str | RuntimePlacementMode | None = None,
    cloud_runner_control_enabled: bool | None = None,
    runner_tool_command_enabled: bool | None = None,
    source: str | None = None,
) -> ProductRuntimePolicy:
    """Resolve product runtime policy using generated config with env override."""
    resolved_profile = normalize_deployment_profile(
        profile
        if profile is not None
        else resolve_config_value(DEPLOYMENT_PROFILE_ENV, "dev_local")
    )
    resolved_placement = _normalize_placement(
        product_runtime_placement
        if product_runtime_placement is not None
        else resolve_config_value(TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV, "local")
    ).value
    return ProductRuntimePolicy(
        profile=resolved_profile,
        product_runtime_placement=resolved_placement,
        cloud_runner_control_enabled=(
            bool(cloud_runner_control_enabled)
            if cloud_runner_control_enabled is not None
            else resolve_config_bool(CLOUD_RUNNER_CONTROL_ENABLED_ENV, default=False)
        ),
        runner_tool_command_enabled=(
            bool(runner_tool_command_enabled)
            if runner_tool_command_enabled is not None
            else resolve_config_bool(RUNNER_TOOL_COMMAND_ENABLED_ENV, default=False)
        ),
        source=source or "generated_config",
    )


def validate_product_runtime_policy(policy: ProductRuntimePolicy) -> None:
    """Validate that a policy can support product runner-only execution."""
    profile = normalize_deployment_profile(policy.profile)
    if profile not in _PRODUCT_PROFILES:
        return

    if policy.product_runtime_placement != RuntimePlacementMode.RUNNER.value:
        raise ProductRuntimePolicyError(
            f"DROWAI_DEPLOYMENT_PROFILE={profile} requires "
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner so local Docker provider is not selected."
        )
    if not policy.cloud_runner_control_enabled:
        raise ProductRuntimePolicyError(
            f"DROWAI_DEPLOYMENT_PROFILE={profile} requires "
            "ENABLE_CLOUD_RUNNER_CONTROL=true."
        )
    if not policy.runner_tool_command_enabled:
        raise ProductRuntimePolicyError(
            f"DROWAI_DEPLOYMENT_PROFILE={profile} requires "
            "RUNNER_TOOL_COMMAND_ENABLED=true."
        )


def is_local_placement_allowed(scope: str | RuntimeCallScope) -> bool:
    """Return whether a scope may explicitly request local placement."""
    return _normalize_scope(scope) in _LOCAL_ALLOWED_SCOPES


def decide_runtime_placement(
    *,
    policy: ProductRuntimePolicy,
    scope: str | RuntimeCallScope,
    requested_placement: str | RuntimePlacementMode | None,
) -> RuntimePlacementDecision:
    """Decide the effective runtime placement for a scoped call."""
    normalized_scope = _normalize_scope(scope)
    if normalized_scope not in {item.value for item in RuntimeCallScope}:
        return RuntimePlacementDecision(
            allowed=False,
            placement=None,
            reason_code="INVALID_RUNTIME_CALL_SCOPE",
            message=f"Unsupported runtime call scope: `{normalized_scope}`.",
            scope=normalized_scope,
        )

    placement_candidate = (
        requested_placement
        if requested_placement is not None
        else policy.product_runtime_placement
    )
    try:
        placement = _normalize_placement(placement_candidate)
    except ProductRuntimePolicyError as exc:
        return RuntimePlacementDecision(
            allowed=False,
            placement=None,
            reason_code="INVALID_RUNTIME_PLACEMENT",
            message=str(exc),
            scope=normalized_scope,
        )

    if placement is RuntimePlacementMode.LOCAL and _is_product_scope(normalized_scope):
        return RuntimePlacementDecision(
            allowed=False,
            placement=None,
            reason_code="PRODUCT_LOCAL_PLACEMENT_FORBIDDEN",
            message="Product task execution must use runner placement.",
            scope=normalized_scope,
        )
    if placement is RuntimePlacementMode.LOCAL and not is_local_placement_allowed(
        normalized_scope
    ):
        return RuntimePlacementDecision(
            allowed=False,
            placement=None,
            reason_code="LOCAL_PLACEMENT_SCOPE_FORBIDDEN",
            message="Local placement requires an explicit dev, test, or diagnostic scope.",
            scope=normalized_scope,
        )

    return RuntimePlacementDecision(
        allowed=True,
        placement=placement.value,
        reason_code=None,
        message="Runtime placement allowed.",
        scope=normalized_scope,
    )


def _normalize_placement(
    placement: str | RuntimePlacementMode | None,
) -> RuntimePlacementMode:
    if isinstance(placement, RuntimePlacementMode):
        return placement
    normalized = str(placement or "").strip().lower()
    if not normalized:
        raise ProductRuntimePolicyError("Runtime placement must not be empty.")
    try:
        return RuntimePlacementMode(normalized)
    except ValueError as exc:
        raise ProductRuntimePolicyError(
            f"Unsupported runtime placement mode: `{normalized}`."
        ) from exc


def _normalize_scope(scope: str | RuntimeCallScope) -> str:
    candidate = getattr(scope, "value", scope)
    return str(candidate or "").strip().lower()


def _is_product_scope(scope: str) -> bool:
    return scope in {RuntimeCallScope.PRODUCT.value, RuntimeCallScope.PRODUCT_TASK.value}


__all__ = [
    "ProductRuntimePolicy",
    "ProductRuntimePolicyError",
    "RuntimeCallScope",
    "RuntimePlacementDecision",
    "decide_runtime_placement",
    "is_local_placement_allowed",
    "normalize_deployment_profile",
    "resolve_product_runtime_policy",
    "validate_product_runtime_policy",
]
