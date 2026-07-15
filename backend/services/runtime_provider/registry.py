"""Runtime provider registry and selection helpers.

Responsibilities:
- Resolve task runtime placement to a concrete task execution runtime provider.
- Default to local Docker runtime behavior for existing deployments.
- Fail closed for unsupported or unknown placement modes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from backend.config.feature_flags import get_default_task_runtime_placement_mode

from .contracts import RuntimePlacementMode
from .provider import TaskExecutionRuntimeProvider

ProviderFactory = Callable[[], TaskExecutionRuntimeProvider]


class UnsupportedRuntimePlacementError(ValueError):
    """Raised when runtime placement mode cannot be resolved to a provider."""


def resolve_task_runtime_placement_mode(
    task: Any,
    *,
    default_mode: str | RuntimePlacementMode | None = None,
) -> RuntimePlacementMode:
    """Resolve placement mode from task metadata with a local default."""
    mode_candidate = getattr(task, "runtime_placement_mode", None)
    if mode_candidate is None:
        mode_candidate = default_mode or get_default_task_runtime_placement_mode()
    return _normalize_placement_mode(mode_candidate)


class RuntimeProviderRegistry:
    """Registry for selecting task runtime providers by placement mode."""

    def __init__(
        self,
        *,
        default_mode: str | RuntimePlacementMode | None = None,
        local_provider_factory: ProviderFactory | None = None,
        runner_provider_factory: ProviderFactory | None = None,
        provider_overrides: Mapping[str | RuntimePlacementMode, TaskExecutionRuntimeProvider]
        | None = None,
    ) -> None:
        self._default_mode = _normalize_placement_mode(
            default_mode or get_default_task_runtime_placement_mode()
        )
        self._local_provider_factory = local_provider_factory or _build_local_provider
        self._runner_provider_factory = runner_provider_factory or _build_runner_provider
        self._providers: dict[RuntimePlacementMode, TaskExecutionRuntimeProvider] = {}

        for mode, provider in (provider_overrides or {}).items():
            self._providers[_normalize_placement_mode(mode)] = provider

    def get_provider(
        self,
        *,
        runtime_placement_mode: str | RuntimePlacementMode | None = None,
    ) -> TaskExecutionRuntimeProvider:
        """Return provider for placement mode or the configured default."""
        mode = self._default_mode
        if runtime_placement_mode is not None:
            mode = _normalize_placement_mode(runtime_placement_mode)

        cached_provider = self._providers.get(mode)
        if cached_provider is not None:
            return cached_provider

        if mode is RuntimePlacementMode.LOCAL:
            local_provider = self._local_provider_factory()
            self._providers[mode] = local_provider
            return local_provider
        if mode is RuntimePlacementMode.RUNNER:
            runner_provider = self._runner_provider_factory()
            self._providers[mode] = runner_provider
            return runner_provider

        raise UnsupportedRuntimePlacementError(
            f"Unsupported task runtime placement mode: `{mode.value}`."
        )

    def get_provider_for_task(self, task: Any) -> TaskExecutionRuntimeProvider:
        """Resolve task placement metadata and return its runtime provider."""
        mode = resolve_task_runtime_placement_mode(task, default_mode=self._default_mode)
        return self.get_provider(runtime_placement_mode=mode)


def _normalize_placement_mode(
    mode: str | RuntimePlacementMode,
) -> RuntimePlacementMode:
    """Normalize incoming placement mode and fail closed on unknown values."""
    if isinstance(mode, RuntimePlacementMode):
        return mode

    normalized = str(mode or "").strip().lower()
    if not normalized:
        raise UnsupportedRuntimePlacementError("Runtime placement mode must not be empty.")

    try:
        return RuntimePlacementMode(normalized)
    except ValueError as exc:
        raise UnsupportedRuntimePlacementError(
            f"Unsupported task runtime placement mode: `{normalized}`."
        ) from exc


def _build_local_provider() -> TaskExecutionRuntimeProvider:
    """Build local Docker provider lazily to keep test injection lightweight."""
    from .local_docker_provider import LocalDockerRuntimeProvider

    return LocalDockerRuntimeProvider()


def _build_runner_provider() -> TaskExecutionRuntimeProvider:
    """Build runner provider lazily via the managed-runner provider factory."""
    from .runner_provider_selection import build_runner_runtime_provider

    return build_runner_runtime_provider()


__all__ = [
    "RuntimeProviderRegistry",
    "UnsupportedRuntimePlacementError",
    "resolve_task_runtime_placement_mode",
]
