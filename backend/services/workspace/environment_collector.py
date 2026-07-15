"""Environment information collection for Kali containers.

This module collects, stores, and formats container environment information
(network config, routes, OS details) for use in LLM prompts.

The environment info is:
1. Collected once when the container starts
2. Saved to workspace/<task_id>/env_info.json
3. Loaded into facts.metadata["environment_info"] for state persistence
4. Formatted as FULL (for planner) or COMPACT (for post-tool reasoning)

Follows workspace patterns from backend.services.workspace.manager.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from docker.models.containers import Container

from backend.config.workspace_config import WorkspaceConfig
from backend.core.time_utils import format_iso, utc_now
from runtime_shared.environment_info import (
    ENV_INFO_FILENAME,
    collect_environment_info_from_executor,
)
from runtime_shared.workspace_filesystem import WorkspaceFilesystem

logger = logging.getLogger("backend.services.environment_collector")

COLLECTION_TIMEOUT_SECONDS = 10


def collect_environment_info(container: "Container") -> Dict[str, Any]:
    """Collect environment information from a running container.
    
    Executes shell commands inside the container to gather network
    configuration, routing table, and OS information.
    
    Args:
        container: Docker container object (SDK mode).
        
    Returns:
        Dictionary with collected environment information.
        Partial data returned if some commands fail.
        
    Note:
        This function is non-blocking and handles errors gracefully.
        Failed commands are logged but don't raise exceptions.
    """
    env_info = collect_environment_info_from_executor(
        lambda command: _exec_command(container, command),
        collected_at=format_iso(utc_now()),
    )
    errors = list(env_info.get("collection_errors") or [])
    if errors:
        logger.warning(f"[ENV] Collection completed with errors: {errors}")
    else:
        logger.info("[ENV] Collection completed successfully")
    return env_info


def _exec_command(container: "Container", cmd: str) -> str:
    """Execute command in container and return stdout.
    
    Args:
        container: Docker container object.
        cmd: Shell command to execute.
        
    Returns:
        Command stdout as string, empty string on failure.
    """
    try:
        result = container.exec_run(cmd, demux=True)
        
        # demux=True returns (stdout, stderr) tuple
        if result.output:
            stdout = result.output[0] if isinstance(result.output, tuple) else result.output
            if stdout:
                return stdout.decode("utf-8", errors="replace").strip()
        
        return ""
        
    except Exception as e:
        logger.warning(f"[ENV] Failed to execute '{cmd}': {e}")
        return ""


# -----------------------------------------------------------------------------
# Persistence Functions
# -----------------------------------------------------------------------------

def save_environment_info(task_id: int, env_info: Dict[str, Any]) -> Path:
    """Save environment info to task workspace.
    
    Args:
        task_id: Task identifier.
        env_info: Environment info dictionary.
        
    Returns:
        Path to saved file.
        
    Raises:
        OSError: If file write fails.
    """
    workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)
    
    # Ensure workspace exists
    workspace_path.mkdir(parents=True, exist_ok=True)
    
    env_file = workspace_path / ENV_INFO_FILENAME
    
    try:
        WorkspaceFilesystem(workspace_path).write_bytes_atomic(
            ENV_INFO_FILENAME,
            json.dumps(env_info, indent=2).encode("utf-8"),
            mode=0o644,
        )
        logger.info(f"[ENV] Saved environment info for task {task_id}: {env_file}")
        return env_file
        
    except OSError as e:
        logger.error(f"[ENV] Failed to save environment info for task {task_id}: {e}")
        raise


def load_environment_info(task_id: int) -> Optional[Dict[str, Any]]:
    """Load environment info from task workspace.
    
    Args:
        task_id: Task identifier.
        
    Returns:
        Environment info dict, or None if not found/invalid.
    """
    workspace_path = WorkspaceConfig.get_task_workspace_path(task_id)

    try:
        payload = WorkspaceFilesystem(workspace_path).read_bytes(
            ENV_INFO_FILENAME,
            max_bytes=1024 * 1024,
        )
        env_info = json.loads(payload.decode("utf-8"))
        logger.debug(f"[ENV] Loaded environment info for task {task_id}")
        return env_info
    except FileNotFoundError:
        logger.debug(f"[ENV] No environment info found for task {task_id}")
        return None
    except UnicodeDecodeError as e:
        logger.warning(f"[ENV] Invalid encoding in env_info.json for task {task_id}: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"[ENV] Invalid JSON in env_info.json for task {task_id}: {e}")
        return None
    except OSError as e:
        logger.warning(f"[ENV] Failed to load environment info for task {task_id}: {e}")
        return None


# -----------------------------------------------------------------------------
# Formatting Functions
# -----------------------------------------------------------------------------

def format_environment_for_prompt(env_info: Optional[Dict[str, Any]]) -> str:
    """Format environment info for inclusion in LLM system prompt (FULL format).
    
    Use this for planner prompt where we need complete network picture.
    
    Args:
        env_info: Environment info dict, or None.
        
    Returns:
        Formatted multi-line string (~200 tokens).
        Returns empty string if env_info is None or invalid.
    """
    if not env_info:
        return ""
    
    try:
        hostname = env_info.get("hostname", "unknown")
        os_info = env_info.get("os", {})
        network = env_info.get("network", {})
        routes = env_info.get("routes", [])
        
        # Format OS info
        os_name = os_info.get("name", "unknown")
        os_version = os_info.get("version", "")
        os_kernel = os_info.get("kernel", "unknown")
        os_display = f"{os_name} {os_version}".strip() if os_version else os_name
        
        # Format network interfaces
        interfaces_lines: List[str] = []
        for iface in network.get("interfaces", []):
            name = iface.get("name", "?")
            ipv4 = iface.get("ipv4", "no IP")
            state = iface.get("state", "unknown")
            interfaces_lines.append(f"  - {name}: {ipv4} ({state})")
        
        interfaces_str = "\n".join(interfaces_lines) if interfaces_lines else "  - No interfaces found"
        
        # Format routes (limit to 5)
        routes_lines: List[str] = []
        for route in routes[:5]:
            dest = route.get("destination", "?")
            gw = route.get("gateway") or "direct"
            iface = route.get("interface", "?")
            routes_lines.append(f"  - {dest} via {gw} dev {iface}")
        
        routes_str = "\n".join(routes_lines) if routes_lines else "  - No routes found"
        
        # Format DNS
        dns_servers = network.get("dns_servers", [])
        dns_str = ", ".join(dns_servers) if dns_servers else "none"
        
        # Format gateway
        gateway = network.get("default_gateway") or "none"
        
        return f"""
