"""
Container log retrieval and startup-progress observability.

Scope:
- Retrieve container logs in SDK and CLI modes.
- Report startup progress by checking container/image existence and pull state.
- Expose read-only Docker queries used by observability endpoints.

Boundary:
- Does NOT create/start/stop/remove containers (that is lifecycle.py).
- Does NOT execute ad-hoc commands or PTY sessions (that is exec.py).
- Does NOT collect runtime metrics (that is metrics.py).
"""

import logging
import subprocess
from datetime import timedelta
from typing import Any, Dict, List

from backend.core.time_utils import format_iso, utc_now

from ..container_utils import get_container_name
from .client import DockerClient
from runtime_shared.vpn_observability import normalize_vpn_log_lines

# Keep logger name aligned with monolith for caplog/patch compatibility.
logger = logging.getLogger("backend.services.unified_docker_service")


class ContainerLogs:
    """Container logs and startup-progress read-only queries."""

    def __init__(self, client: DockerClient):
        self._client = client
        self.client = client.client
        self.docker_available = client.docker_available
        self.api_mode = client.api_mode
        self.image_name = client.image_name
        self.containers = client.containers

    async def get_container_logs(
        self, task_id: int, lines: int = 50
    ) -> List[Dict[str, Any]]:
        """Get container logs with real-time streaming capability and startup handling."""
        container_name = get_container_name(task_id)
        logs = []

        if not self.docker_available:
            logs.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "unified-docker-sim",
                    "level": "info",
                    "message": f"Simulated logs for {container_name}",
                }
            )
            return logs

        try:
            # First check if container exists
            container_exists = await self.check_container_exists(task_id)

            if not container_exists:
                # Container doesn't exist yet - provide creation progress feedback
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": f"Container {container_name} is being created...",
                    }
                )

                # Check if image exists to determine what's happening
                image_exists = await self.check_image_exists()
                if not image_exists:
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "info",
                            "message": f"Pulling Docker image {self.image_name}...",
                        }
                    )
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "info",
                            "message": "This may take a few minutes on first run",
                        }
                    )

                    # Check if there's an active pull process
                    try:
                        if self.api_mode == "sdk":
                            # Check for active pulls in SDK mode
                            images = self.client.images.list()
                            # Look for any image with our name that might be in progress
                            for img in images:
                                if any(
                                    tag.startswith(self.image_name.split(":")[0])
                                    for tag in img.tags or []
                                ):
                                    logs.append(
                                        {
                                            "timestamp": format_iso(utc_now()),
                                            "service": "unified-docker",
                                            "level": "info",
                                            "message": "Image pull in progress...",
                                        }
                                    )
                                    break
                        else:
                            # CLI mode - check for active pulls
                            result = subprocess.run(
                                [
                                    "docker",
                                    "images",
                                    "--filter",
                                    "dangling=false",
                                    "--format",
                                    "{{.Repository}}:{{.Tag}}",
                                ],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            if self.image_name in result.stdout:
                                logs.append(
                                    {
                                        "timestamp": format_iso(utc_now()),
                                        "service": "unified-docker",
                                        "level": "info",
                                        "message": "Image found locally, creating container...",
                                    }
                                )
                    except Exception:
                        # Ignore errors in pull status check
                        pass
                else:
                    logs.append(
                        {
                            "timestamp": format_iso(utc_now()),
                            "service": "unified-docker",
                            "level": "info",
                            "message": "Docker image found locally, creating container...",
                        }
                    )

                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "info",
                        "message": "Logs will appear here once the container is running",
                    }
                )
                return logs

            # Container exists - get actual logs
            if self.api_mode == "sdk":
                try:
                    container = self.client.containers.get(container_name)
                    log_output = container.logs(
                        tail=lines,
                        timestamps=True,
                        since=utc_now() - timedelta(hours=1),
                    ).decode("utf-8")

                    # Parse logs
                    for line in log_output.split("\n"):
                        if line.strip():
                            parts = line.split(" ", 1)
                            if len(parts) >= 2:
                                logs.append(
                                    {
                                        "timestamp": parts[0],
                                        "service": "kali-container",
                                        "level": "info",
                                        "message": parts[1],
                                    }
                                )
                except Exception as e:
                    if "not found" in str(e).lower():
                        # Container was removed between existence check and log retrieval
                        logs.append(
                            {
                                "timestamp": format_iso(utc_now()),
                                "service": "unified-docker",
                                "level": "warn",
                                "message": f"Container {container_name} was removed while retrieving logs",
                            }
                        )
                    else:
                        raise
            else:
                # CLI mode
                result = subprocess.run(
                    ["docker", "logs", "--tail", str(lines), "--timestamps", container_name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if line.strip():
                            # Clean ANSI escape sequences
                            from ...utils.ansi_cleaner import clean_ansi_codes

                            cleaned_line = clean_ansi_codes(line)
                            parts = cleaned_line.split(" ", 1)
                            if len(parts) >= 2:
                                logs.append(
                                    {
                                        "timestamp": parts[0],
                                        "service": "kali-container",
                                        "level": "info",
                                        "message": parts[1],
                                    }
                                )
                else:
                    # Container exists but no logs yet or error
                    if "not found" in result.stderr.lower():
                        logs.append(
                            {
                                "timestamp": format_iso(utc_now()),
                                "service": "unified-docker",
                                "level": "warn",
                                "message": f"Container {container_name} was removed while retrieving logs",
                            }
                        )
                    else:
                        logs.append(
                            {
                                "timestamp": format_iso(utc_now()),
                                "service": "unified-docker",
                                "level": "info",
                                "message": f"Container {container_name} is running but no logs available yet",
                            }
                        )

        except Exception as e:
            logger.error(f"Failed to get container logs for task {task_id}: {e}")
            # Don't add error logs for "not found" errors during startup
            if "not found" not in str(e).lower() and "no such container" not in str(
                e
            ).lower():
                logs.append(
                    {
                        "timestamp": format_iso(utc_now()),
                        "service": "unified-docker",
                        "level": "error",
                        "message": f"Failed to retrieve logs: {str(e)}",
                    }
                )

        logs.extend(self._get_vpn_logs(container_name=container_name, lines=lines))

        return logs

    def _get_vpn_logs(self, *, container_name: str, lines: int) -> List[Dict[str, Any]]:
        """Read the existing task-runtime VPN log without a second stream path."""
        try:
            command = ["/bin/bash", "-lc", f"tail -n {max(1, int(lines))} /vpn/connection.log 2>/dev/null || true"]
            if self.api_mode == "sdk":
                result = self.client.containers.get(container_name).exec_run(command)
                raw_output = result.output.decode("utf-8", errors="replace") if result.output else ""
            else:
                result = subprocess.run(
                    ["docker", "exec", container_name, *command],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                raw_output = result.stdout
            entries = normalize_vpn_log_lines(raw_output.splitlines())
            fallback_timestamp = format_iso(utc_now())
            for entry in entries:
                if not entry.get("timestamp"):
                    entry["timestamp"] = fallback_timestamp
            return entries
        except Exception as exc:
            logger.debug("Failed to read VPN logs for %s: %s", container_name, exc)
            return []

    async def check_container_exists(self, task_id: int) -> bool:
        """Check if a container exists for the given task."""
        container_name = get_container_name(task_id)

        if not self.docker_available:
            return False

        try:
            if self.api_mode == "sdk":
                try:
                    self.client.containers.get(container_name)
                    return True
                except Exception:
                    return False
            # CLI mode - check if container exists
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"name={container_name}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() == container_name
        except Exception:
            return False

    async def check_image_exists(self) -> bool:
        """Check if the Docker image exists locally."""
        if not self.docker_available:
            return False

        try:
            if self.api_mode == "sdk":
                try:
                    self.client.images.get(self.image_name)
                    return True
                except Exception:
                    return False
            # CLI mode - check if image exists
            result = subprocess.run(
                ["docker", "images", "-q", self.image_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    async def check_active_image_pull(self) -> bool:
        """Check if there's an active image pull in progress."""
        if not self.docker_available:
            return False

        try:
            if self.api_mode == "sdk":
                # In SDK mode, we can't easily detect active pulls
                # Return False and let the caller handle it
                return False
            # CLI mode - check for active pulls
            result = subprocess.run(
                ["docker", "system", "df", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                # Check if there are any dangling images (might indicate pull in progress)
                result = subprocess.run(
                    ["docker", "images", "--filter", "dangling=true", "-q"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return bool(result.stdout.strip())
            return False
        except Exception:
            return False

    async def get_container_startup_progress(self, task_id: int) -> Dict[str, Any]:
        """Get detailed container startup progress information."""
        container_name = get_container_name(task_id)
        progress = {
            "task_id": task_id,
            "container_name": container_name,
            "container_exists": False,
            "image_exists": False,
            "active_pull": False,
            "status": "unknown",
            "message": "",
            "timestamp": format_iso(utc_now()),
        }

        if not self.docker_available:
            progress["status"] = "docker_unavailable"
            progress["message"] = "Docker is not available"
            return progress

        try:
            # Check container existence
            progress["container_exists"] = await self.check_container_exists(task_id)

            if progress["container_exists"]:
                progress["status"] = "running"
                progress["message"] = f"Container {container_name} is running"
                return progress

            # Check image existence
            progress["image_exists"] = await self.check_image_exists()

            if not progress["image_exists"]:
                progress["status"] = "pulling_image"
                progress["message"] = f"Pulling Docker image {self.image_name}"

                # Check for active pull
                progress["active_pull"] = await self.check_active_image_pull()
                if progress["active_pull"]:
                    progress["message"] += " (pull in progress)"
            else:
                progress["status"] = "creating_container"
                progress["message"] = f"Image exists, creating container {container_name}"

        except Exception as e:
            progress["status"] = "error"
            progress["message"] = f"Error checking progress: {str(e)}"

        return progress
