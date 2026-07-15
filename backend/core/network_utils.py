"""Pure helpers for validating and normalizing network addresses."""

from __future__ import annotations

from ipaddress import ip_address


def normalize_ip_address(value: object) -> str | None:
    """Return a canonical IPv4/IPv6 address or ``None`` for invalid input."""

    candidate = str(value or "").strip()
    if not candidate:
        return None
    address_without_zone = candidate.split("%", 1)[0]
    try:
        return ip_address(address_without_zone).compressed
    except ValueError:
        return None