**Container Environment:**
- Hostname: {hostname}
- OS: {os_display}
- Kernel: {os_kernel}

**Network Configuration:**
- Interfaces:
{interfaces_str}
- Default Gateway: {gateway}
- DNS Servers: {dns_str}

**Routing Table:**
{routes_str}
"""
    
    except Exception as e:
        logger.warning(f"[ENV] Failed to format environment info: {e}")
        return ""


def format_environment_compact(env_info: Optional[Dict[str, Any]]) -> str:
    """Format environment info as compact one-liner for reasoning prompts.
    
    Use this for post_tool_reasoning where we need minimal token usage
    but still want network awareness.
    
    Args:
        env_info: Environment info dict, or None.
        
    Returns:
        Compact single-line string (~50 tokens).
        Example: "eth0=172.17.0.2/16 | gw=172.17.0.1 | DNS=8.8.8.8"
        Returns empty string if env_info is None or invalid.
    """
    if not env_info:
        return ""
    
    try:
        network = env_info.get("network", {})
        parts: List[str] = []
        
        # Find primary interface (skip loopback)
        interfaces = network.get("interfaces", [])
        for iface in interfaces:
            name = iface.get("name", "")
            if name and name != "lo":  # Skip loopback
                ipv4 = iface.get("ipv4", "")
                if ipv4:
                    parts.append(f"{name}={ipv4}")
                    break
        
        # Add gateway
        gateway = network.get("default_gateway")
        if gateway:
            parts.append(f"gw={gateway}")
        
        # Add primary DNS (just the first one)
        dns_servers = network.get("dns_servers", [])
        if dns_servers:
            parts.append(f"DNS={dns_servers[0]}")
        
        return " | ".join(parts) if parts else ""
    
    except Exception as e:
        logger.warning(f"[ENV] Failed to format compact environment info: {e}")
        return ""


# -----------------------------------------------------------------------------
# Exports
# -----------------------------------------------------------------------------

__all__ = [
    "ENV_INFO_FILENAME",
    "collect_environment_info",
    "save_environment_info",
    "load_environment_info",
    "format_environment_for_prompt",
    "format_environment_compact",
]
