"""Deployment-profile parsing and fail-closed runtime validation.

This module resolves the active deployment profile (with legacy topology
compatibility mapping) and enforces product runtime guardrails for wired startup
or readiness paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.config.data_plane import (
    get_data_plane_config,
    has_object_store_bucket,
    is_non_local_object_store_backend,
)
from backend.services.runtime_provider.product_policy import (
    ProductRuntimePolicy,
    ProductRuntimePolicyError,
    normalize_deployment_profile,
    resolve_product_runtime_policy,
    validate_product_runtime_policy,
)


class DeploymentProfileValidationError(ValueError):
    """Raised when the current deployment profile is unsafe or incomplete."""


class DeploymentProfile(str, Enum):
    """Typed deployment profiles for runtime-path consolidation."""

    DEV_LOCAL = "dev_local"
    SINGLE_HOST = "single_host"
    DISTRIBUTED = "distributed"


@dataclass(frozen=True)
class DeploymentProfileState:
    """Resolved deployment profile with runtime-path configuration state."""

    profile: DeploymentProfile
    runtime_placement_mode: str
    cloud_runner_control_enabled: bool
    runner_tool_command_enabled: bool
    object_store_backend: str
    object_store_backend_non_local: bool
    object_store_bucket_configured: bool

    @property
    def topology(self) -> DeploymentProfile:
        """Backward-compatible alias for older topology field naming."""
        return self.profile


def resolve_deployment_profile(
    profile: str | DeploymentProfile | None = None,
) -> DeploymentProfile:
    """Resolve profile mode from explicit value or generated deployment config."""
    if isinstance(profile, DeploymentProfile):
        return profile
    try:
        policy = resolve_product_runtime_policy(profile=profile)
        return DeploymentProfile(normalize_deployment_profile(policy.profile))
    except (ProductRuntimePolicyError, ValueError) as exc:
        raise DeploymentProfileValidationError(str(exc)) from exc


def get_deployment_profile_state(
    topology: str | DeploymentProfile | None = None,
) -> DeploymentProfileState:
    """Build and validate the active deployment profile."""
    try:
        data_plane_config = get_data_plane_config()
        policy = resolve_product_runtime_policy(profile=topology)
    except (ProductRuntimePolicyError, ValueError) as exc:
        raise DeploymentProfileValidationError(str(exc)) from exc
    profile = DeploymentProfileState(
        profile=DeploymentProfile(policy.profile),
        runtime_placement_mode=policy.product_runtime_placement,
        cloud_runner_control_enabled=policy.cloud_runner_control_enabled,
        runner_tool_command_enabled=policy.runner_tool_command_enabled,
        object_store_backend=data_plane_config.object_store_backend,
        object_store_backend_non_local=is_non_local_object_store_backend(data_plane_config),
        object_store_bucket_configured=has_object_store_bucket(data_plane_config),
    )
    _validate_profile(profile)
    return profile


def _validate_profile(profile: DeploymentProfileState) -> None:
    if profile.profile is DeploymentProfile.DEV_LOCAL:
        return

    if profile.profile in {DeploymentProfile.SINGLE_HOST, DeploymentProfile.DISTRIBUTED}:
        try:
            validate_product_runtime_policy(
                ProductRuntimePolicy(
                    profile=profile.profile.value,
                    product_runtime_placement=profile.runtime_placement_mode,
                    cloud_runner_control_enabled=profile.cloud_runner_control_enabled,
                    runner_tool_command_enabled=profile.runner_tool_command_enabled,
                    source="deployment_topology",
                )
            )
        except ProductRuntimePolicyError as exc:
            raise DeploymentProfileValidationError(str(exc)) from exc
        if profile.object_store_backend_non_local and not profile.object_store_bucket_configured:
            raise DeploymentProfileValidationError(
                f"DROWAI_DEPLOYMENT_PROFILE={profile.profile.value} requires "
                "DATA_PLANE_OBJECT_STORE_BUCKET when DATA_PLANE_OBJECT_STORE_BACKEND is non-local."
            )
        return

    raise DeploymentProfileValidationError(
        f"Unsupported deployment profile `{profile.profile.value}`."
    )
