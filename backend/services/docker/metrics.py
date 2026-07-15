"""
Real-time container resource metrics collection.

Scope:
- Collect CPU, memory, network, and storage metrics from running containers.
- Support SDK stats path, CLI fallback path, and simulation fallback.
- Trigger opportunistic VPN health checks as a best-effort side-effect by
  delegating detection logic to vpn_service.

Boundary:
- Does NOT create/start/stop/remove containers (that is lifecycle.py).
- Does NOT retrieve logs/startup progress (that is logs.py).
- Does NOT execute ad-hoc commands or PTY sessions (that is exec.py).
- Does NOT own VPN detection logic; it only delegates to VPNService.
"""

import logging
import random
import subprocess
import time
from typing import Any, Dict

from backend.core.time_utils import format_iso, utc_now

from ..container_utils import get_container_name
from .client import DockerClient

# Keep logger name aligned with monolith for caplog/patch compatibility.
logger = logging.getLogger("backend.services.unified_docker_service")


class ContainerMetrics:
    """Container resource metrics collection and fallback simulation."""

    def __init__(self, client: DockerClient):
        self._client = client
        self.client = client.client
        self.docker_available = client.docker_available
        self.api_mode = client.api_mode
        self.image_name = client.image_name
        self.containers = client.containers

        # Compatibility seams for legacy patch targets from unified_docker_service.
        self._get_container_name = get_container_name
        self._subprocess_run = subprocess.run
        self._time_time = time.time
        self._random_uniform = random.uniform
        self._format_iso = format_iso
        self._utc_now = utc_now

    async def get_container_metrics(self, task_id: int) -> Dict[str, Any]:
        """
        Get real-time resource metrics for a container.
        """
        container_name = self._get_container_name(task_id)

        try:
            if self.api_mode == "sdk" and self.client:
                # First check if container exists and is running
                try:
                    container = self.client.containers.get(container_name)
                    if container.status != "running":
                        logger.debug(
                            f"Container {container_name} is not running (status: {container.status})"
                        )
                        return {
                            "cpu_percent": 0.0,
                            "memory_usage_mb": 0.0,
                            "memory_limit_mb": 0.0,
                            "memory_percent": 0.0,
                            "disk_usage_mb": 0.0,
                            "disk_limit_mb": 0.0,
                            "disk_percent": 0.0,
                            "status": container.status,
                            "container_running": False,
                        }
                except Exception as e:
                    logger.debug(
                        f"Container {container_name} not found or not accessible: {e}"
                    )
                    return {
                        "cpu_percent": 0.0,
                        "memory_usage_mb": 0.0,
                        "memory_limit_mb": 0.0,
                        "memory_percent": 0.0,
                        "disk_usage_mb": 0.0,
                        "disk_limit_mb": 0.0,
                        "disk_percent": 0.0,
                        "status": "not_found",
                        "container_running": False,
                    }
                stats = container.stats(stream=False)

                # Calculate CPU percentage with safe error handling
                cpu_percent = 0.0
                try:
                    cpu_stats = stats.get("cpu_stats", {})
                    precpu_stats = stats.get("precpu_stats", {})

                    # Get CPU usage data safely
                    current_cpu = cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
                    previous_cpu = precpu_stats.get("cpu_usage", {}).get(
                        "total_usage", 0
                    )
                    current_system = cpu_stats.get("system_cpu_usage", 0)
                    previous_system = precpu_stats.get("system_cpu_usage", 0)

                    # Calculate deltas
                    cpu_delta = current_cpu - previous_cpu
                    system_delta = current_system - previous_system

                    # Get number of CPUs - fallback to 1 if percpu_usage not available
                    online_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [1]))
                    if online_cpus == 0:
                        online_cpus = 1

                    # Calculate CPU percentage
                    if system_delta > 0 and cpu_delta >= 0:
                        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0
                        cpu_percent = min(cpu_percent, 100.0)  # Cap at 100%
                except Exception as e:
                    logger.warning(f"CPU calculation error for task {task_id}: {e}")
                    cpu_percent = 0.0

                # Memory usage with safe error handling - use free command for accurate system memory
                memory_usage_mb = 0.0
                memory_limit_mb = 8192.0  # Default 8GB limit
                memory_percent = 0.0

                try:
                    # Try multiple approaches to get actual memory usage from container
                    memory_success = False

                    # Method 1: Try /proc/meminfo (more reliable in containers)
                    try:
                        result = container.exec_run(
                            "cat /proc/meminfo | head -3", stdout=True, stderr=True
                        )
                        if result.exit_code == 0 and result.output:
                            meminfo_output = result.output.decode().strip()
                            logger.info(
                                f"Meminfo output for task {task_id}: {meminfo_output}"
                            )

                            # Parse /proc/meminfo output
                            lines = meminfo_output.split("\n")
                            mem_total_kb = 0
                            mem_available_kb = 0

                            for line in lines:
                                if "MemTotal:" in line:
                                    mem_total_kb = int(line.split()[1])
                                elif "MemAvailable:" in line:
                                    mem_available_kb = int(line.split()[1])
                                elif "MemFree:" in line and mem_available_kb == 0:
                                    # Fallback to MemFree if MemAvailable not available
                                    mem_available_kb = int(line.split()[1])

                            if mem_total_kb > 0:
                                memory_limit_mb = mem_total_kb / 1024
                                memory_used_kb = mem_total_kb - mem_available_kb
                                memory_usage_mb = memory_used_kb / 1024
                                memory_percent = (memory_used_kb / mem_total_kb) * 100.0
                                memory_success = True
                                logger.info(
                                    "Successfully parsed /proc/meminfo for task "
                                    f"{task_id}: {memory_usage_mb:.2f}MB/{memory_limit_mb:.2f}MB "
                                    f"({memory_percent:.2f}%)"
                                )
                    except Exception as meminfo_error:
                        logger.warning(
                            f"Failed to read /proc/meminfo for task {task_id}: {meminfo_error}"
                        )

                    # Method 2: Try free command if meminfo failed
                    if not memory_success:
                        try:
                            result = container.exec_run("free -m", stdout=True, stderr=True)
                            logger.info(
                                f"Free command for task {task_id}: exit_code={result.exit_code}"
                            )

                            if result.exit_code == 0 and result.output:
                                free_output = result.output.decode().strip()
                                logger.info(f"Free output for task {task_id}: {free_output}")

                                lines = free_output.split("\n")
                                for line in lines:
                                    if line.startswith("Mem:"):
                                        parts = line.split()
                                        if len(parts) >= 3:
                                            memory_limit_mb = float(parts[1])
                                            memory_usage_mb = float(parts[2])
                                            memory_percent = (
                                                memory_usage_mb / memory_limit_mb
                                            ) * 100.0
                                            memory_success = True
                                            logger.info(
                                                "Successfully parsed free output for task "
                                                f"{task_id}: {memory_usage_mb}MB/{memory_limit_mb}MB "
                                                f"({memory_percent}%)"
                                            )
                                            break
                        except Exception as free_error:
                            logger.warning(
                                f"Free command failed for task {task_id}: {free_error}"
                            )

                    # If both methods failed, raise exception to use fallback
                    if not memory_success:
                        raise ValueError("All memory detection methods failed")
                except Exception as e:
                    logger.warning(f"Memory calculation error for task {task_id}: {e}")
                    # Final fallback
                    memory_usage_mb = 0.0
                    memory_limit_mb = 8192.0
                    memory_percent = 0.0

                # Network stats with safe error handling
                rx_bytes = 0
                tx_bytes = 0

                try:
                    networks = stats.get("networks", {})
                    if networks:
                        rx_bytes = sum(net.get("rx_bytes", 0) for net in networks.values())
                        tx_bytes = sum(net.get("tx_bytes", 0) for net in networks.values())
                except Exception as e:
                    logger.warning(
                        f"Network stats calculation error for task {task_id}: {e}"
                    )
                    rx_bytes = 0
                    tx_bytes = 0

                # Storage stats - get real container filesystem usage
                storage_used_bytes = 0
                storage_size_root_fs = 10 * 1024 * 1024 * 1024  # 10GB default

                try:
                    # Method 1: Try Docker container attributes first
                    container.reload()  # Ensure fresh data
                    size_rw = container.attrs.get("SizeRw", 0) or 0
                    size_root_fs = container.attrs.get("SizeRootFs", 0) or 0

                    if size_rw > 0:
                        storage_used_bytes = size_rw
                        storage_size_root_fs = (
                            size_root_fs if size_root_fs > 0 else storage_size_root_fs
                        )
                    else:
                        # Method 2: Fallback to df command if attributes unavailable
                        try:
                            exec_result = container.exec_run(
                                "df -B1 /",  # Get bytes usage for root filesystem
                                privileged=False,
                            )

                            if exec_result.exit_code == 0:
                                df_output = exec_result.output.decode("utf-8")
                                lines = df_output.strip().split("\n")

                                # Parse df output: filesystem blocks used available use% mounted_on
                                if len(lines) > 1:
                                    parts = lines[1].split()
                                    if len(parts) >= 3:
                                        try:
                                            # Third column is used bytes
                                            storage_used_bytes = int(parts[2])
                                            # Second column is total size
                                            if len(parts) >= 2:
                                                storage_size_root_fs = int(parts[1])
                                        except (ValueError, IndexError):
                                            logger.warning(
                                                f"Failed to parse df output for task {task_id}"
                                            )
                        except Exception as df_error:
                            logger.warning(
                                f"Failed to execute df command for task {task_id}: {df_error}"
                            )
                            # Fallback to reasonable estimate
                            storage_used_bytes = 100 * 1024 * 1024  # 100MB estimate

                except Exception as e:
                    logger.warning(f"Storage stats calculation error for task {task_id}: {e}")
                    storage_used_bytes = 100 * 1024 * 1024  # 100MB fallback

                storage_used_mb = storage_used_bytes / (1024 * 1024)
                storage_used_gb = storage_used_bytes / (1024 * 1024 * 1024)

                # Opportunistically detect VPN state and broadcast via delegated service.
                try:
                    from backend.database import SessionLocal
                    from backend.services.vpn_service import VPNService

                    db = SessionLocal()
                    try:
                        vpn_svc = VPNService(db)
                        await vpn_svc.check_container_vpn_health(task_id, container)
                    finally:
                        db.close()
                except Exception:
                    pass

                return {
                    "cpu_percent": round(cpu_percent, 2),
                    "memory_usage_mb": round(memory_usage_mb, 2),
                    "memory_limit_mb": round(memory_limit_mb, 2),
                    "memory_percent": round(memory_percent, 2),
                    "storage": {
                        "used_mb": round(storage_used_mb, 2),
                        "used_bytes": storage_used_bytes,
                        "size_root_fs": storage_size_root_fs,
                        "used_gb": round(storage_used_gb, 3),
                    },
                    "network": {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes},
                    "timestamp": self._format_iso(self._utc_now()),
                }
            else:
                # Fallback to CLI-based metrics collection
                cmd = [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "table {{.CPUPerc}},{{.MemUsage}},{{.MemPerc}}",
                    container_name,
                ]
                result = self._subprocess_run(
                    cmd, capture_output=True, text=True, timeout=10
                )

                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    if len(lines) > 1:  # Skip header
                        data = lines[1].split(",")
                        cpu_percent = float(data[0].replace("%", ""))
                        mem_usage = data[1].split("/")[0].strip()
                        mem_percent = float(data[2].replace("%", ""))

                        # Parse memory usage (e.g., "123.4MiB")
                        if "MiB" in mem_usage:
                            memory_usage_mb = float(mem_usage.replace("MiB", ""))
                        elif "GiB" in mem_usage:
                            memory_usage_mb = float(mem_usage.replace("GiB", "")) * 1024
                        else:
                            memory_usage_mb = 512  # Default

                        return {
                            "cpu_percent": cpu_percent,
                            "memory_usage_mb": memory_usage_mb,
                            "memory_limit_mb": 8192,  # 8GB default
                            "memory_percent": mem_percent,
                            "storage": {
                                "used_mb": 100,
                                "used_bytes": 100 * 1024 * 1024,
                                "size_root_fs": 10 * 1024 * 1024 * 1024,
                                "used_gb": 0.1,
                            },
                            "network": {
                                "rx_bytes": 1024 * 1024,  # 1MB
                                "tx_bytes": 512 * 1024,  # 512KB
                            },
                            "timestamp": self._format_iso(self._utc_now()),
                        }

                # Fallback simulation data
                return {
                    "cpu_percent": 15.5,
                    "memory_usage_mb": 1024,
                    "memory_limit_mb": 8192,
                    "memory_percent": 12.5,
                    "storage": {
                        "used_mb": 250,
                        "used_bytes": 250 * 1024 * 1024,
                        "size_root_fs": 10 * 1024 * 1024 * 1024,
                        "used_gb": 0.25,
                    },
                    "network": {
                        "rx_bytes": 2 * 1024 * 1024,
                        "tx_bytes": 1024 * 1024,
                    },
                    "timestamp": self._format_iso(self._utc_now()),
                }

        except Exception as e:
            logger.error(f"Failed to get container metrics for task {task_id}: {e}")
            # Return realistic simulated metrics when Docker is unavailable

            # Generate realistic varying metrics based on time
            base_time = int(self._time_time()) % 60
            cpu_base = 15 + (base_time % 30)  # 15-45% CPU
            memory_base = 800 + (base_time % 400)  # 800-1200MB memory

            # Add some randomness for realism
            cpu_percent = round(cpu_base + self._random_uniform(-5, 10), 2)
            memory_mb = round(memory_base + self._random_uniform(-50, 100), 2)
            memory_percent = round((memory_mb / 8192) * 100, 2)

            return {
                "cpu_percent": max(0, min(cpu_percent, 100)),
                "memory_usage_mb": memory_mb,
                "memory_limit_mb": 8192.0,
                "memory_percent": memory_percent,
                "storage": {
                    "used_mb": round(200 + self._random_uniform(50, 150), 2),
                    "used_bytes": int(
                        (200 + self._random_uniform(50, 150)) * 1024 * 1024
                    ),
                    "size_root_fs": 10 * 1024 * 1024 * 1024,
                    "used_gb": round((200 + self._random_uniform(50, 150)) / 1024, 3),
                },
                "network": {
                    "rx_bytes": int(self._random_uniform(1024 * 1024, 5 * 1024 * 1024)),
                    "tx_bytes": int(self._random_uniform(512 * 1024, 2 * 1024 * 1024)),
                },
                "timestamp": self._format_iso(self._utc_now()),
            }
