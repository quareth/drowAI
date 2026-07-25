"""Normalize graph-free OWASP Amass v5 name and address query output."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any

from .amass_runtime import (
    AMASS_BEFORE_NAMES_BEGIN,
    AMASS_BEFORE_NAMES_END,
    AMASS_BEFORE_RESOLVED_BEGIN,
    AMASS_BEFORE_RESOLVED_END,
    AMASS_NAMES_BEGIN,
    AMASS_NAMES_END,
    AMASS_RESOLVED_BEGIN,
    AMASS_RESOLVED_END,
    AMASS_STATUS_BEGIN,
    AMASS_STATUS_END,
)

_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NO_NAMES_MESSAGE = "no names were discovered"
_SECTION_BY_MARKER = {
    AMASS_BEFORE_NAMES_BEGIN: "before_names",
    AMASS_BEFORE_NAMES_END: None,
    AMASS_BEFORE_RESOLVED_BEGIN: "before_resolved",
    AMASS_BEFORE_RESOLVED_END: None,
    AMASS_NAMES_BEGIN: "names",
    AMASS_NAMES_END: None,
    AMASS_RESOLVED_BEGIN: "resolved",
    AMASS_RESOLVED_END: None,
    AMASS_STATUS_BEGIN: "status",
    AMASS_STATUS_END: None,
}


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
    root_domain: Any = None,
) -> dict[str, Any]:
    """Parse tagged ``amass subs`` output into deterministic name/IP metadata."""

    before_names: set[str] = set()
    names: set[str] = set()
    addresses_by_name: dict[str, set[str]] = {}
    diagnostics: list[str] = []
    seen_markers: set[str] = set()
    section: str | None = None
    status_fields: dict[str, str] = {}

    for raw_line in str(output_text or "").splitlines():
        line = raw_line.strip()
        if line in _SECTION_BY_MARKER:
            section = _SECTION_BY_MARKER[line]
            seen_markers.add(line)
            continue
        if not line or line.lower() == _NO_NAMES_MESSAGE:
            continue

        if section in {"before_names", "names"}:
            name = normalize_dns_name(line)
            if name is None:
                _append_diagnostic(diagnostics, "invalid_name_row")
                continue
            if section == "before_names":
                before_names.add(name)
            else:
                names.add(name)
                addresses_by_name.setdefault(name, set())
            continue

        if section in {"before_resolved", "resolved"}:
            name_text, separator, address_text = line.partition(" ")
            name = normalize_dns_name(name_text)
            if name is None or not separator or not address_text.strip():
                _append_diagnostic(diagnostics, "invalid_resolved_row")
                continue
            addresses = _normalize_ip_addresses(address_text.split(","))
            if not addresses:
                _append_diagnostic(diagnostics, "resolved_row_without_valid_address")
                continue
            if section == "before_resolved":
                before_names.add(name)
            else:
                names.add(name)
                addresses_by_name.setdefault(name, set()).update(addresses)
            continue

        if section == "status":
            key, separator, value = line.partition("=")
            if not separator:
                _append_diagnostic(diagnostics, "invalid_status_row")
                continue
            normalized_key = str(key or "").strip()
            if not normalized_key:
                _append_diagnostic(diagnostics, "invalid_status_row")
                continue
            status_fields[normalized_key] = str(value or "").strip()
            continue

    expected_markers = {
        AMASS_NAMES_BEGIN,
        AMASS_NAMES_END,
        AMASS_RESOLVED_BEGIN,
        AMASS_RESOLVED_END,
    }
    if not expected_markers.issubset(seen_markers):
        _append_diagnostic(diagnostics, "incomplete_capture_sections")
    if (
        AMASS_STATUS_BEGIN in seen_markers or AMASS_STATUS_END in seen_markers
    ) and not {AMASS_STATUS_BEGIN, AMASS_STATUS_END}.issubset(seen_markers):
        _append_diagnostic(diagnostics, "incomplete_status_section")

    ordered_names = sorted(names)
    ordered_ips = _sort_ip_addresses(
        address
        for name in ordered_names
        for address in addresses_by_name.get(name, set())
    )
    subdomains: list[dict[str, Any]] = []
    hosts: list[dict[str, Any]] = []
    resolved_name_count = 0
    root_name = normalize_dns_name(root_domain)
    should_emit_roles = root_name is not None or before_names or status_fields
    roles_by_name = _classify_discovery_roles(
        ordered_names,
        before_names=before_names,
        root_name=root_name,
    )

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
        if should_emit_roles:
            subdomains[-1]["discovery_role"] = roles_by_name[name]
            subdomains[-1]["result_scope"] = "task_cumulative"
        hosts.append({"hostname": name, "ip": addresses})

    parse_status = "success"
    if diagnostics:
        parse_status = "partial"
    elif not ordered_names:
        parse_status = "empty"
    enumeration_exit_code = _safe_int(status_fields.get("enum_status"), int(exit_code))
    enumeration_status = _enumeration_status(status_fields, exit_code=int(exit_code))
    result_completeness = _result_completeness(
        enumeration_status=enumeration_status,
        names_count=len(ordered_names),
    )
    partial_results = result_completeness == "partial"
    seed_names = [
        name for name in ordered_names if roles_by_name.get(name) == "scope_seed"
    ]
    prior_names = [
        name for name in ordered_names if roles_by_name.get(name) == "prior_known"
    ]
    newly_discovered_names = [
        name
        for name in ordered_names
        if roles_by_name.get(name) == "newly_discovered"
    ]

    metadata: dict[str, Any] = {
        "subdomains": subdomains,
        "hosts": hosts,
        "ips": ordered_ips,
        "names_count": len(ordered_names),
        "resolved_names_count": resolved_name_count,
        "unresolved_names_count": len(ordered_names) - resolved_name_count,
        "ip_count": len(ordered_ips),
        "parse_status": parse_status,
        "enumeration_status": enumeration_status,
        "result_completeness": result_completeness,
        "partial_results": partial_results,
        "enumeration_exit_code": enumeration_exit_code,
        "capture_format": "amass_v5_subs_text",
        "diagnostics": diagnostics,
    }
    if status_fields:
        metadata["collector_status"] = dict(sorted(status_fields.items()))
    if should_emit_roles:
        metadata.update(
            {
                "seed_names": seed_names,
                "prior_names": prior_names,
                "newly_discovered_names": newly_discovered_names,
                "seed_names_count": len(seed_names),
                "prior_names_count": len(prior_names),
                "newly_discovered_names_count": len(newly_discovered_names),
                "discovered_names_count": len(newly_discovered_names),
            }
        )
    return metadata


def _classify_discovery_roles(
    names: list[str],
    *,
    before_names: set[str],
    root_name: str | None,
) -> dict[str, str]:
    """Return deterministic seed/prior/new roles for post-query names."""

    roles: dict[str, str] = {}
    for name in names:
        if root_name is not None and name == root_name:
            roles[name] = "scope_seed"
        elif name in before_names:
            roles[name] = "prior_known"
        else:
            roles[name] = "newly_discovered"
    return roles


def _enumeration_status(status_fields: dict[str, str], *, exit_code: int) -> str:
    """Return workflow execution status independently from parser status."""

    final_status = str(status_fields.get("final_status") or "").strip().lower()
    error_code = str(status_fields.get("error_code") or "").strip().lower()
    timed_out = str(status_fields.get("timed_out") or "").strip().lower()
    if final_status == "complete":
        return "complete"
    if final_status == "timed_out" or timed_out == "true" or error_code.endswith(
        "timeout"
    ):
        return "timed_out"
    if final_status == "query_failed":
        return "query_failed"
    if final_status == "enum_failed":
        return "failed"
    if final_status:
        return final_status
    return "complete" if int(exit_code) == 0 else "failed"


def _result_completeness(
    *,
    enumeration_status: str,
    names_count: int,
) -> str:
    """Return result completeness separately from capture/parser validity."""

    if names_count <= 0:
        return "empty" if enumeration_status == "complete" else "none"
    if enumeration_status == "complete":
        return "complete"
    return "partial"


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)


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
