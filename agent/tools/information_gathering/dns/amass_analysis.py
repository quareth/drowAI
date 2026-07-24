"""Normalize graph-free OWASP Amass v5 name and address query output."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any

AMASS_NAMES_BEGIN = "__DROWAI_AMASS_V5_NAMES_BEGIN__"
AMASS_NAMES_END = "__DROWAI_AMASS_V5_NAMES_END__"
AMASS_RESOLVED_BEGIN = "__DROWAI_AMASS_V5_RESOLVED_BEGIN__"
AMASS_RESOLVED_END = "__DROWAI_AMASS_V5_RESOLVED_END__"

_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NO_NAMES_MESSAGE = "no names were discovered"


def normalize_dns_name(value: Any) -> str | None:
    """Return a lowercase ASCII DNS name, or ``None`` when invalid."""

    candidate = str(value or "").strip().rstrip(".")
    if not candidate or len(candidate) > 253:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return None

    try:
        normalized = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None

    labels = normalized.split(".")
    if len(labels) < 2 or any(not _DNS_LABEL_PATTERN.fullmatch(label) for label in labels):
        return None
    return normalized


def parse_amass_v5_results(
    output_text: str,
    *,
    exit_code: int = 0,
) -> dict[str, Any]:
    """Parse tagged ``amass subs`` output into deterministic name/IP metadata."""

    names: set[str] = set()
    addresses_by_name: dict[str, set[str]] = {}
    diagnostics: list[str] = []
    seen_markers: set[str] = set()
    section: str | None = None

    for raw_line in str(output_text or "").splitlines():
        line = raw_line.strip()
        if line == AMASS_NAMES_BEGIN:
            section = "names"
            seen_markers.add(line)
            continue
        if line == AMASS_NAMES_END:
            section = None
            seen_markers.add(line)
            continue
        if line == AMASS_RESOLVED_BEGIN:
            section = "resolved"
            seen_markers.add(line)
            continue
        if line == AMASS_RESOLVED_END:
            section = None
            seen_markers.add(line)
            continue
        if not line or line.lower() == _NO_NAMES_MESSAGE:
            continue

        if section == "names":
            name = normalize_dns_name(line)
            if name is None:
                _append_diagnostic(diagnostics, "invalid_name_row")
                continue
            names.add(name)
            addresses_by_name.setdefault(name, set())
            continue

        if section == "resolved":
            name_text, separator, address_text = line.partition(" ")
            name = normalize_dns_name(name_text)
            if name is None or not separator or not address_text.strip():
                _append_diagnostic(diagnostics, "invalid_resolved_row")
                continue
            addresses = _normalize_ip_addresses(address_text.split(","))
            if not addresses:
                _append_diagnostic(diagnostics, "resolved_row_without_valid_address")
                continue
            names.add(name)
            addresses_by_name.setdefault(name, set()).update(addresses)

    expected_markers = {
        AMASS_NAMES_BEGIN,
        AMASS_NAMES_END,
        AMASS_RESOLVED_BEGIN,
        AMASS_RESOLVED_END,
    }
    if seen_markers != expected_markers:
        _append_diagnostic(diagnostics, "incomplete_capture_sections")

    ordered_names = sorted(names)
    ordered_ips = _sort_ip_addresses(
        address
        for name in ordered_names
        for address in addresses_by_name.get(name, set())
    )
    subdomains: list[dict[str, Any]] = []
    hosts: list[dict[str, Any]] = []
    resolved_name_count = 0

    for name in ordered_names:
        addresses = _sort_ip_addresses(addresses_by_name.get(name, set()))
        if addresses:
            resolved_name_count += 1
        record_types = sorted(
            {"A" if ipaddress.ip_address(address).version == 4 else "AAAA" for address in addresses}
        )
        subdomains.append(
            {
                "subdomain": name,
                "ip": addresses,
                "record_types": record_types,
                "source": "amass",
            }
        )
        hosts.append({"hostname": name, "ip": addresses})

    parse_status = "success"
    if int(exit_code) != 0:
        parse_status = "partial" if ordered_names else "failed"
    elif diagnostics:
        parse_status = "partial"
    elif not ordered_names:
        parse_status = "empty"

    return {
        "subdomains": subdomains,
        "hosts": hosts,
        "ips": ordered_ips,
        "names_count": len(ordered_names),
        "resolved_names_count": resolved_name_count,
        "unresolved_names_count": len(ordered_names) - resolved_name_count,
        "ip_count": len(ordered_ips),
        "parse_status": parse_status,
        "capture_format": "amass_v5_subs_text",
        "diagnostics": diagnostics,
    }


def _normalize_ip_addresses(values: Iterable[Any]) -> set[str]:
    """Return canonical IPv4/IPv6 strings from a candidate collection."""

    normalized: set[str] = set()
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        try:
            normalized.add(str(ipaddress.ip_address(candidate)))
        except ValueError:
            continue
    return normalized


def _sort_ip_addresses(values: Iterable[str]) -> list[str]:
    """Return unique IP strings ordered by address family and numeric value."""

    parsed = {ipaddress.ip_address(value) for value in values}
    return [str(address) for address in sorted(parsed, key=lambda item: (item.version, int(item)))]


def _append_diagnostic(diagnostics: list[str], value: str) -> None:
    """Append one stable diagnostic code without duplicates."""

    if value not in diagnostics:
        diagnostics.append(value)
