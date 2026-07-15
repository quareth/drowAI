"""Backward-compatibility shim.

All Docker service logic is decomposed under ``backend/services/docker``.
This module preserves legacy import paths and module-level patch targets used
across tests and callers.
"""

from __future__ import annotations

# Public package facade (canonical implementation).
from .docker import UnifiedDockerService as _FacadeUnifiedDockerService

# Module-level symbols that tests patch by import path.
import docker  # noqa: F401
from .docker.container_config import get_workspace_path  # noqa: F401
from .docker.lifecycle import save_environment_info  # noqa: F401


def _compat_get_workspace_path(task_id: int):
    """Resolve workspace path via shim module global for monkeypatch compatibility."""
    return get_workspace_path(task_id)


def _compat_save_environment_info(task_id: int, env_info):
    """Resolve env save function via shim module global for monkeypatch compatibility."""
    return save_environment_info(task_id, env_info)


class UnifiedDockerService(_FacadeUnifiedDockerService):
    """Compat constructor that injects shim-level dynamic hooks into every instance."""

    def __init__(self):
        super().__init__(
            workspace_path_resolver=_compat_get_workspace_path,
            save_environment_info_fn=_compat_save_environment_info,
        )


unified_docker_service = UnifiedDockerService()

__all__ = ["UnifiedDockerService", "unified_docker_service"]
