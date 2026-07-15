"""Shared environment-info shape and parsers for task runtimes.

This module is backend-free so local Docker, the customer runner, and prompt
context loaders can share one canonical environment-info structure without
crossing management-plane or execution-plane ownership boundaries.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

ENV_INFO_FILENAME = "env_info.json"


def create_empty_env_info(*, collected_at: str | None = None) -> dict[str, Any]:
    """Create the canonical runtime environment info structure."""
    return {
        "collected_at": collected_at,
        "hostname": "unknown",
        "os": {
            "name": "unknown",
            "version": "unknown",
            "kernel": "unknown",
        },
        "network": {
            "interfaces": [],
            "default_gateway": None,
            "dns_servers": [],
        },
        "routes": [],
        "collection_errors": [],
    }


def collect_environment_info_from_executor(
    execute: Callable[[str], str],
    *,
    collected_at: str | None = None,
) -> dict[str, Any]:
    """Collect canonical environment info through a command executor callback."""
    env_info = create_empty_env_info(
        collected_at=collected_at or datetime.now(tz=UTC).isoformat()
    )
    errors: list[str] = []

    hostname = _safe_execute(execute, "hostname")
    if hostname:
        env_info["hostname"] = hostname
    else:
        errors.append("Failed to get hostname")

    os_release = _safe_execute(execute, "cat /etc/os-release")
    if os_release:
        env_info["os"] = parse_os_release(os_release)
    else:
        errors.append("Failed to read /etc/os-release")

    kernel = _safe_execute(execute, "uname -r")
    if kernel:
        env_info["os"]["kernel"] = kernel
    else:
        errors.append("Failed to get kernel version")

    ip_addr_output = _safe_execute(execute, "ip addr show")
    if ip_addr_output:
        env_info["network"]["interfaces"] = parse_ip_addr(ip_addr_output)
    else:
        errors.append("Failed to get network interfaces")

    routes_output = _safe_execute(execute, "ip route")
    if routes_output:
        env_info["routes"] = parse_routes(routes_output)
        for route in env_info["routes"]:
            if route.get("destination") == "default":
                env_info["network"]["default_gateway"] = route.get("gateway")
                break
    else:
        errors.append("Failed to get routing table")

    dns_output = _safe_execute(execute, "cat /etc/resolv.conf")
    if dns_output:
        env_info["network"]["dns_servers"] = parse_dns(dns_output)
    else:
        errors.append("Failed to read /etc/resolv.conf")

    env_info["collection_errors"] = errors
    return env_info


def parse_os_release(content: str) -> dict[str, str]:
    """Parse `/etc/os-release` content into the canonical OS mapping."""
    info = {
        "name": "unknown",
        "version": "unknown",
        "kernel": "unknown",
    }
    for line in content.split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip('"\'')
        if key == "PRETTY_NAME":
            info["name"] = value
        elif key == "VERSION_ID":
            info["version"] = value
    return info


def parse_ip_addr(output: str) -> list[dict[str, str]]:
    """Parse `ip addr show` output into interface dictionaries."""
    interfaces: list[dict[str, str]] = []
    current_iface: dict[str, str] | None = None
    for line in output.split("\n"):
        if line and not line.startswith(" "):
            parts = line.split(":")
            if len(parts) >= 2:
                name = parts[1].strip().split("@")[0]
                state = "UP" if "UP" in line else "DOWN"
                current_iface = {"name": name, "state": state}
                interfaces.append(current_iface)
        elif current_iface and "inet " in line and "inet6" not in line:
            parts = line.strip().split()
            if len(parts) >= 2:
                current_iface["ipv4"] = parts[1]
    return interfaces


def parse_routes(output: str) -> list[dict[str, Any]]:
    """Parse `ip route` output into route dictionaries."""
    routes: list[dict[str, Any]] = []
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        route: dict[str, Any] = {
            "destination": parts[0],
            "gateway": None,
            "interface": None,
        }
        if "via" in parts:
            try:
                via_idx = parts.index("via")
                if via_idx + 1 < len(parts):
                    route["gateway"] = parts[via_idx + 1]
            except (ValueError, IndexError):
                pass
        if "dev" in parts:
            try:
                dev_idx = parts.index("dev")
                if dev_idx + 1 < len(parts):
                    route["interface"] = parts[dev_idx + 1]
            except (ValueError, IndexError):
                pass
        routes.append(route)
    return routes


def parse_dns(content: str) -> list[str]:
    """Parse `/etc/resolv.conf` nameserver entries."""
    servers: list[str] = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) >= 2:
                servers.append(parts[1])
    return servers


def _safe_execute(execute: Callable[[str], str], command: str) -> str:
    try:
        return str(execute(command) or "").strip()
    except Exception:
        return ""


__all__ = [
    "ENV_INFO_FILENAME",
    "collect_environment_info_from_executor",
    "create_empty_env_info",
    "parse_dns",
    "parse_ip_addr",
    "parse_os_release",
    "parse_routes",
]
