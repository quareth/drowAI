"""
Container runtime control operations.

Scope:
- Provide direct container control actions (status/stop/pause/unpause/signal/remove).
- Provide managed-container registry access and cleanup helpers.

Boundary:
- Does NOT orchestrate container creation/startup flows (that is lifecycle.py).
- Does NOT retrieve logs/startup progress (that is logs.py).
- Does NOT execute ad-hoc commands or PTY sessions (that is exec.py).
- Does NOT collect metrics (that is metrics.py).
"""

from __future__ import annotations

import subprocess
import json
from typing import Any, Dict, List, Tuple

from ..container_utils import get_container_name
from .client import DockerClient
from runtime_shared.runtime_network import (
    build_runtime_network_name,
    is_owned_task_network,
    TASK_LABEL,
)


class ContainerOperations:
    """Direct container runtime operations and registry cleanup helpers."""

    def __init__(self, client: DockerClient):
        self._client = client
        self.client = client.client
        self.docker_available = client.docker_available
        self.api_mode = client.api_mode
        self.image_name = client.image_name
        self.containers = client.containers

    async def get_container_status(self, task_id: int) -> str:
        """Get container status."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            return "simulated"

        try:
            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                container.reload()
                return container.status
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{.State.Status}}"],
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() if result.returncode == 0 else "not_found"
        except Exception:
            return "not_found"

    async def stop_container(self, task_id: int) -> Tuple[bool, str]:
        """Stop container."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            return True, f"Simulated stop of {container_name}"

        try:
            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                container.stop()
            else:
                subprocess.run(["docker", "stop", container_name], check=True)

            return True, f"Container {container_name} stopped successfully"
        except Exception as e:
            return False, str(e)

    async def pause_container(self, task_id: int) -> Tuple[bool, str]:
        """Pause container (Docker pause - freezes processes)."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            return True, f"Simulated pause of {container_name}"

        try:
            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                container.pause()
            else:
                subprocess.run(["docker", "pause", container_name], check=True)

            return True, f"Container {container_name} paused successfully"
        except Exception as e:
            return False, str(e)

    async def unpause_container(self, task_id: int) -> Tuple[bool, str]:
        """Unpause container (Docker unpause - resumes processes)."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            return True, f"Simulated unpause of {container_name}"

        try:
            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                container.unpause()
            else:
                subprocess.run(["docker", "unpause", container_name], check=True)

            return True, f"Container {container_name} unpaused successfully"
        except Exception as e:
            return False, str(e)

    async def send_signal(self, task_id: int, signal_name: str) -> Tuple[bool, str]:
        """Send a POSIX signal to the running container."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            return True, f"Simulated send {signal_name} to {container_name}"

        try:
            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                container.kill(signal=signal_name)
            else:
                subprocess.run(
                    ["docker", "kill", "-s", signal_name, container_name],
                    check=True,
                )
            return True, f"Signal {signal_name} sent to {container_name}"
        except Exception as e:
            return False, str(e)

    async def remove_container(self, task_id: int, force: bool = False) -> Tuple[bool, str]:
        """Remove container."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            if task_id in self.containers:
                del self.containers[task_id]
            return True, f"Simulated removal of {container_name}"

        try:
            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                container.remove(force=force)
            else:
                cmd = ["docker", "rm"]
                if force:
                    cmd.append("-f")
                cmd.append(container_name)
                subprocess.run(cmd, check=True)

            # Clean up local reference
            if task_id in self.containers:
                del self.containers[task_id]

            self._remove_empty_task_network(container_name, task_id)

            return True, f"Container {container_name} removed successfully"
        except Exception as e:
            return False, str(e)

    def _remove_empty_task_network(self, container_name: str, task_id: int) -> None:
        """Remove only an empty bridge carrying this provider's ownership labels."""
        network_name = build_runtime_network_name(container_name)
        try:
            if self.api_mode == "sdk":
                network = self.client.networks.get(network_name)
                network.reload()
                attributes = network.attrs
                labels = attributes.get("Labels") or {}
                if (
                    is_owned_task_network(attributes, runtime_owner="local-docker")
                    and labels.get(TASK_LABEL) == str(task_id)
                    and not attributes.get("Containers")
                ):
                    network.remove()
                return
            result = subprocess.run(
                ["docker", "network", "inspect", network_name],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return
            payload = json.loads(result.stdout)
            attributes = payload[0] if isinstance(payload, list) and payload else {}
            labels = attributes.get("Labels") or {}
            if (
                is_owned_task_network(attributes, runtime_owner="local-docker")
                and labels.get(TASK_LABEL) == str(task_id)
                and not attributes.get("Containers")
            ):
                subprocess.run(["docker", "network", "rm", network_name], check=False)
        except Exception:
            return

    async def get_all_containers(self) -> Dict[int, Any]:
        """Get all managed containers."""
        return self.containers.copy()

    async def cleanup_containers(self, force: bool = False) -> List[str]:
        """Clean up all managed containers."""
        cleanup_results = []
        for task_id in list(self.containers.keys()):
            success, message = await self.remove_container(task_id, force=force)
            cleanup_results.append(f"Task {task_id}: {message}")
        return cleanup_results
