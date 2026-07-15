"""Collect topology-aware Management and tenant Runner network information.

Management interfaces, routing, and DNS are observed locally. Runner addresses
come from the latest tenant-scoped control-channel connection seen by
Management, keeping the read model truthful across local, NAT, and distributed
deployments.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import platform
import socket
import struct
import subprocess
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config.deployment_topology import get_deployment_profile_state
from backend.core.network_utils import normalize_ip_address
from backend.core.time_utils import utc_now
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection
from backend.schemas.network_overview import (
    ManagementNetworkOverview,
    NetworkInterfaceAddress,
    NetworkOverviewResponse,
    RunnerNetworkOverview,
)
from backend.services.platform.management_url import ManagementUrlError, ManagementUrlService


_LINUX_ROUTE_TABLE_PATH = Path("/proc/net/route")
_RESOLVER_CONFIG_PATH = Path("/etc/resolv.conf")
_ROUTE_COMMAND_TIMEOUT_SECONDS = 2
_LINUX_DEFAULT_ROUTE_DESTINATION = "00000000"
_LINUX_ROUTE_GATEWAY_FLAG = 0x2


@dataclass(frozen=True, slots=True)
class DefaultGateway:
    """Default gateway address and the interface that owns its route."""

    ip_address: str
    interface_name: str | None


@dataclass(frozen=True, slots=True)
class HostNetworkSnapshot:
    """Locally observed Management networking without deployment metadata."""

    primary_ip: str | None
    interfaces: tuple[NetworkInterfaceAddress, ...]
    gateway: DefaultGateway | None
    dns_servers: tuple[str, ...]


class HostNetworkDiscovery:
    """Read active Management interfaces, default routing, and DNS state."""

    def __init__(
        self,
        *,
        interface_reader: Callable[[], Mapping[str, list[Any]]] = psutil.net_if_addrs,
        stats_reader: Callable[[], Mapping[str, Any]] = psutil.net_if_stats,
        gateway_reader: Callable[[], DefaultGateway | None] | None = None,
        dns_reader: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._interface_reader = interface_reader
        self._stats_reader = stats_reader
        self._gateway_reader = gateway_reader or _read_default_gateway
        self._dns_reader = dns_reader or _read_dns_servers

    def collect(self) -> HostNetworkSnapshot:
        """Return deterministic active interface, route, and resolver data."""

        stats = self._stats_reader()
        interfaces: list[NetworkInterfaceAddress] = []
        for interface_name, addresses in self._interface_reader().items():
            interface_stats = stats.get(interface_name)
            if interface_stats is not None and not bool(interface_stats.isup):
                continue
            for item in addresses:
                family = _family_name(item.family)
                if family is None:
                    continue
                normalized = normalize_ip_address(item.address)
                if normalized is None:
                    continue
                parsed = ipaddress.ip_address(normalized)
                if parsed.is_unspecified or parsed.is_link_local:
                    continue
                interfaces.append(
                    NetworkInterfaceAddress(
                        interface_name=interface_name,
                        address=normalized,
                        family=family,
                        prefix_length=_prefix_length(item.netmask, family=family),
                        is_loopback=parsed.is_loopback,
                    )
                )

        interfaces.sort(
            key=lambda item: (
                item.is_loopback,
                item.interface_name,
                item.family != "ipv4",
                item.address,
            )
        )
        gateway = self._gateway_reader()
        primary_ip = _select_primary_ip(interfaces, gateway=gateway)
        return HostNetworkSnapshot(
            primary_ip=primary_ip,
            interfaces=tuple(interfaces),
            gateway=gateway,
            dns_servers=tuple(dict.fromkeys(self._dns_reader())),
        )


class NetworkOverviewService:
    """Compose host discovery with tenant-scoped Runner connection records."""

    def __init__(
        self,
        db: Session,
        *,
        host_discovery: HostNetworkDiscovery | None = None,
        management_url_service: ManagementUrlService | None = None,
        deployment_profile_resolver: Callable[[], Any] = get_deployment_profile_state,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self._db = db
        self._host_discovery = host_discovery or HostNetworkDiscovery()
        self._management_urls = management_url_service or ManagementUrlService()
        self._deployment_profile_resolver = deployment_profile_resolver
        self._now_provider = now_provider

    def collect(self, *, tenant_id: int, request: Request) -> NetworkOverviewResponse:
        """Return the current deployment and tenant Runner network projection."""

        now = self._now_provider()
        host = self._host_discovery.collect()
        management_url, management_host, url_source = self._resolve_management_url(request=request)
        profile = self._deployment_profile_resolver().profile.value

        return NetworkOverviewResponse(
            deployment_profile=str(profile),
            management=ManagementNetworkOverview(
                advertised_url=management_url,
                advertised_host=management_host,
                advertised_url_source=url_source,
                primary_ip=host.primary_ip,
                interfaces=list(host.interfaces),
                gateway_ip=host.gateway.ip_address if host.gateway else None,
                gateway_interface=host.gateway.interface_name if host.gateway else None,
                dns_servers=list(host.dns_servers),
            ),
            runners=self._runner_overviews(tenant_id=tenant_id, now=now),
            collected_at=now,
        )

    def _resolve_management_url(self, *, request: Request) -> tuple[str | None, str | None, str]:
        try:
            resolved = self._management_urls.resolve(request=request)
        except ManagementUrlError:
            return None, None, "unavailable"
        parsed = urlparse(resolved.management_url)
        return resolved.management_url, parsed.hostname, resolved.source

    def _runner_overviews(self, *, tenant_id: int, now: datetime) -> list[RunnerNetworkOverview]:
        runner_rows = self._db.execute(
            select(Runner, ExecutionSite)
            .join(ExecutionSite, ExecutionSite.id == Runner.execution_site_id)
            .where(Runner.tenant_id == tenant_id, ExecutionSite.tenant_id == tenant_id)
            .order_by(ExecutionSite.name.asc(), Runner.name.asc(), Runner.id.asc())
        ).all()
        runner_ids = [runner.id for runner, _site in runner_rows]
        latest_connections: dict[Any, RunnerConnection] = {}
        if runner_ids:
            connections = self._db.execute(
                select(RunnerConnection)
                .where(
                    RunnerConnection.tenant_id == tenant_id,
                    RunnerConnection.runner_id.in_(runner_ids),
                )
                .order_by(RunnerConnection.last_seen_at.desc(), RunnerConnection.id.desc())
            ).scalars()
            for connection in connections:
                latest_connections.setdefault(connection.runner_id, connection)

        results: list[RunnerNetworkOverview] = []
        for runner, site in runner_rows:
            connection = latest_connections.get(runner.id)
            results.append(
                RunnerNetworkOverview(
                    id=runner.id,
                    name=runner.name,
                    site_id=site.id,
                    site_name=site.name,
                    site_network_label=site.network_label,
                    status=runner.status,
                    connection_status=_connection_status(connection, now=now),
                    observed_ip=connection.remote_ip_address if connection else None,
                    observed_at=connection.last_seen_at if connection else None,
                )
            )
        return results


def _family_name(family: Any) -> str | None:
    if family == socket.AF_INET:
        return "ipv4"
    if family == socket.AF_INET6:
        return "ipv6"
    return None


def _prefix_length(netmask: object, *, family: str) -> int | None:
    normalized = str(netmask or "").split("%", 1)[0].strip()
    if not normalized:
        return None
    try:
        zero_address = "0.0.0.0" if family == "ipv4" else "::"
        return ipaddress.ip_network(f"{zero_address}/{normalized}", strict=False).prefixlen
    except ValueError:
        return None


def _select_primary_ip(
    interfaces: list[NetworkInterfaceAddress],
    *,
    gateway: DefaultGateway | None,
) -> str | None:
    candidates = [item for item in interfaces if not item.is_loopback]
    if gateway and gateway.interface_name:
        routed = [item for item in candidates if item.interface_name == gateway.interface_name]
        if routed:
            return sorted(routed, key=lambda item: item.family != "ipv4")[0].address
    if candidates:
        return sorted(candidates, key=lambda item: item.family != "ipv4")[0].address
    return interfaces[0].address if interfaces else None


def _read_default_gateway() -> DefaultGateway | None:
    system_name = platform.system().lower()
    if system_name == "linux":
        return _read_linux_default_gateway(_LINUX_ROUTE_TABLE_PATH)
    if system_name in {"darwin", "freebsd", "openbsd", "netbsd"}:
        return _read_route_command_gateway()
    return None


def _read_linux_default_gateway(path: Path) -> DefaultGateway | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[1:]
    except (OSError, UnicodeError):
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[1] != _LINUX_DEFAULT_ROUTE_DESTINATION:
            continue
        try:
            flags = int(fields[3], 16)
            gateway_ip = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
        except (ValueError, OSError, struct.error):
            continue
        if flags & _LINUX_ROUTE_GATEWAY_FLAG and normalize_ip_address(gateway_ip):
            return DefaultGateway(ip_address=gateway_ip, interface_name=fields[0])
    return None


def _read_route_command_gateway() -> DefaultGateway | None:
    try:
        result = subprocess.run(
            ["route", "-n", "get", "default"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_ROUTE_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip().lower()] = value.strip()
    gateway_ip = normalize_ip_address(values.get("gateway"))
    if gateway_ip is None:
        return None
    return DefaultGateway(ip_address=gateway_ip, interface_name=values.get("interface") or None)


def _read_dns_servers() -> tuple[str, ...]:
    try:
        lines = _RESOLVER_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    servers: list[str] = []
    for line in lines:
        content = line.split("#", 1)[0].strip()
        fields = content.split()
        if len(fields) != 2 or fields[0].lower() != "nameserver":
            continue
        normalized = normalize_ip_address(fields[1])
        if normalized:
            servers.append(normalized)
    return tuple(dict.fromkeys(servers))


def _connection_status(connection: RunnerConnection | None, *, now: datetime) -> str:
    if connection is None:
        return "never_connected"
    lease_expires_at = connection.lease_expires_at
    if lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
    if connection.status == "active" and lease_expires_at > now:
        return "connected"
    return "disconnected"
