"""Normalize parsed Amass DNS metadata into a shared immutable fact view.

This module owns backend-free canonicalization, deduplication, and ordering for
already-normalized Amass metadata. It does not parse collector output or render
semantic and persistence-specific observations.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .canonical_keys import build_host_dns_key, build_host_ip_key

_DISCOVERY_ROLES = frozenset(
    {"scope_seed", "prior_known", "newly_discovered"}
)


@dataclass(frozen=True, slots=True)
class AmassFacts:
    """Canonical normalized DNS facts shared by agent and backend consumers."""

    names: tuple[str, ...]
    ips: tuple[str, ...]
    addresses_by_name: Mapping[str, tuple[str, ...]]
    roles_by_name: Mapping[str, str]
    result_scope_by_name: Mapping[str, str]


def collect_amass_facts(metadata: Mapping[str, object]) -> AmassFacts:
    """Return canonical, deduplicated facts from compatible Amass metadata."""

    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    names: set[str] = set()
    ips: set[str] = set()
    addresses_by_name: dict[str, set[str]] = {}
    roles_by_name: dict[str, str] = {}
    result_scope_by_name: dict[str, str] = {}

    for row in _as_list(metadata_dict.get("subdomains")):
        item = _as_mapping(row)
        name = _canonical_dns_name(item.get("subdomain"))
        if name is None:
            continue
        names.add(name)
        role = str(item.get("discovery_role") or "").strip()
        if role in _DISCOVERY_ROLES:
            roles_by_name[name] = role
        result_scope = str(item.get("result_scope") or "").strip()
        if result_scope:
            result_scope_by_name[name] = result_scope
        _add_addresses(
            item.get("ip"),
            addresses_by_name.setdefault(name, set()),
            ips,
        )

    for row in _as_list(metadata_dict.get("hosts")):
        item = _as_mapping(row)
        name = _canonical_dns_name(item.get("hostname"))
        if name is None:
            continue
        names.add(name)
        _add_addresses(
            item.get("ip"),
            addresses_by_name.setdefault(name, set()),
            ips,
        )

    ips.update(_canonical_ip_values(metadata_dict.get("ips")))

    ordered_names = tuple(sorted(names))
    ordered_addresses = {
        name: _sort_ip_addresses(addresses_by_name.get(name, set()))
        for name in ordered_names
    }
    return AmassFacts(
        names=ordered_names,
        ips=_sort_ip_addresses(ips),
        addresses_by_name=MappingProxyType(ordered_addresses),
        roles_by_name=MappingProxyType(dict(roles_by_name)),
        result_scope_by_name=MappingProxyType(dict(result_scope_by_name)),
    )


def dns_record_type(address: str) -> str:
    """Return the DNS record type for one canonical IP address."""

    return "A" if ipaddress.ip_address(address).version == 4 else "AAAA"


def _add_addresses(
    value: object,
    addresses_for_name: set[str],
    all_ips: set[str],
) -> None:
    """Add canonical addresses to both per-name and global collections."""

    addresses = _canonical_ip_values(value)
    addresses_for_name.update(addresses)
    all_ips.update(addresses)


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _canonical_dns_name(value: object) -> str | None:
    try:
        key = build_host_dns_key(value)
    except ValueError:
        return None
    return key.removeprefix("host.dns:")


def _canonical_ip_values(value: object) -> set[str]:
    candidates = value if isinstance(value, list) else [value]
    normalized: set[str] = set()
    for candidate in candidates:
        try:
            key = build_host_ip_key(candidate)
        except ValueError:
            continue
        normalized.add(key.removeprefix("host.ip:"))
    return normalized


def _sort_ip_addresses(values: set[str]) -> tuple[str, ...]:
    parsed = {ipaddress.ip_address(value) for value in values}
    return tuple(
        str(address)
        for address in sorted(parsed, key=lambda item: (item.version, int(item)))
    )


__all__ = ["AmassFacts", "collect_amass_facts", "dns_record_type"]
