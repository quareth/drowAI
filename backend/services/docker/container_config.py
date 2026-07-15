"""
Container configuration builder for unified Docker decomposition.

Scope:
- Build container creation payloads and environment variables.
- Resolve workspace readiness checks used before container startup.

Boundary:
- Uses RuntimeConfig for runtime-path and mount-policy decisions.
- Contains no Docker SDK create/start lifecycle orchestration.
"""

import logging
import os
import re
from typing import Any, Callable, Dict, Optional

from runtime_shared.docker_contracts import (
    CONTAINER_RUNTIME_INPUT_PATH,
    CONTAINER_VPN_CONFIG_PATH,
    WORKSPACE_CONTROL_MOUNT_POLICY,
    build_fail_closed_runtime_command,
    build_runtime_contract_environment,
)
from runtime_shared.runtime_network import build_runtime_network_name

from ...config.workspace_config import WorkspaceConfig
from ..container_utils import (
    get_container_name,
    get_workspace_path,
)
from .runtime_config import RuntimeConfig

# Keep logger name aligned with monolith for caplog/patch compatibility.
logger = logging.getLogger("backend.services.unified_docker_service")
_E2E_SUITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _provider_key_status(provider_environment: Dict[str, str]) -> str:
    """Return a masked provider-aware key status for container logs."""
    provider = str(provider_environment.get("LLM_PROVIDER") or "").strip().upper()
    if not provider:
        return "<NO_PROVIDER>"
    key_name = f"{provider}_API_KEY"
    if key_name in provider_environment:
        return "<KEY_SET>" if provider_environment.get(key_name) else "<NO_KEY>"
    return "<BACKEND_ONLY>"


def _workspace_mount_log_fields(workspace_path: str) -> Dict[str, str]:
    """Build sanitized mount diagnostics without leaking absolute host paths."""
    workspace_basename = os.path.basename(workspace_path.rstrip("/")) or "<unknown-workspace>"
    return {
        "workspace_mount_id": workspace_basename,
        "workspace_mount_source_type": "host-local",
    }


