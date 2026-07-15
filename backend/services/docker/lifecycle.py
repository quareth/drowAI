"""
Container lifecycle orchestration and image management.

Scope:
- Orchestrate full container creation flow: validate, configure, create, start,
  bootstrap environment, collect env info, attempt VPN recovery.
- Manage Docker image availability, including pull progress reporting.
- Preserve simulation fallback when Docker is unavailable.
- Keep legacy compatibility wrappers (`pull_image`, `create_container`, etc.).

Boundary:
- Does NOT build container configuration (delegates to container_config.py).
- Does NOT collect metrics (that is metrics.py).
- Does NOT retrieve logs/startup progress (that is logs.py).
- Does NOT execute ad-hoc commands or PTY sessions (that is exec.py).
"""

import json
import logging
import subprocess
import os
from ipaddress import IPv4Network, ip_network
from typing import Any, Dict, List, Optional

from backend.core.time_utils import format_iso, utc_now
from runtime_shared.docker_contracts import WORKSPACE_CONTROL_MOUNT_POLICY
from runtime_shared.runtime_image_contract import is_digest_pinned_runtime_image
from runtime_shared.runtime_manifest import build_runtime_manifest
from runtime_shared.docker_network_manager import DockerTaskNetworkManager
from runtime_shared.runtime_network import (
    RUNTIME_NETWORK_DRIVER,
    RUNTIME_NETWORK_OPTIONS,
    RuntimeNetworkError,
    build_runtime_network_spec,
    iter_runtime_subnets,
    network_labels,
    parse_runtime_network_pool,
    validate_managed_network,
)

from ...config.workspace_config import WorkspaceConfig
from ..container_utils import get_container_name
from ..workspace.environment_collector import collect_environment_info, save_environment_info
from .client import DockerClient
from .container_config import ContainerConfigBuilder
from .runtime_config import RuntimeConfig

# Keep logger name aligned with monolith for caplog/patch compatibility.
logger = logging.getLogger("backend.services.unified_docker_service")


