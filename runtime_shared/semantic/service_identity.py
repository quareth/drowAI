"""Runtime-safe service socket identity helpers.

This module is the single authority for service.socket keys across runtime
tools and backend knowledge projection. Socket identity is transport-level
only; application protocols are descriptive metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any

_SERVICE_SOCKET_PATTERN = re.compile(
    r"^service\.socket:(?P<ip>[^/]+)/(?P<protocol>[a-z0-9_.+-]+)/(?P<port>\d+)$",
    re.IGNORECASE,
)
_APPLICATION_PROTOCOL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,31}$", re.IGNORECASE)

TRANSPORT_PROTOCOLS: frozenset[str] = frozenset({"tcp", "udp"})

APPLICATION_PROTOCOL_TRANSPORTS: dict[str, str] = {
    "ftp": "tcp",
    "ftps": "tcp",
    "ssh": "tcp",
    "telnet": "tcp",
    "smtp": "tcp",
    "smtps": "tcp",
    "pop3": "tcp",
    "pop3s": "tcp",
    "imap": "tcp",
    "imaps": "tcp",
    "http": "tcp",
    "https": "tcp",
    "rdp": "tcp",
    "smb": "tcp",
    "ldap": "tcp",
    "ldaps": "tcp",
    "mysql": "tcp",
    "postgresql": "tcp",
    "postgres": "tcp",
    "redis": "tcp",
    "mongodb": "tcp",
    "mssql": "tcp",
    "winrm": "tcp",
    "vnc": "tcp",
    "ntp": "udp",
    "snmp": "udp",
    "dhcp": "udp",
    "mdns": "udp",
    "dns": "udp",
}

DEFAULT_APPLICATION_PORTS: dict[str, int] = {
    "ftp": 21,
    "ssh": 22,
    "telnet": 23,
    "smtp": 25,
    "dns": 53,
    "http": 80,
    "pop3": 110,
    "ntp": 123,
    "imap": 143,
    "snmp": 161,
    "ldap": 389,
    "https": 443,
    "smb": 445,
    "smtps": 465,
    "ldaps": 636,
    "imaps": 993,
    "pop3s": 995,
    "mssql": 1433,
    "mysql": 3306,
    "rdp": 3389,
    "postgresql": 5432,
    "postgres": 5432,
    "vnc": 5900,
    "redis": 6379,
    "mongodb": 27017,
}


@dataclass(frozen=True)
class ServiceSocketParts:
    """Parsed canonical service socket key parts."""

    ip: str
    protocol: str
    port: int

    @property
    def subject_key(self) -> str:
        """Return this socket as a canonical service.socket key."""
        return build_service_socket_key(ip=self.ip, protocol=self.protocol, port=self.port)


def normalize_ip(value: Any) -> str | None:
    """Normalize an IP address token, returning None for invalid input."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def normalize_port(value: Any) -> int | None:
    """Normalize a TCP/UDP port, returning None for invalid input."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    return port


def normalize_transport_protocol(value: Any, *, default: str | None = "tcp") -> str | None:
    """Normalize socket transport protocol values."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized in TRANSPORT_PROTOCOLS:
        return normalized
    return None


def normalize_application_protocol(value: Any) -> str | None:
    """Normalize application protocol/service names used only as metadata."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if not _APPLICATION_PROTOCOL_PATTERN.fullmatch(normalized):
        return None
    return normalized


def infer_transport_from_application_protocol(value: Any) -> str | None:
    """Return default transport for a known application protocol."""
    protocol = normalize_application_protocol(value)
    if protocol is None:
        return None
    return APPLICATION_PROTOCOL_TRANSPORTS.get(protocol)


def default_port_for_application_protocol(value: Any) -> int | None:
    """Return the default port for a known application protocol."""
    protocol = normalize_application_protocol(value)
    if protocol is None:
        return None
    return DEFAULT_APPLICATION_PORTS.get(protocol)


def build_service_socket_key(*, ip: Any, protocol: Any, port: Any) -> str:
    """Build a strict canonical service.socket key."""
    normalized_ip = normalize_ip(ip)
    if normalized_ip is None:
        raise ValueError(f"invalid ip address: {ip}")
    normalized_protocol = normalize_transport_protocol(protocol, default=None)
    if normalized_protocol is None:
        raise ValueError("protocol must be tcp or udp")
    normalized_port = normalize_port(port)
    if normalized_port is None:
        raise ValueError("port must be between 1 and 65535")
    return f"service.socket:{normalized_ip}/{normalized_protocol}/{normalized_port}"


def parse_service_socket_key(value: Any) -> ServiceSocketParts | None:
    """Parse a strict canonical service.socket key."""
    match = _SERVICE_SOCKET_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        return None
    protocol = normalize_transport_protocol(match.group("protocol"), default=None)
    port = normalize_port(match.group("port"))
    normalized_ip = normalize_ip(match.group("ip"))
    if protocol is None or port is None or normalized_ip is None:
        return None
    return ServiceSocketParts(ip=normalized_ip, protocol=protocol, port=port)


def require_service_socket_key(value: Any) -> ServiceSocketParts:
    """Parse a canonical service.socket key or raise ValueError."""
    parsed = parse_service_socket_key(value)
    if parsed is None:
        raise ValueError("service.socket subject_key must be service.socket:<ip>/<tcp|udp>/<port>")
    return parsed


def build_service_socket_key_from_application(
    *,
    ip: Any,
    application_protocol: Any,
    port: Any = None,
    transport: Any = None,
) -> str | None:
    """Build a canonical socket key from app protocol metadata when known."""
    app_protocol = normalize_application_protocol(application_protocol)
    if app_protocol is None:
        return None
    resolved_transport = normalize_transport_protocol(transport, default=None)
    if resolved_transport is None:
        resolved_transport = infer_transport_from_application_protocol(app_protocol)
    if resolved_transport is None:
        return None
    resolved_port = normalize_port(port)
    if resolved_port is None:
        resolved_port = default_port_for_application_protocol(app_protocol)
    if resolved_port is None:
        return None
    try:
        return build_service_socket_key(
            ip=ip,
            protocol=resolved_transport,
            port=resolved_port,
        )
    except ValueError:
        return None
