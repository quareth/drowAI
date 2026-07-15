"""
Runtime path and mount-policy configuration for unified Docker decomposition.

Scope:
- Resolve canonical runtime mode and diagnostic metadata.
- Build startup bundles, VPN paths, workspace bootstrap commands, and bind mounts.

Boundary:
- Contains zero Docker SDK calls or container lifecycle orchestration.
- Stateless utility class with deterministic behavior from inputs/env only.
"""

import logging
from typing import Dict, List, Tuple

from runtime_shared.docker_contracts import (
    CONTAINER_CONTROL_PATH,
    CONTAINER_VPN_CONFIG_PATH,
    CONTAINER_WORKSPACE_PATH,
    IMAGE_INTERNAL_PYTHON_ROOT,
    IMAGE_INTERNAL_VPN_SCRIPT_PATH,
    RUNTIME_PATH_MODE_IMAGE_INTERNAL,
    RUNTIME_PATH_SOURCE_IMAGE_INTERNAL,
    WORKSPACE_CONTROL_MOUNT_POLICY,
    build_container_volumes as shared_build_container_volumes,
    build_runtime_startup_command,
    build_workspace_bootstrap_commands,
)

logger = logging.getLogger(__name__)


class RuntimeConfig:
    """Stateless runtime/mount-policy helpers extracted from UnifiedDockerService."""

    def resolve_runtime_path_mode_with_activation(self) -> Tuple[str, str]:
        """Resolve canonical runtime path mode plus deterministic activation reason."""
        return RUNTIME_PATH_MODE_IMAGE_INTERNAL, "fixed_image_internal"

    def resolve_runtime_path_mode(self) -> str:
        """
        Resolve canonical runtime path mode for startup and VPN path builders.

        Image-internal is the only active runtime path mode.
        """
        mount_policy = WORKSPACE_CONTROL_MOUNT_POLICY
        mode, activation = self.resolve_runtime_path_mode_with_activation()
        mode, _ = self.enforce_runtime_mode_for_mount_policy(mode, activation, mount_policy)
        return mode

    def enforce_runtime_mode_for_mount_policy(
        self,
        runtime_path_mode: str,
        activation_reason: str,
        mount_policy: str,
    ) -> Tuple[str, str]:
        """Enforce runtime path compatibility constraints implied by the active mount policy."""
        if runtime_path_mode != RUNTIME_PATH_MODE_IMAGE_INTERNAL:
            logger.warning(
                "Unexpected runtime path mode=%s under mount_policy=%s; forcing image-internal",
                runtime_path_mode,
                mount_policy,
            )
            return RUNTIME_PATH_MODE_IMAGE_INTERNAL, "runtime_mode_image_internal_enforced"
        return runtime_path_mode, activation_reason

    def get_runtime_path_diagnostic_fields(self, mount_policy: str | None = None) -> Dict[str, str]:
        """Snapshot of effective path mode for operator logs (startup, VPN retry, recovery)."""
        if mount_policy is None:
            mount_policy = WORKSPACE_CONTROL_MOUNT_POLICY
        mode, activation = self.resolve_runtime_path_mode_with_activation()
        mode, activation = self.enforce_runtime_mode_for_mount_policy(mode, activation, mount_policy)
        path_source = self.resolve_runtime_path_source(mode)
        vpn_script = self.resolve_vpn_script_path(mode)
        startup_workspace_init = f"{IMAGE_INTERNAL_PYTHON_ROOT}/workspace_init.py"
        startup_executor = f"{IMAGE_INTERNAL_PYTHON_ROOT}/executor_daemon.py"
        return {
            "effective_mode": mode,
            "path_source": path_source,
            "activation_reason": activation,
            "vpn_script_path": vpn_script,
            "startup_workspace_init": startup_workspace_init,
            "startup_executor": startup_executor,
            "mount_policy": mount_policy,
        }

    def resolve_runtime_path_source(self, runtime_path_mode: str) -> str:
        """Return deterministic runtime path source label for diagnostics."""
        return RUNTIME_PATH_SOURCE_IMAGE_INTERNAL

    def resolve_vpn_script_path(self, runtime_path_mode: str) -> str:
        """Resolve VPN manager script path for selected runtime path mode."""
        return IMAGE_INTERNAL_VPN_SCRIPT_PATH

    def get_vpn_script_path_for_current_mode(self) -> str:
        """Return VPN script path from the canonical runtime path mode resolver."""
        return self.resolve_vpn_script_path(self.resolve_runtime_path_mode())

    def build_vpn_connect_exec_shell(self, task_id: int, *, reconnect: bool = False) -> str:
        """
        Shell fragment for a best-effort VPN `connect` inside the task container.

        Startup/retry/recovery all use the same image-internal VPN script path.
        """
        vpn_script_path = self.get_vpn_script_path_for_current_mode()
        action = "reconnect" if reconnect else "connect"
        return f"VPN_CONFIG={CONTAINER_VPN_CONFIG_PATH} bash {vpn_script_path} {action}"

    def startup_workdir_for_mode(self, runtime_path_mode: str) -> str:
        """Working directory inside the container for the selected runtime path mode."""
        return IMAGE_INTERNAL_PYTHON_ROOT

    def build_startup_runtime_bundle(
        self,
        runtime_path_source: str,
        vpn_script_path: str,
        activation_reason: str,
    ) -> Tuple[str, str, str]:
        """
        Build PYTHONPATH, container working directory, and shell startup command chain.

        Startup always uses in-image entrypoints and the resolver VPN script path.
        """
        pythonpath = IMAGE_INTERNAL_PYTHON_ROOT
        workdir = IMAGE_INTERNAL_PYTHON_ROOT
        startup_command = build_runtime_startup_command(
            runtime_path_source=runtime_path_source,
            activation_reason=activation_reason,
            vpn_script_path=vpn_script_path,
        )
        return pythonpath, workdir, startup_command

    def workspace_bootstrap_commands(self, task_id: int) -> List[str]:
        """Shell commands to create task workspace symlink inside the runtime tree (post-start)."""
        return build_workspace_bootstrap_commands(task_id)

    def workspace_bootstrap_commands_for_policy(self, task_id: int, mount_policy: str) -> List[str]:
        """Build workspace bootstrap commands for the active mount policy."""
        _ = mount_policy
        return self.workspace_bootstrap_commands(task_id)

    def build_container_volumes(
        self,
        task_id: int,
        workspace_mount_source: str,
        control_mount_source: str,
        mount_policy: str,
    ) -> Dict[str, Dict[str, str]]:
        """Build container bind mounts from one canonical mount-policy decision path."""
        _ = task_id  # compatibility parameter retained for existing caller signatures.
        return shared_build_container_volumes(
            workspace_mount_source=workspace_mount_source,
            control_mount_source=control_mount_source,
            mount_policy=mount_policy,
        )

    def validate_mount_contract(
        self,
        volumes: Dict[str, Dict[str, str]],
        mount_policy: str,
        task_id: int,
    ) -> None:
        """Validate effective bind-mount contract for the selected mount policy."""
        if mount_policy != WORKSPACE_CONTROL_MOUNT_POLICY:
            raise RuntimeError(
                f"Unsupported mount policy for task={task_id}: {mount_policy}"
            )
        if len(volumes) != 2:
            raise RuntimeError(
                f"Invalid workspace-control mount contract for task={task_id}: expected 2 binds, got {len(volumes)}"
            )
        mounts = {(item.get("bind"), item.get("mode")) for item in volumes.values()}
        expected = {
            (CONTAINER_WORKSPACE_PATH, "rw"),
            (CONTAINER_CONTROL_PATH, "ro"),
        }
        if mounts != expected:
            raise RuntimeError(
                f"Invalid workspace-control mount contract for task={task_id}: {volumes}"
            )