class ContainerConfigBuilder:
    """Builds and validates container configuration payloads."""

    def __init__(
        self,
        runtime_config: RuntimeConfig,
        workspace_path_resolver=get_workspace_path,
        provider_environment_builder: Optional[Callable[[int, Optional[int]], Dict[str, str]]] = None,
    ):
        self.runtime_config = runtime_config
        self._workspace_path_resolver = workspace_path_resolver
        self._provider_environment_builder = provider_environment_builder or self._build_provider_environment

    def prepare_container_config(
        self,
        task_id: int,
        target: str = "127.0.0.1",
        user_id: Optional[int] = None,
        image_name: Optional[str] = None,
        tenant_id: str | int = "local",
    ) -> Dict[str, Any]:
        """Prepare standardized container configuration."""
        container_name = get_container_name(task_id)
        workspace_path = self._workspace_path_resolver(task_id)
        mount_policy = WORKSPACE_CONTROL_MOUNT_POLICY
        diag = self.runtime_config.get_runtime_path_diagnostic_fields(mount_policy=mount_policy)
        runtime_path_source = diag["path_source"]
        vpn_script_path = diag["vpn_script_path"]
        activation_reason = diag["activation_reason"]

        startup_pythonpath, startup_workdir, startup_command = self.runtime_config.build_startup_runtime_bundle(
            runtime_path_source,
            vpn_script_path,
            activation_reason,
        )
        logger.info(
            "[runtime-path-mode] task_id=%s context=container_prepare effective_mode=%s path_source=%s "
            "activation=%s mount_policy=%s startup_workspace_init=%s startup_executor=%s vpn_script=%s",
            task_id,
            diag["effective_mode"],
            diag["path_source"],
            diag["activation_reason"],
            diag["mount_policy"],
            diag["startup_workspace_init"],
            diag["startup_executor"],
            diag["vpn_script_path"],
        )

        mount_log_fields = _workspace_mount_log_fields(workspace_path)
        logger.info(
            "[mount-policy] task_id=%s effective_policy=%s workspace_mount_id=%s workspace_source_type=%s",
            task_id,
            mount_policy,
            mount_log_fields["workspace_mount_id"],
            mount_log_fields["workspace_mount_source_type"],
        )

        backend_host = "host.docker.internal"
        logger.info("Using Docker host-gateway alias for container communication")

        # Environment variables
        environment = {
            "TASK_ID": str(task_id),
            "WORKSPACE": "/workspace",
            "TARGET": target,
            "PYTHONPATH": startup_pythonpath,
            "USER_ID": str(user_id) if user_id else "",
            "BACKEND_HOST": backend_host,
            "DROWAI_RUNTIME_PATH_SOURCE": runtime_path_source,
            "DROWAI_MOUNT_POLICY": mount_policy,
            "DROWAI_RUNTIME_INPUT_PATH": CONTAINER_RUNTIME_INPUT_PATH,
        }
        environment["DROWAI_RUNTIME_NETWORK"] = build_runtime_network_name(container_name)
        environment.update(build_runtime_contract_environment())

        # Add provider runtime environment if user_id is provided.
        if user_id:
            try:
                provider_environment = self._provider_environment_builder(user_id, task_id)
                environment.update(provider_environment)
                key_status = _provider_key_status(provider_environment)
                logger.info(
                    "Added LLM provider environment for user %s task_id=%s provider=%s model=%s key_status=%s",
                    user_id,
                    task_id,
                    provider_environment.get("LLM_PROVIDER", "<unknown>"),
                    provider_environment.get("LLM_MODEL", "<unknown>"),
                    key_status,
                )
            except Exception as exc:
                logger.warning(
                    "No valid LLM provider environment for user %s task_id=%s: %s",
                    user_id,
                    task_id,
                    exc,
                )
                logger.warning("User needs to configure provider credentials before running containers")
        else:
            logger.warning("No user_id provided for container creation - LLM provider credentials will not be available")

        volumes = self.runtime_config.build_container_volumes(
            task_id=task_id,
            workspace_mount_source=workspace_path,
            control_mount_source=str(WorkspaceConfig.get_task_control_path(task_id)),
            mount_policy=mount_policy,
        )
        self.runtime_config.validate_mount_contract(volumes, mount_policy, task_id)

        extra_hosts = {"host.docker.internal": "host-gateway"}

        labels = {
            "drowai.task_id": str(task_id),
            "drowai.type": "kali-pentesting",
        }
        e2e_suite_id = os.getenv("E2E_RUNTIME_SUITE_ID", "").strip()
        if _E2E_SUITE_ID_PATTERN.fullmatch(e2e_suite_id):
            labels["drowai.e2e_suite_id"] = e2e_suite_id

        config = {
            "name": container_name,
            "image": image_name,
            "environment": environment,
            "volumes": volumes,
            "network": build_runtime_network_name(container_name),
            "runtime_network_tenant_id": str(tenant_id),
            "extra_hosts": extra_hosts,
            "detach": True,
            "tty": True,
            "stdin_open": True,
            # Capabilities and device for OpenVPN
            "cap_add": ["NET_ADMIN"],
            "devices": ["/dev/net/tun:/dev/net/tun"],
            "privileged": False,
            "security_opt": [],
            # Start the executor daemon only after validating the runtime contract.
            "command": [
                "/bin/bash",
                "-lc",
                # Runtime path mode is image-internal only.
                build_fail_closed_runtime_command(startup_command),
            ],
            "working_dir": startup_workdir,
            # Resource limits to prevent excessive resource consumption
            "mem_limit": "2g",  # 2GB memory limit
            "memswap_limit": "2g",  # Disable swap usage
            "cpu_period": 100000,  # CPU period in microseconds (100ms)
            "cpu_quota": 150000,  # CPU quota (150% of one core = 1.5 cores max)
            "shm_size": "256m",  # Shared memory size for tools that need it
            "ulimits": [
                {"name": "nofile", "soft": 65536, "hard": 65536},  # File descriptor limit
                {"name": "nproc", "soft": 4096, "hard": 4096},  # Process limit
            ],
            "labels": labels,
            "user": "root",
        }

        # If task has VPN configured, attach env hint for scripts
        try:
            from backend.database import SessionLocal
            from backend.models.core import Task as TaskModel

            db = SessionLocal()
            task = db.query(TaskModel).filter(TaskModel.id == task_id).first()
            if task and task.vpn_enabled:
                config["environment"]["VPN_ENABLED"] = "true"
                # Point to expected ovpn path inside workspace mount
                config["environment"]["VPN_CONFIG"] = CONTAINER_VPN_CONFIG_PATH
        except Exception as e:
            logger.debug(f"Could not annotate VPN env for task {task_id}: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass

        return config

    @staticmethod
    def _build_provider_environment(user_id: int, task_id: Optional[int]) -> Dict[str, str]:
        """Build provider runtime environment through the LLM provider service."""
        from ...database import get_db
        from ..llm_provider import LLMProviderEnvironmentService

        db = None
        try:
            db = next(get_db())
            return LLMProviderEnvironmentService(db).build_environment(
                user_id=user_id,
                task_id=task_id,
            )
        finally:
            if db is not None:
                db.close()

    def validate_workspace_ready(self, task_id: int) -> tuple[bool, str]:
        """Validate workspace is ready for container creation."""
        workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)

        # Check workspace exists
        if not workspace_path.exists():
            return False, f"Workspace directory does not exist: {workspace_path}"

        # Check workspace is readable
        if not os.access(workspace_path, os.R_OK):
            return False, f"Workspace directory not readable: {workspace_path}"

        # Check scope file exists and is readable
        scope_file = workspace_path / "scope.md"
        if not scope_file.exists():
            return False, f"Scope file missing: {scope_file}"

        if not os.access(scope_file, os.R_OK):
            return False, f"Scope file not readable: {scope_file}"

        # Check scope file has content
        try:
            if scope_file.stat().st_size == 0:
                return False, "Scope file is empty"
        except OSError as e:
            return False, f"Cannot check scope file size: {e}"

        # Only validate essential files exist, don't require standard subdirectories
        # This prevents creating empty dirs that mask actual task content
        return True, f"Workspace validation successful: scope.md found with {scope_file.stat().st_size} bytes"