class ContainerLifecycle:
    """Container lifecycle orchestration extracted from UnifiedDockerService."""

    def __init__(
        self,
        client: DockerClient,
        config_builder: ContainerConfigBuilder,
        runtime_config: RuntimeConfig,
        save_environment_info_fn=save_environment_info,
        check_image_exists_fn=None,
    ):
        self._client = client
        self._config_builder = config_builder
        self._runtime_config = runtime_config
        self._save_environment_info = save_environment_info_fn
        self._check_image_exists_fn = check_image_exists_fn

        # Mirror monolith state shape for method-level behavioral parity.
        self.client = client.client
        self.docker_available = client.docker_available
        self.api_mode = client.api_mode
        self.image_name = client.image_name
        self.containers = client.containers

    def _prepare_container_config(
        self,
        task_id: int,
        target: str = "127.0.0.1",
        user_id: Optional[int] = None,
        tenant_id: str | int = "local",
    ) -> Dict[str, Any]:
        """Delegate container payload assembly to container_config builder."""
        return self._config_builder.prepare_container_config(
            task_id=task_id,
            target=target,
            user_id=user_id,
            image_name=self.image_name,
            tenant_id=tenant_id,
        )

    def _validate_workspace_ready(self, task_id: int) -> tuple[bool, str]:
        """Delegate workspace validation to container_config builder."""
        return self._config_builder.validate_workspace_ready(task_id)

    async def create_and_start_container(
        self,
        task_id: int,
        target: str = "127.0.0.1",
        user_id: Optional[int] = None,
        tenant_id: str | int = "local",
    ) -> Dict[str, Any]:
        """
        Unified container creation and startup method.
        Replaces all scattered create_container methods.
        """
        logs = []

        # Step 1: Validate workspace is ready
        workspace_valid, validation_message = self._validate_workspace_ready(task_id)
        if not workspace_valid:
            error_msg = f"Workspace validation failed: {validation_message}"
            logger.error(error_msg)
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "level": "ERROR",
                    "message": error_msg,
                }
            )
            return {
                "success": False,
                "container_id": None,
                "logs": logs,
                "error": error_msg,
            }

        logs.append(
            {
                "timestamp": format_iso(utc_now()),
                "level": "INFO",
                "message": f"Workspace validation passed: {validation_message}",
            }
        )

        config = self._prepare_container_config(task_id, target, user_id, tenant_id)
        container_name = config["name"]

        # Log operation start
        start_log = {
            "timestamp": format_iso(utc_now()),
            "service": "unified-docker",
            "level": "info",
            "message": f"Starting container creation for task {task_id}",
        }
        logs.append(start_log)

        if not self.docker_available:
            return await self._simulate_container_creation(task_id, logs)

        try:
            WorkspaceConfig.ensure_control_structure(task_id)
            WorkspaceConfig.migrate_legacy_runtime_input(task_id)
            # Step 1: Ensure image is available
            image_logs = await self._ensure_image_available()
            logs.extend(image_logs)

            network_result = self._ensure_task_network(config, task_id)
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": (
                        f"Managed task network {network_result['name']} "
                        f"{network_result['action']} ({network_result['subnet']})"
                    ),
                }
            )

            # Step 2: Create container
            if self.api_mode == "sdk":
                container = await self._create_container_sdk(config)
            else:
                container = await self._create_container_cli(config)

            # Step 3: Start container
            start_result = await self._start_container(container, task_id)
            logs.extend(start_result["logs"])

            if start_result["success"]:
                contract_ok, contract_error = self._verify_runtime_contract(container)
                if not contract_ok:
                    self._cleanup_failed_provision(
                        container,
                        config,
                        task_id,
                        remove_network=bool(network_result.get("created")),
                    )
                    return {
                        "success": False,
                        "container_id": None,
                        "logs": logs,
                        "error": contract_error,
                    }
                # Step 4: Initialize environment
                init_logs = await self._initialize_container_environment(container, task_id)
                logs.extend(init_logs)

                # Step 5: Collect and save environment info for LLM context.
                # VPN config/connect is orchestrated through the runtime provider
                # after provisioning returns, so first-image pulls cannot race it.
                env_logs = await self._collect_and_save_environment_info(container, task_id)
                logs.extend(env_logs)
                WorkspaceConfig.finalize_legacy_control_cutover(task_id)

                # Store container reference
                self.containers[task_id] = container

                success_log = {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"Container {container_name} created and started successfully",
                }
                logs.append(success_log)

                return {
                    "success": True,
                    "container_id": container.id if hasattr(container, "id") else f"cli_{task_id}",
                    "container_name": container_name,
                    "logs": logs,
                }
            else:
                self._cleanup_failed_provision(
                    container,
                    config,
                    task_id,
                    remove_network=bool(network_result.get("created")),
                )
                return {
                    "success": False,
                    "error": start_result["error"],
                    "container_id": None,
                    "logs": logs,
                }

        except Exception as e:
            if "container" in locals():
                self._cleanup_failed_provision(
                    container,
                    config,
                    task_id,
                    remove_network=bool(
                        "network_result" in locals() and network_result.get("created")
                    ),
                )
            elif "network_result" in locals() and network_result.get("created"):
                self._remove_task_network_best_effort(config, task_id)
            error_log = {
                "timestamp": format_iso(utc_now()),
                "service": "unified-docker",
                "level": "error",
                "message": f"Container creation failed: {str(e)}",
            }
            logs.append(error_log)

            return {
                "success": False,
                "error": str(e),
                "container_id": None,
                "logs": logs,
            }

    async def _simulate_container_creation(self, task_id: int, logs: List[Dict]) -> Dict[str, Any]:
        """Handle simulation mode when Docker is unavailable."""
        container_name = get_container_name(task_id)

        simulation_logs = [
            {
                "timestamp": format_iso(utc_now()),
                "service": "unified-docker-sim",
                "level": "info",
                "message": "Docker unavailable - running in simulation mode",
            },
            {
                "timestamp": format_iso(utc_now()),
                "service": "unified-docker-sim",
                "level": "info",
                "message": f"Simulating container {container_name} creation",
            },
            {
                "timestamp": format_iso(utc_now()),
                "service": "unified-docker-sim",
                "level": "info",
                "message": f"Container {container_name} simulated successfully",
            },
        ]

        logs.extend(simulation_logs)

        # Store simulated container
        self.containers[task_id] = {
            "id": f"sim_{task_id}_{int(utc_now().timestamp())}",
            "name": container_name,
            "status": "running",
            "image": self.image_name,
        }

        return {
            "success": True,
            "container_id": f"sim_{task_id}_{int(utc_now().timestamp())}",
            "container_name": container_name,
            "logs": logs,
        }

    def _ensure_image_available_sdk(self, image_name: str) -> None:
        """Ensure image exists locally for SDK create path."""
        if not self.client or not hasattr(self.client, "images"):
            return
        try:
            self.client.images.get(image_name)
        except Exception:
            logger.info("Pulling missing Docker image: %s", image_name)
            self.client.images.pull(image_name)

    async def _check_image_exists(self) -> bool:
        """Use logs service image lookup as single source of truth."""
        if self._check_image_exists_fn is not None:
            return await self._check_image_exists_fn()

        # Fallback preserves direct lifecycle instantiation compatibility.
        from .logs import ContainerLogs

        return await ContainerLogs(self._client).check_image_exists()

    async def _ensure_image_available(self) -> List[Dict[str, Any]]:
        """Ensure Docker image is available locally."""
        logs = []

        image_exists = await self._check_image_exists()
        refresh_tagged = image_exists and not is_digest_pinned_runtime_image(
            self.image_name
        )
        should_pull = not image_exists or refresh_tagged

        if self.api_mode == "sdk":
            if not should_pull:
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": f"Image {self.image_name} found locally",
                    }
                )
            else:
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": (
                            f"Refreshing tagged image {self.image_name}..."
                            if refresh_tagged
                            else f"Pulling image {self.image_name}..."
                        ),
                    }
                )
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": "This may take several minutes depending on your internet connection",
                    }
                )

                # Pull image with detailed progress
                try:
                    for line in self.client.api.pull(self.image_name, stream=True, decode=True):
                        if "status" in line:
                            # Handle different types of pull progress
                            if "id" in line and "status" in line:
                                # Layer download progress
                                layer_id = line.get("id", "unknown")[:12]
                                status = line["status"]
                                if "Downloading" in status:
                                    progress_detail = line.get("progressDetail", {})
                                    if progress_detail:
                                        current = progress_detail.get("current", 0)
                                        total = progress_detail.get("total", 0)
                                        if total > 0:
                                            percentage = int((current / total) * 100)
                                            logs.append(
                                                {
                                                    "timestamp": format_iso(utc_now()),
                                                    "service": "unified-docker",
                                                    "level": "info",
                                                    "message": f"Downloading layer {layer_id}: {percentage}% ({current}/{total} bytes)",
                                                }
                                            )
                                        else:
                                            logs.append(
                                                {
                                                    "timestamp": format_iso(utc_now()),
                                                    "service": "unified-docker",
                                                    "level": "info",
                                                    "message": f"Downloading layer {layer_id}: {status}",
                                                }
                                            )
                                    else:
                                        logs.append(
                                            {
                                                "timestamp": format_iso(utc_now()),
                                                "service": "unified-docker",
                                                "level": "info",
                                                "message": f"Downloading layer {layer_id}: {status}",
                                            }
                                        )
                                elif "Extracting" in status:
                                    logs.append(
                                        {
                                            "timestamp": format_iso(utc_now()),
                                            "service": "unified-docker",
                                            "level": "info",
                                            "message": f"Extracting layer {layer_id}: {status}",
                                        }
                                    )
                                elif "Verifying" in status:
                                    logs.append(
                                        {
                                            "timestamp": format_iso(utc_now()),
                                            "service": "unified-docker",
                                            "level": "info",
                                            "message": f"Verifying layer {layer_id}: {status}",
                                        }
                                    )
                                elif "Pull complete" in status:
                                    logs.append(
                                        {
                                            "timestamp": format_iso(utc_now()),
                                            "service": "unified-docker",
                                            "level": "info",
                                            "message": f"Layer {layer_id} completed: {status}",
                                        }
                                    )
                                else:
                                    logs.append(
                                        {
                                            "timestamp": format_iso(utc_now()),
                                            "service": "unified-docker",
                                            "level": "info",
                                            "message": f"Layer {layer_id}: {status}",
                                        }
                                    )
                            elif "status" in line and "Digest:" in line["status"]:
                                # Image digest verification
                                logs.append(
                                    {
                                        "timestamp": format_iso(utc_now()),
                                        "service": "unified-docker",
                                        "level": "info",
                                        "message": f"Verifying image: {line['status']}",
                                    }
                                )
                            elif "status" in line and "Downloaded newer image" in line["status"]:
                                # Final success message
                                logs.append(
                                    {
                                        "timestamp": format_iso(utc_now()),
                                        "service": "unified-docker",
                                        "level": "info",
                                        "message": f"Image pull completed: {line['status']}",
                                    }
                                )
                            elif "error" in line:
                                # Error during pull
                                logs.append(
                                    {
                                        "timestamp": format_iso(utc_now()),
                                        "service": "unified-docker",
                                        "level": "error",
                                        "message": f"Pull error: {line['error']}",
                                    }
                                )
                            else:
                                # Generic status update
                                logs.append(
                                    {
                                        "timestamp": format_iso(utc_now()),
                                        "service": "unified-docker",
                                        "level": "info",
                                        "message": f"Pull: {line['status']}",
                                    }
                                )
                except Exception as pull_error:
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "warning" if image_exists else "error",
                            "message": (
                                "Failed to refresh tagged image; using existing local "
                                f"image {self.image_name}: {str(pull_error)}"
                                if image_exists
                                else f"Failed to pull image: {str(pull_error)}"
                            ),
                        }
                    )
                    if not image_exists:
                        raise pull_error
        else:
            # CLI mode - pull missing images and refresh mutable tags.
            if should_pull:
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": (
                            f"Refreshing tagged image {self.image_name} via CLI..."
                            if refresh_tagged
                            else f"Pulling image {self.image_name} via CLI..."
                        ),
                    }
                )
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": "This may take several minutes depending on your internet connection",
                    }
                )

                # Use docker pull with progress output
                try:
                    pull_result = subprocess.run(
                        ["docker", "pull", self.image_name],
                        capture_output=True,
                        text=True,
                        timeout=1800,  # 30 minute timeout
                    )

                    if pull_result.returncode == 0:
                        # Parse the output for progress information
                        for line in pull_result.stdout.split("\n"):
                            if line.strip():
                                if (
                                    "Downloading" in line
                                    or "Extracting" in line
                                    or "Verifying" in line
                                ):
                                    logs.append(
                                        {
                                            "timestamp": format_iso(utc_now()),
                                            "service": "unified-docker",
                                            "level": "info",
                                            "message": line.strip(),
                                        }
                                    )

                        logs.append(
                            {
                                "timestamp": format_iso(utc_now()),
                                "service": "unified-docker",
                                "level": "info",
                                "message": f"Image {self.image_name} pulled successfully",
                            }
                        )
                    else:
                        logs.append(
                            {
                                "timestamp": format_iso(utc_now()),
                                "service": "unified-docker",
                                "level": "error",
                                "message": f"Failed to pull image: {pull_result.stderr}",
                            }
                        )
                        raise Exception(f"CLI pull failed: {pull_result.stderr}")
                except subprocess.TimeoutExpired:
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "error",
                            "message": "Image pull timed out after 30 minutes",
                        }
                    )
                    if not image_exists:
                        raise Exception("Image pull timed out")
                except Exception as cli_error:
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "warning" if image_exists else "error",
                            "message": (
                                "CLI refresh failed; using existing local image "
                                f"{self.image_name}: {str(cli_error)}"
                                if image_exists
                                else f"CLI pull error: {str(cli_error)}"
                            ),
                        }
                    )
                    if not image_exists:
                        raise cli_error
            else:
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": f"Image {self.image_name} found locally",
                    }
                )

        return logs

    async def _create_container_sdk(self, config: Dict[str, Any]) -> Any:
        """Create container using Docker SDK."""
        return self.client.containers.create(
            image=config["image"],
            name=config["name"],
            environment=config["environment"],
            volumes=config["volumes"],
            network=config["network"],
            extra_hosts=config.get("extra_hosts", {}),
            detach=config["detach"],
            tty=config["tty"],
            stdin_open=config["stdin_open"],
            command=config["command"],
            working_dir=config["working_dir"],
            labels=config["labels"],
            user=config.get("user"),
            cap_add=config.get("cap_add"),
            devices=config.get("devices"),
            privileged=config.get("privileged", False),
        )

    def _ensure_task_network(self, config: Dict[str, Any], task_id: int) -> Dict[str, Any]:
        """Create or validate the local provider's isolated task bridge."""
        pool = parse_runtime_network_pool(os.getenv("DROWAI_RUNTIME_NETWORK_POOL"))
        spec = build_runtime_network_spec(
            container_name=config["name"],
            runtime_identity=config["name"],
            tenant_id=config["runtime_network_tenant_id"],
            task_id=task_id,
            runtime_owner="local-docker",
            pool=pool,
        )
        if self.api_mode == "sdk":
            return self._ensure_task_network_sdk(spec)
        return self._ensure_task_network_cli(spec)

    def _ensure_task_network_sdk(self, spec: Any) -> Dict[str, Any]:
        result = DockerTaskNetworkManager(lambda: self.client).ensure(spec)
        return {
            "name": result.name,
            "subnet": result.subnet,
            "action": "created" if result.created else "reused",
            "created": result.created,
        }

    def _ensure_task_network_cli(self, spec: Any) -> Dict[str, Any]:
        existing = self._inspect_cli_network(spec.name)
        if existing is not None:
            subnet = validate_managed_network(spec, existing)
            return {"name": spec.name, "subnet": str(subnet), "action": "reused", "created": False}
        attempted: set[IPv4Network] = set()
        while True:
            occupied = self._occupied_cli_subnets()
            candidate = next(
                (item for item in iter_runtime_subnets(spec, occupied) if item not in attempted),
                None,
            )
            if candidate is None:
                raise RuntimeNetworkError("Managed runtime network pool is exhausted.")
            attempted.add(candidate)
            cmd = ["docker", "network", "create", "--driver", RUNTIME_NETWORK_DRIVER, "--subnet", str(candidate)]
            for key, value in RUNTIME_NETWORK_OPTIONS.items():
                cmd.extend(["--opt", f"{key}={value}"])
            for key, value in network_labels(spec, candidate).items():
                cmd.extend(["--label", f"{key}={value}"])
            cmd.append(spec.name)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return {"name": spec.name, "subnet": str(candidate), "action": "created", "created": True}
            raced = self._inspect_cli_network(spec.name)
            if raced is not None:
                subnet = validate_managed_network(spec, raced)
                return {"name": spec.name, "subnet": str(subnet), "action": "reused", "created": False}
            if "overlap" in result.stderr.lower():
                continue
            raise RuntimeError(f"Docker network creation failed: {result.stderr.strip()}")

    def _occupied_cli_subnets(self) -> tuple[IPv4Network, ...]:
        ids = subprocess.run(
            ["docker", "network", "ls", "-q"], capture_output=True, text=True
        )
        if ids.returncode != 0 or not ids.stdout.split():
            return ()
        result = subprocess.run(
            ["docker", "network", "inspect", *ids.stdout.split()],
            capture_output=True,
            text=True,
        )
        try:
            payload = json.loads(result.stdout) if result.returncode == 0 else []
        except json.JSONDecodeError:
            payload = []
        return self._subnets_from_inspections(payload if isinstance(payload, list) else [])

    @staticmethod
    def _subnets_from_inspections(inspections: List[Dict[str, Any]]) -> tuple[IPv4Network, ...]:
        subnets: list[IPv4Network] = []
        for attributes in inspections:
            for config in ((attributes.get("IPAM") or {}).get("Config") or []):
                try:
                    parsed = ip_network(str(config.get("Subnet")), strict=False)
                except ValueError:
                    continue
                if isinstance(parsed, IPv4Network):
                    subnets.append(parsed)
        return tuple(subnets)

    @staticmethod
    def _inspect_cli_network(name: str) -> Dict[str, Any] | None:
        result = subprocess.run(
            ["docker", "network", "inspect", name], capture_output=True, text=True
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeNetworkError("Docker network inspection returned invalid JSON.") from exc
        return payload[0] if isinstance(payload, list) and payload else None

    def _remove_task_network_best_effort(self, config: Dict[str, Any], task_id: int) -> None:
        """Remove an empty local-owned bridge created by a failed provision."""
        try:
            spec = build_runtime_network_spec(
                container_name=config["name"],
                runtime_identity=config["name"],
                tenant_id=config["runtime_network_tenant_id"],
                task_id=task_id,
                runtime_owner="local-docker",
                pool=parse_runtime_network_pool(os.getenv("DROWAI_RUNTIME_NETWORK_POOL")),
            )
            if self.api_mode == "sdk":
                DockerTaskNetworkManager(lambda: self.client).remove_empty(spec)
            else:
                attributes = self._inspect_cli_network(spec.name)
                if attributes is not None:
                    validate_managed_network(spec, attributes)
                    if not attributes.get("Containers"):
                        subprocess.run(["docker", "network", "rm", spec.name], check=False)
        except Exception:
            logger.warning("Failed to clean empty managed network for task %s", task_id)

    def _cleanup_failed_provision(
        self,
        container: Any,
        config: Dict[str, Any],
        task_id: int,
        *,
        remove_network: bool,
    ) -> None:
        """Remove a failed container before retiring its newly created empty bridge."""
        self._stop_failed_container(container)
        try:
            if hasattr(container, "remove"):
                container.remove(force=True)
            else:
                subprocess.run(
                    ["docker", "rm", "-f", config["name"]],
                    capture_output=True,
                    text=True,
                )
        except Exception:
            logger.warning("Failed to remove failed runtime container for task %s", task_id)
        if remove_network:
            self._remove_task_network_best_effort(config, task_id)

    async def _create_container_cli(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Create container using Docker CLI."""
        cmd = ["docker", "run", "-d", "--name", config["name"]]

        # Privileged mode (for TUN access)
        if config.get("privileged"):
            cmd.append("--privileged")

        # Capabilities
        for cap in config.get("cap_add", []) or []:
            cmd.extend(["--cap-add", cap])

        # Device mappings
        for dev in config.get("devices", []) or []:
            cmd.extend(["--device", dev])

        # Add environment variables
        for key, value in config["environment"].items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add volume mounts
        for host_path, mount_config in config["volumes"].items():
            mount_str = f"{host_path}:{mount_config['bind']}"
            if mount_config.get("mode"):
                mount_str += f":{mount_config['mode']}"
            cmd.extend(["-v", mount_str])

        # Add extra hosts (for Linux host.docker.internal support)
        for host, ip in config.get("extra_hosts", {}).items():
            cmd.extend(["--add-host", f"{host}:{ip}"])

        # Add labels
        for key, value in config["labels"].items():
            cmd.extend(["--label", f"{key}={value}"])

        # Add other options
        cmd.extend(
            [
                "--network",
                config["network"],
                "-t",
                "-i",
                "--workdir",
                config["working_dir"],
                config["image"],
            ]
        )

        cmd.extend(config["command"])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return {"id": result.stdout.strip(), "name": config["name"]}
        raise Exception(f"CLI container creation failed: {result.stderr}")

    async def _start_container(self, container: Any, task_id: int) -> Dict[str, Any]:
        """Start the created container."""
        logs = []

        try:
            if hasattr(container, "start"):
                # SDK mode
                container.start()
                container_id = container.id
            else:
                # CLI mode - container is already started by 'docker run -d'
                container_id = container["id"]

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"Container started successfully (ID: {container_id[:12]})",
                }
            )

            return {"success": True, "logs": logs}

        except Exception as e:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "error",
                    "message": f"Failed to start container: {str(e)}",
                }
            )
            return {"success": False, "error": str(e), "logs": logs}

    def _verify_runtime_contract(self, container: Any) -> tuple[bool, str | None]:
        """Fail closed when the running image does not report layout 2.0 contracts."""
        command = [
            "python3",
            "/opt/drowai/runtime/python/executor_daemon.py",
            "--runtime-info",
        ]
        try:
            if hasattr(container, "exec_run"):
                result = container.exec_run(command)
                exit_code = int(result.exit_code)
                output = result.output.decode("utf-8", errors="replace")
            else:
                container_name = container.get("name")
                result = subprocess.run(
                    ["docker", "exec", container_name, *command],
                    capture_output=True,
                    text=True,
                )
                exit_code = result.returncode
                output = result.stdout
            if exit_code != 0:
                return False, "Runtime manifest probe failed."
            payload = json.loads(output)
        except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return False, "Runtime manifest probe failed."

        expected = build_runtime_manifest().to_dict()
        checked_keys = (
            "runtime_contract_version",
            "file_comm_schema_version",
            "workspace_layout_version",
            "semantic_schema_versions",
        )
        mismatch = [key for key in checked_keys if payload.get(key) != expected.get(key)]
        if mismatch:
            return False, "Runtime manifest contract mismatch: " + ", ".join(mismatch)
        return True, None

    @staticmethod
    def _stop_failed_container(container: Any) -> None:
        """Best-effort stop for a runtime that failed its startup contract."""
        try:
            if hasattr(container, "stop"):
                container.stop(timeout=1)
                return
            container_name = container.get("name")
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
            )
        except Exception:
            return

    async def _initialize_container_environment(
        self,
        container: Any,
        task_id: int,
    ) -> List[Dict[str, Any]]:
        """Initialize the container environment with task-specific workspace structure."""
        logs = []

        try:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": "Initializing container environment with task isolation...",
                }
            )

            # Create workspaces directory structure in container for agent compatibility
            # This ensures the agent can find workspaces/task-{id} structure even though
            # only the specific task workspace is mounted (path follows active runtime path mode).
            mount_policy = WORKSPACE_CONTROL_MOUNT_POLICY
            init_commands = self._runtime_config.workspace_bootstrap_commands_for_policy(
                task_id, mount_policy
            )

            for cmd in init_commands:
                if hasattr(container, "exec_run"):
                    # SDK mode
                    result = container.exec_run(cmd, privileged=False)
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "info",
                            "message": f"Init command: {cmd} -> {result.output.decode() if result.output else 'OK'}",
                        }
                    )
                else:
                    # CLI mode
                    container_name = container.get("name", f"kali-container-{task_id}")
                    cli_cmd = ["docker", "exec", container_name, "bash", "-c", cmd]
                    result = subprocess.run(cli_cmd, capture_output=True, text=True)
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "info",
                            "message": f"Init command: {cmd} -> {result.stdout if result.stdout else 'OK'}",
                        }
                    )

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"Task {task_id} workspace isolation configured successfully",
                }
            )

            # Basic environment check
            if hasattr(container, "exec_run"):
                # SDK mode
                result = container.exec_run("python3 --version")
                if result.exit_code == 0:
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "info",
                            "message": f"Python version: {result.output.decode().strip()}",
                        }
                    )

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": "Container environment initialized successfully",
                }
            )

        except Exception as e:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "warning",
                    "message": f"Environment initialization warning: {str(e)}",
                }
            )

        return logs

    async def _ensure_vpn_ready(
        self,
        container: Any,
        task_id: int,
    ) -> List[Dict[str, Any]]:
        """Ensure VPN is connected if enabled for this task.

        Runs the VPN connect script synchronously inside the container.
        Best-effort: failures are logged but do not block startup.

        Args:
            container: Docker container object (SDK mode).
            task_id: Task identifier.

        Returns:
            List of lifecycle log entries.
        """
        logs: List[Dict[str, Any]] = []

        if not hasattr(container, "exec_run"):
            return logs

        db = None
        try:
            from backend.database import SessionLocal
            from backend.models.core import Task as TaskModel

            db = SessionLocal()
            task = db.query(TaskModel).filter(TaskModel.id == task_id).first()
            if not (task and task.vpn_enabled):
                return logs

            recovery_diag = self._runtime_config.get_runtime_path_diagnostic_fields()
            recovery_shell = self._runtime_config.build_vpn_connect_exec_shell(task_id)
            logger.info(
                "[runtime-path-mode] task_id=%s context=vpn_ensure_ready "
                "effective_mode=%s activation=%s mount_policy=%s vpn_script=%s",
                task_id,
                recovery_diag["effective_mode"],
                recovery_diag["activation_reason"],
                recovery_diag["mount_policy"],
                recovery_diag["vpn_script_path"],
            )

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"Ensuring VPN is ready for task {task_id}...",
                }
            )

            container.exec_run(["bash", "-lc", recovery_shell])

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"VPN readiness check completed for task {task_id}",
                }
            )

        except Exception as e:
            logger.debug(
                "VPN readiness check skipped for task %s: %s",
                task_id,
                e,
            )
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "warning",
                    "message": f"VPN readiness check failed: {e}",
                }
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

        return logs

    async def _collect_and_save_environment_info(
        self,
        container: Any,
        task_id: int,
    ) -> List[Dict[str, Any]]:
        """Collect environment info from container and save to workspace."""
        logs: List[Dict[str, Any]] = []

        try:
            # Only collect if container has exec_run (SDK mode)
            if not hasattr(container, "exec_run"):
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": "Environment info collection skipped (CLI mode)",
                    }
                )
                return logs

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": "Collecting container environment info...",
                }
            )

            # Collect environment info
            env_info = collect_environment_info(container)

            # Save to workspace (injected seam preserves shim patch target compatibility)
            env_file = self._save_environment_info(task_id, env_info)

            # Log summary
            hostname = env_info.get("hostname", "unknown")
            os_name = env_info.get("os", {}).get("name", "unknown")
            interfaces = env_info.get("network", {}).get("interfaces", [])
            interface_count = len([i for i in interfaces if i.get("name") != "lo"])

            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"Environment info collected: {hostname} ({os_name}), {interface_count} network interface(s)",
                }
            )

            # Log any collection errors
            collection_errors = env_info.get("collection_errors", [])
            if collection_errors:
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "warning",
                        "message": f"Some environment data unavailable: {', '.join(collection_errors)}",
                    }
                )

            logger.info("[ENV] Saved environment info for task %s to %s", task_id, env_file)

        except Exception as e:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "warning",
                    "message": f"Environment info collection failed: {str(e)}",
                }
            )
            logger.warning("[ENV] Failed to collect environment info for task %s: %s", task_id, e)

        return logs

    async def pull_image(self, task_id: int) -> List[Dict[str, Any]]:
        """Legacy method - redirects to unified image management."""
        return await self._ensure_image_available()

    async def create_container(
        self,
        task_id: int,
        target: str = "127.0.0.1",
        **kwargs,
    ) -> Dict[str, Any]:
        """Legacy method - redirects to unified container creation."""
        user_id = kwargs.get("user_id")
        tenant_id = kwargs.get("tenant_id", "local")
        return await self.create_and_start_container(task_id, target, user_id, tenant_id)
