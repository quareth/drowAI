"""Runner runtime-provider factory for managed-runner placement.

Responsibilities:
- Build the single managed runner provider used by product runner placement.
- Keep cloud-runner control enablement as an explicit safety gate.
"""

from __future__ import annotations

from collections.abc import Callable

from backend.config.feature_flags import is_cloud_runner_control_enabled

from .cloud_runner_provider import CloudRunnerRuntimeProvider
from .provider import TaskExecutionRuntimeProvider
ProviderFactory = Callable[[], TaskExecutionRuntimeProvider]


class ManagedRunnerProviderUnavailableError(ValueError):
    """Raised when managed-runner provider selection is not available safely."""


def validate_managed_runner_control_enabled(
    *,
    cloud_runner_control_enabled: bool | None = None,
) -> None:
    """Validate that managed runner control is enabled for runner placement."""
    cloud_enabled = (
        is_cloud_runner_control_enabled()
        if cloud_runner_control_enabled is None
        else bool(cloud_runner_control_enabled)
    )
    if not cloud_enabled:
        raise ManagedRunnerProviderUnavailableError(
            "Managed runner placement requires `ENABLE_CLOUD_RUNNER_CONTROL=true`."
        )


def build_runner_runtime_provider(
    *,
    cloud_runner_control_enabled: bool | None = None,
    cloud_factory: ProviderFactory | None = None,
) -> TaskExecutionRuntimeProvider:
    """Build the managed runner provider for `runtime_placement_mode=runner`."""
    validate_managed_runner_control_enabled(
        cloud_runner_control_enabled=cloud_runner_control_enabled,
    )

    cloud_provider_factory = cloud_factory or CloudRunnerRuntimeProvider
    return cloud_provider_factory()


__all__ = [
    "ManagedRunnerProviderUnavailableError",
    "build_runner_runtime_provider",
    "validate_managed_runner_control_enabled",
]
