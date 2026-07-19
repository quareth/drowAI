"""Registered-target URL and DNS policy for guarded LLM provider egress.

Public routes require HTTPS and globally routable DNS. Operator-configured
local development routes are confined to loopback addresses.
"""

from __future__ import annotations

from ipaddress import ip_address
import re
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

from .types import LLMEgressNetworkScope, ValidatedEgressTarget

DNSResolver = Callable[[str, int], Iterable[str]]
_HTTP_SCHEME = "http"
_HTTPS_SCHEME = "https"
_HTTP_DEFAULT_PORT = 80
_HTTPS_DEFAULT_PORT = 443
_LOOPBACK_SCHEMES = frozenset({_HTTP_SCHEME, _HTTPS_SCHEME})
_PUBLIC_SCHEMES = frozenset({_HTTPS_SCHEME})


class EgressPolicyError(ValueError):
    """Sanitized failure raised when an endpoint is not safe for guarded egress."""


class FixedProviderEgressPolicy:
    """Validate registered origins, paths, ports, and scoped DNS answers."""

    def __init__(self, *, dns_resolver: DNSResolver | None = None) -> None:
        self._dns_resolver = dns_resolver or _resolve_dns

    def validate_endpoint(
        self,
        endpoint: str,
        *,
        expected_host: str,
        allowed_ports: frozenset[int],
        allowed_path_prefixes: tuple[str, ...],
        network_scope: LLMEgressNetworkScope = LLMEgressNetworkScope.PUBLIC,
    ) -> ValidatedEgressTarget:
        """Validate a registry-owned endpoint immediately before use."""

        if (
            not isinstance(endpoint, str)
            or not endpoint
            or endpoint != endpoint.strip()
            or any(character.isspace() for character in endpoint)
        ):
            raise EgressPolicyError("Guarded endpoint is invalid")

        try:
            parsed = urlsplit(endpoint)
            explicit_port = parsed.port
        except ValueError:
            raise EgressPolicyError("Guarded endpoint is invalid") from None

        host = str(parsed.hostname or "").lower()
        allowed_schemes = (
            _LOOPBACK_SCHEMES
            if network_scope is LLMEgressNetworkScope.LOOPBACK
            else _PUBLIC_SCHEMES
        )
        default_port = (
            _HTTPS_DEFAULT_PORT
            if parsed.scheme == _HTTPS_SCHEME
            else _HTTP_DEFAULT_PORT
        )
        port = explicit_port or default_port
        if (
            parsed.scheme not in allowed_schemes
            or not host
            or host != expected_host.strip().lower()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in allowed_ports
        ):
            raise EgressPolicyError("Guarded endpoint violates fixed origin policy")

        path = parsed.path or "/"
        if not _path_is_allowed(path, allowed_path_prefixes):
            raise EgressPolicyError("Guarded endpoint violates fixed path policy")

        addresses = self._resolve_addresses(host, port, network_scope)
        return ValidatedEgressTarget(
            url=endpoint,
            scheme=parsed.scheme,
            host=host,
            port=port,
            path=path,
            resolved_addresses=addresses,
            network_scope=network_scope,
        )

    def revalidate(self, target: ValidatedEgressTarget) -> None:
        """Reject DNS answer changes between target validation and request send."""

        current = self._resolve_addresses(
            target.host,
            target.port,
            target.network_scope,
        )
        if current != target.resolved_addresses:
            raise EgressPolicyError("Guarded endpoint DNS answers changed before send")

    def _resolve_addresses(
        self,
        host: str,
        port: int,
        network_scope: LLMEgressNetworkScope,
    ) -> tuple[str, ...]:
        """Resolve and require addresses within the registered network scope."""

        try:
            raw_addresses = tuple(self._dns_resolver(host, port))
        except Exception:
            raise EgressPolicyError("Guarded endpoint DNS resolution failed") from None
        if not raw_addresses:
            raise EgressPolicyError(
                "Guarded endpoint DNS resolution returned no addresses"
            )

        normalized: set[str] = set()
        for raw_address in raw_addresses:
            try:
                address = ip_address(str(raw_address))
            except ValueError:
                raise EgressPolicyError(
                    "Guarded endpoint DNS answer is invalid"
                ) from None
            if network_scope is LLMEgressNetworkScope.LOOPBACK:
                if not address.is_loopback:
                    raise EgressPolicyError(
                        "Guarded endpoint DNS answer is not loopback"
                    )
            elif (
                not address.is_global
                or address.is_loopback
                or address.is_link_local
                or address.is_private
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                raise EgressPolicyError("Guarded endpoint DNS answer is not public")
            normalized.add(str(address))
        return tuple(sorted(normalized))


def _path_is_allowed(path: str, allowed_path_prefixes: tuple[str, ...]) -> bool:
    """Reject traversal/encoded separators and require a registered path prefix."""

    if not path.startswith("/") or "\\" in path or "//" in path:
        return False
    if re.search(r"%(?:2e|2f|5c)", path, flags=re.IGNORECASE):
        return False
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return False
    return any(path.startswith(prefix) for prefix in allowed_path_prefixes)


def _resolve_dns(host: str, port: int) -> tuple[str, ...]:
    """Resolve TCP addresses for the guarded endpoint host."""

    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(record[4][0] for record in records)


__all__ = [
    "DNSResolver",
    "EgressPolicyError",
    "FixedProviderEgressPolicy",
]
