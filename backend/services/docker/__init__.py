"""Facade for the decomposed Docker service package.

Scope:
- Compose internal modules (client, runtime_config, container_config,
  lifecycle, logs, exec, metrics) into a single UnifiedDockerService class.
- Re-export the UnifiedDockerService class and unified_docker_service singleton.

Boundary:
- Contains NO business logic. Delegates every public method call to the
  appropriate internal module.
- External callers import from here (or from the backward-compat shim).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..container_utils import get_workspace_path
from .client import DockerClient
from .container_config import ContainerConfigBuilder
from .exec import ContainerExec
from .lifecycle import ContainerLifecycle, save_environment_info
from .logs import ContainerLogs
from .metrics import ContainerMetrics
from .operations import ContainerOperations
from .runtime_config import RuntimeConfig


class UnifiedDockerService:
    """Facade: delegates to decomposed Docker service modules.

    Preserves identical public API for backward compatibility.
    """
    _LIFECYCLE_PATCHABLE_METHODS = (
        "_prepare_container_config",
        "_validate_workspace_ready",
        "_simulate_container_creation",
        "_ensure_image_available",
        "_ensure_task_network",
        "_create_container_sdk",
        "_create_container_cli",
        "_start_container",
        "_initialize_container_environment",
        "_ensure_vpn_ready",
        "_collect_and_save_environment_info",
    )
    _LOGS_PATCHABLE_METHODS = {
        "_check_container_exists": "check_container_exists",
        "_check_image_exists": "check_image_exists",
        "_check_active_image_pull": "check_active_image_pull",
    }

    def __init__(
        self,
        *,
        workspace_path_resolver: Optional[Callable[..., Any]] = None,
        save_environment_info_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        resolved_workspace_path_resolver = (
            workspace_path_resolver
            if workspace_path_resolver is not None
            else get_workspace_path
        )
        resolved_save_environment_info_fn = (
            save_environment_info_fn
            if save_environment_info_fn is not None
            else save_environment_info
        )

        self._client = DockerClient()
        self._runtime_config = RuntimeConfig()
        self._config_builder = ContainerConfigBuilder(
            self._runtime_config,
            workspace_path_resolver=resolved_workspace_path_resolver,
        )
        self._lifecycle = ContainerLifecycle(
            self._client,
            self._config_builder,
            self._runtime_config,
            save_environment_info_fn=resolved_save_environment_info_fn,
            check_image_exists_fn=None,
        )
        self._logs = ContainerLogs(self._client)
        self._lifecycle._check_image_exists_fn = self._logs.check_image_exists
        self._operations = ContainerOperations(self._client)
        self._exec = ContainerExec(self._client)
        self._metrics = ContainerMetrics(self._client)

    @property
    def client(self):
        return self._client.client

    @client.setter
    def client(self, value):
        self._client.client = value

    @property
    def docker_available(self):
        return self._client.docker_available

    @docker_available.setter
    def docker_available(self, value):
        self._client.docker_available = value

    @property
    def api_mode(self):
        return self._client.api_mode

    @api_mode.setter
    def api_mode(self, value):
        self._client.api_mode = value

    @property
    def image_name(self):
        return self._client.image_name

    @image_name.setter
    def image_name(self, value):
        self._client.image_name = value

    @property
    def containers(self):
        return self._client.containers

    @containers.setter
    def containers(self, value):
        self._client.containers = value

    def _sync_lifecycle_delegates(self) -> None:
        """Sync mutable facade state and patch seams into lifecycle delegate."""
        self._lifecycle.client = self.client
        self._lifecycle.docker_available = self.docker_available
        self._lifecycle.api_mode = self.api_mode
        self._lifecycle.image_name = self.image_name
        self._lifecycle.containers = self.containers
        self._sync_logs_delegates()
        self._lifecycle._check_image_exists_fn = self._logs.check_image_exists

        lifecycle_cls = type(self._lifecycle)
        for method_name in self._LIFECYCLE_PATCHABLE_METHODS:
            patched = self.__dict__.get(method_name)
            if patched is not None:
                setattr(self._lifecycle, method_name, patched)
            else:
                setattr(
                    self._lifecycle,
                    method_name,
                    getattr(lifecycle_cls, method_name).__get__(self._lifecycle, lifecycle_cls),
                )

    def _sync_logs_delegates(self) -> None:
        """Sync mutable facade state and patch seams into logs delegate."""
        self._logs.client = self.client
        self._logs.docker_available = self.docker_available
        self._logs.api_mode = self.api_mode
        self._logs.image_name = self.image_name
        self._logs.containers = self.containers

        logs_cls = type(self._logs)
        for legacy_name, delegate_name in self._LOGS_PATCHABLE_METHODS.items():
            patched = self.__dict__.get(legacy_name)
            if patched is not None:
                setattr(self._logs, delegate_name, patched)
            else:
                setattr(
                    self._logs,
                    delegate_name,
                    getattr(logs_cls, delegate_name).__get__(self._logs, logs_cls),
                )

    def _sync_exec_delegates(self) -> None:
        """Sync mutable facade state into exec delegate."""
        self._exec.client = self.client
        self._exec.docker_available = self.docker_available
        self._exec.api_mode = self.api_mode
        self._exec.image_name = self.image_name
        self._exec.containers = self.containers

    def _sync_operations_delegates(self) -> None:
        """Sync mutable facade state into operations delegate."""
        self._operations.client = self.client
        self._operations.docker_available = self.docker_available
        self._operations.api_mode = self.api_mode
        self._operations.image_name = self.image_name
        self._operations.containers = self.containers

    def _sync_metrics_delegates(self) -> None:
        """Sync mutable facade state into metrics delegate."""
        self._metrics.client = self.client
        self._metrics.docker_available = self.docker_available
        self._metrics.api_mode = self.api_mode
        self._metrics.image_name = self.image_name
        self._metrics.containers = self.containers

    def get_runtime_path_diagnostic_fields(self, mount_policy=None):
        return self._runtime_config.get_runtime_path_diagnostic_fields(mount_policy)

    def get_vpn_script_path_for_current_mode(self):
        return self._runtime_config.get_vpn_script_path_for_current_mode()

    def build_vpn_connect_exec_shell(self, task_id, *, reconnect=False):
        return self._runtime_config.build_vpn_connect_exec_shell(task_id, reconnect=reconnect)

    async def create_and_start_container(
        self, task_id, target="127.0.0.1", user_id=None, tenant_id="local"
    ):
        self._sync_lifecycle_delegates()
        return await self._lifecycle.create_and_start_container(
            task_id, target, user_id, tenant_id
        )

    async def get_container_status(self, task_id):
        self._sync_operations_delegates()
        return await self._operations.get_container_status(task_id)

    async def stop_container(self, task_id):
        self._sync_operations_delegates()
        return await self._operations.stop_container(task_id)

    async def pause_container(self, task_id):
        self._sync_operations_delegates()
        return await self._operations.pause_container(task_id)

    async def unpause_container(self, task_id):
        self._sync_operations_delegates()
        return await self._operations.unpause_container(task_id)

    async def send_signal(self, task_id, signal_name):
        self._sync_operations_delegates()
        return await self._operations.send_signal(task_id, signal_name)

    async def remove_container(self, task_id, force=False):
        self._sync_operations_delegates()
        return await self._operations.remove_container(task_id, force)

    async def pull_image(self, task_id):
        self._sync_lifecycle_delegates()
        return await self._lifecycle.pull_image(task_id)

    async def create_container(self, task_id, target="127.0.0.1", **kwargs):
        self._sync_lifecycle_delegates()
        return await self._lifecycle.create_container(task_id, target, **kwargs)

    def is_docker_available(self):
        return self._client.docker_available

    async def get_all_containers(self):
        self._sync_operations_delegates()
        return await self._operations.get_all_containers()

    async def cleanup_containers(self, force=False):
        self._sync_operations_delegates()
        return await self._operations.cleanup_containers(force)

    async def get_container_logs(self, task_id, lines=50):
        self._sync_logs_delegates()
        return await self._logs.get_container_logs(task_id, lines)

    async def get_container_startup_progress(self, task_id):
        self._sync_logs_delegates()
        return await self._logs.get_container_startup_progress(task_id)

    async def execute_container_command(self, task_id, command):
        self._sync_exec_delegates()
        return await self._exec.execute_container_command(task_id, command)

    async def broadcast_log_to_websocket(self, task_id, log_entry):
        self._sync_exec_delegates()
        return await self._exec.broadcast_log_to_websocket(task_id, log_entry)

    def get_container_name_by_id(self, task_id):
        self._sync_exec_delegates()
        return self._exec.get_container_name_by_id(task_id)

    async def start_persistent_pty(self, task_id, shell="/bin/bash", cols=80, rows=24):
        self._sync_exec_delegates()
        return await self._exec.start_persistent_pty(task_id, shell, cols, rows)

    async def get_container_metrics(self, task_id):
        self._sync_metrics_delegates()
        return await self._metrics.get_container_metrics(task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    def _prepare_container_config(self, task_id, target="127.0.0.1", user_id=None):
        self._sync_lifecycle_delegates()
        return self._lifecycle._prepare_container_config(task_id, target, user_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    def _validate_workspace_ready(self, task_id):
        self._sync_lifecycle_delegates()
        return self._lifecycle._validate_workspace_ready(task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _simulate_container_creation(self, task_id, logs):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._simulate_container_creation(task_id, logs)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _ensure_image_available(self):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._ensure_image_available()

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _create_container_sdk(self, config):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._create_container_sdk(config)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _create_container_cli(self, config):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._create_container_cli(config)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _start_container(self, container, task_id):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._start_container(container, task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _initialize_container_environment(self, container, task_id):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._initialize_container_environment(container, task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    def _workspace_bootstrap_commands_for_policy(self, task_id, mount_policy):
        return self._runtime_config.workspace_bootstrap_commands_for_policy(
            task_id, mount_policy
        )

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _check_container_exists(self, task_id):
        self._sync_logs_delegates()
        return await self._logs.check_container_exists(task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _check_image_exists(self):
        self._sync_logs_delegates()
        return await self._logs.check_image_exists()

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _check_active_image_pull(self):
        self._sync_logs_delegates()
        return await self._logs.check_active_image_pull()

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _ensure_vpn_ready(self, container, task_id):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._ensure_vpn_ready(container, task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    async def _collect_and_save_environment_info(self, container, task_id):
        self._sync_lifecycle_delegates()
        return await self._lifecycle._collect_and_save_environment_info(container, task_id)

    # Test-compat only: retained to preserve existing direct-instance test patch targets.
    def _ensure_image_available_sdk(self, image_name):
        self._sync_lifecycle_delegates()
        return self._lifecycle._ensure_image_available_sdk(image_name)

    def _configure_test_compat_hooks(
        self,
        *,
        workspace_path_resolver: Callable[..., Any],
        save_environment_info_fn: Callable[..., Any],
    ) -> None:
        """Compat-only seam used by the shim to preserve legacy patch targets."""
        self._config_builder._workspace_path_resolver = workspace_path_resolver
        self._lifecycle._save_environment_info = save_environment_info_fn


unified_docker_service = UnifiedDockerService()

__all__ = ["UnifiedDockerService", "unified_docker_service"]
