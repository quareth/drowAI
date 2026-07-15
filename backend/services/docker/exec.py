"""
Container command execution and PTY session primitives.

Scope:
- Execute ad-hoc commands inside running containers (SDK and CLI modes).
- Start persistent PTY exec sessions for interactive terminal workflows.
- Broadcast log entries to task WebSocket subscribers.
- Provide container name lookup compatibility helper.

Boundary:
- Does NOT create/start/stop/remove containers (that is lifecycle.py).
- Does NOT collect metrics (that is metrics.py) or retrieve logs (that is logs.py).
- Does NOT own high-level terminal session orchestration
  (that is terminal_session_manager.py).
"""

import logging
import subprocess
from typing import Any, Dict, List

from backend.core.time_utils import format_iso, utc_now

from ..container_utils import get_container_name
from .client import DockerClient

# Keep logger name aligned with monolith for caplog/patch compatibility.
logger = logging.getLogger("backend.services.unified_docker_service")


class ContainerExec:
    """Container command execution and low-level PTY session operations."""

    def __init__(self, client: DockerClient):
        self._client = client
        self.client = client.client
        self.docker_available = client.docker_available
        self.api_mode = client.api_mode
        self.image_name = client.image_name
        self.containers = client.containers

    async def execute_container_command(
        self, task_id: int, command: str
    ) -> Dict[str, Any]:
        """Execute a command and preserve its exit status with bounded logs."""
        container_name = get_container_name(task_id)
        logs = []

        if not self.docker_available:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker-sim",
                    "level": "info",
                    "message": f"Simulated command execution: {command}",
                }
            )
            return {"success": True, "exit_code": 0, "stdout": "", "stderr": "", "logs": logs}

        try:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "info",
                    "message": f"Executing: {command}",
                }
            )

            if self.api_mode == "sdk":
                container = self.client.containers.get(container_name)
                result = container.exec_run(command, stream=False)

                raw_output = result.output.decode(errors="replace") if result.output else ""
                output_lines = raw_output.splitlines()
                for decoded_line in output_lines:
                    if decoded_line.strip():
                        logs.append(
                            {
                                "timestamp": format_iso(utc_now()),
                                "service": "kali-container",
                                "level": "info",
                                "message": decoded_line.strip(),
                            }
                        )

                exit_code = int(result.exit_code)
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": f"Command completed with exit code: {exit_code}",
                    }
                )
            else:
                # CLI mode
                result = subprocess.run(
                    ["docker", "exec", container_name, "/bin/bash", "-c", command],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

                exit_code = int(result.returncode)
                output_lines = result.stdout.splitlines() if result.stdout else []
                if result.stdout:
                    for line in result.stdout.split("\n"):
                        if line.strip():
                            logs.append(
                                {
                                    "timestamp": format_iso(utc_now()),
                                    "service": "kali-container",
                                    "level": "info",
                                    "message": line.strip(),
                                }
                            )

                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": f"Command completed with exit code: {exit_code}",
                    }
                )

            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": "\n".join(output_lines),
                "stderr": result.stderr if self.api_mode != "sdk" else "",
                "logs": logs,
                "error": None if exit_code == 0 else f"Command exited with status {exit_code}",
            }
        except Exception as e:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker",
                    "level": "error",
                    "message": f"Command execution failed: {str(e)}",
                }
            )

            return {
                "success": False,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "logs": logs,
                "error": str(e),
            }

    async def broadcast_log_to_websocket(self, task_id: int, log_entry: Dict[str, Any]):
        """Broadcast log entry to WebSocket connections."""
        try:
            from ..websocket.connection_manager import websocket_manager

            await websocket_manager.broadcast_to_task(
                task_id,
                {
                    "type": "log_entry",
                    "task_id": task_id,
                    "data": log_entry,
                },
            )
        except Exception as e:
            logger.debug(f"WebSocket broadcast failed: {e}")

    def get_container_name_by_id(self, task_id: int) -> str:
        """Get container name for external compatibility."""
        return get_container_name(task_id)

    async def start_persistent_pty(
        self, task_id: int, shell: str = "/bin/bash", cols: int = 80, rows: int = 24
    ):
        """
        Start a persistent PTY exec session in the container for interactive terminal use.
        Returns (exec_instance, socket) for interactive communication.
        """
        container_name = get_container_name(task_id)
        if not self.docker_available:
            raise RuntimeError("Docker is not available")
        if self.api_mode != "sdk":
            raise RuntimeError("Persistent PTY only supported in SDK mode")
        container = self.client.containers.get(container_name)
        exec_id = self.client.api.exec_create(
            container.id,
            cmd=shell,
            tty=True,
            stdin=True,
            stdout=True,
            stderr=True,
            environment=None,
            privileged=True,
            user="root",
        )["Id"]
        sock = self.client.api.exec_start(
            exec_id, detach=False, tty=True, stream=True, socket=True, demux=False
        )
        # Optionally resize PTY
        try:
            self.client.api.exec_resize(exec_id, height=rows, width=cols)
        except Exception as e:
            logger.warning(f"PTY resize failed: {e}")
        return exec_id, sock
