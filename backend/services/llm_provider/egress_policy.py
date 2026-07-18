"""Fixed-target URL and DNS policy for guarded LLM provider egress.

Phase 1 permits only HTTPS targets selected by the code-owned operation
registry. User-configured endpoints and restricted network zones remain closed.
"""

from __future__ import annotations

from ipaddress import ip_address
import re
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

from .types import ValidatedEgressTarget

DNSResolver = Callable[[str, int], Iterable[str]]


class EgressPolicyError(ValueError):
    """Sanitized failure raised when an endpoint is not safe for guarded egress."""


class FixedProviderEgressPolicy:
    """Validate exact fixed HTTPS origins, paths, and public DNS answers."""

    def __init__(self, *, dns_resolver: DNSResolver | None = None) -> None:
        self._dns_resolver = dns_resolver or _resolve_dns

    def validate_endpoint(
        self,
        endpoint: str,
        *,
        expected_host: str,
        allowed_ports: frozenset[int],
        allowed_path_prefixes: tuple[str, ...],
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
            port = parsed.port or 443
        except ValueError:
            raise EgressPolicyError("Guarded endpoint is invalid") from None

        host = str(parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
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

        addresses = self._resolve_public_addresses(host, port)
        return ValidatedEgressTarget(
            url=endpoint,
            scheme="https",
            host=host,
            port=port,
            path=path,
            resolved_addresses=addresses,
        )

    def revalidate(self, target: ValidatedEgressTarget) -> None:
        """Reject DNS answer changes between target validation and request send."""

        current = self._resolve_public_addresses(target.host, target.port)
        if current != target.resolved_addresses:
            raise EgressPolicyError("Guarded endpoint DNS answers changed before send")

    def _resolve_public_addresses(self, host: str, port: int) -> tuple[str, ...]:
        """Resolve and require a non-empty stable set of globally routable IPs."""

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
            if (
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
