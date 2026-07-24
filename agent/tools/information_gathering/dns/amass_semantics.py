"""Project normalized Amass DNS metadata into semantic transport rows.

This module owns Amass semantic observations and evidence built only from the
normalized metadata returned by amass_analysis.py. It does not parse raw Amass
output, execute commands, or import backend knowledge services.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from agent.semantic.evidence_vocabulary import SemanticEvidenceType
from runtime_shared.semantic.canonical_keys import (
    build_host_dns_key,
    build_host_ip_key,
    build_relationship_edge_key,
)

AMASS_SEMANTIC_SCHEMA_VERSION = "amass.v1"
AMASS_CAPABILITY_FAMILY = "dns_enumeration"

AMASS_OBSERVATION_DNS_NAME_DISCOVERED = "dns.name_discovered"
AMASS_OBSERVATION_IP_ADDRESS_RESOLVED = "dns.address_resolved"
AMASS_OBSERVATION_DNS_RESOLVES_TO = "relationship.resolves_to"

AMASS_SUBJECT_TYPE_DNS = "host.dns"
AMASS_SUBJECT_TYPE_IP = "host.ip"
AMASS_SUBJECT_TYPE_RELATIONSHIP = "relationship.edge"
AMASS_RELATIONSHIP_TYPE_RESOLVES_TO = "resolves_to"

AMASS_TOOL_SOURCE = "amass"
AMASS_EVIDENCE_LIMIT = 6


def build_amass_observations(
    metadata: Mapping[str, object],
) -> list[dict[str, object]]:
    """Project normalized Amass DNS facts into semantic observations."""
    facts = _collect_normalized_facts(metadata)
    observations: list[dict[str, object]] = []

    for name in facts.names:
        dns_key = build_host_dns_key(name)
        addresses = facts.addresses_by_name.get(name, ())
        payload: dict[str, object] = {
            "tool_source": AMASS_TOOL_SOURCE,
            "dns_name": name,
            "resolved_address_count": len(addresses),
        }
        if addresses:
            payload["record_types"] = sorted(
                {_record_type(address) for address in addresses}
            )
        observations.append(
            {
                "observation_type": AMASS_OBSERVATION_DNS_NAME_DISCOVERED,
                "subject_type": AMASS_SUBJECT_TYPE_DNS,
                "subject_key": dns_key,
                "payload": payload,
            }
        )

    for address in facts.ips:
        observations.append(
            {
                "observation_type": AMASS_OBSERVATION_IP_ADDRESS_RESOLVED,
                "subject_type": AMASS_SUBJECT_TYPE_IP,
                "subject_key": build_host_ip_key(address),
                "payload": {
                    "tool_source": AMASS_TOOL_SOURCE,
                    "address": address,
                    "record_type": _record_type(address),
                },
            }
        )

    for name in facts.names:
        source_key = build_host_dns_key(name)
        for address in facts.addresses_by_name.get(name, ()):
            target_key = build_host_ip_key(address)
            observations.append(
                {
                    "observation_type": AMASS_OBSERVATION_DNS_RESOLVES_TO,
                    "subject_type": AMASS_SUBJECT_TYPE_RELATIONSHIP,
                    "subject_key": build_relationship_edge_key(
                        source_subject_key=source_key,
                        relationship_type=AMASS_RELATIONSHIP_TYPE_RESOLVES_TO,
                        target_subject_key=target_key,
                    ),
                    "payload": {
                        "source_subject_type": AMASS_SUBJECT_TYPE_DNS,
                        "source_subject_key": source_key,
                        "relationship_type": AMASS_RELATIONSHIP_TYPE_RESOLVES_TO,
                        "target_subject_type": AMASS_SUBJECT_TYPE_IP,
                        "target_subject_key": target_key,
                        "record_type": _record_type(address),
                        "tool_source": AMASS_TOOL_SOURCE,
                    },
                }
            )

    return observations


def build_amass_evidence(
    metadata: Mapping[str, object],
    args: Any,
) -> list[dict[str, object]]:
    """Build bounded semantic evidence entries from normalized Amass metadata."""
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    evidence: list[dict[str, object]] = []

    target = str(getattr(args, "target", "") or "").strip().lower()
    if target:
        evidence.append(
            {
                "type": SemanticEvidenceType.TARGET_TEMPLATE.value,
                "name": "root_domain",
                "value": target,
                "detail": {},
            }
        )

    mode = getattr(args, "mode", None)
    mode_value = str(getattr(mode, "value", mode) or "").strip().lower()
    if mode_value:
        evidence.append(
            {
                "type": SemanticEvidenceType.VARIANT.value,
                "name": "scan_mode",
                "value": mode_value,
                "detail": {},
            }
        )

    inactivity_timeout = _safe_int(getattr(args, "inactivity_timeout_minutes", 0))
    if inactivity_timeout > 0:
        evidence.append(
            {
                "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                "name": "inactivity_timeout",
                "value": inactivity_timeout,
                "detail": {"unit": "minutes"},
            }
        )

    evidence.append(
        {
            "type": SemanticEvidenceType.RESULT_SUMMARY.value,
            "name": "names_total",
            "value": _safe_int(metadata_dict.get("names_count")),
            "detail": {"unit": "names"},
        }
    )
    evidence.append(
        {
            "type": SemanticEvidenceType.RESULT_SUMMARY.value,
            "name": "unique_ips",
            "value": _safe_int(metadata_dict.get("ip_count")),
            "detail": {"unit": "addresses"},
        }
    )

    parse_status = str(metadata_dict.get("parse_status") or "").strip().lower()
    if parse_status:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "parse_status",
                "value": parse_status,
                "detail": {
                    "severity": "info" if parse_status == "success" else "warning"
                },
            }
        )

    return evidence[:AMASS_EVIDENCE_LIMIT]


class _AmassFacts:
    def __init__(
        self,
        *,
        names: tuple[str, ...],
        ips: tuple[str, ...],
        addresses_by_name: dict[str, tuple[str, ...]],
    ) -> None:
        self.names = names
        self.ips = ips
        self.addresses_by_name = addresses_by_name


def _collect_normalized_facts(metadata: Mapping[str, object]) -> _AmassFacts:
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    names: set[str] = set()
    addresses_by_name: dict[str, set[str]] = {}
    ips: set[str] = set()

    for row in _as_list(metadata_dict.get("subdomains")):
        item = _as_mapping(row)
        name = _normalize_dns_candidate(item.get("subdomain"))
        if name is None:
            continue
        names.add(name)
        address_set = addresses_by_name.setdefault(name, set())
        for address in _normalize_ip_values(item.get("ip")):
            address_set.add(address)
            ips.add(address)

    for row in _as_list(metadata_dict.get("hosts")):
        item = _as_mapping(row)
        name = _normalize_dns_candidate(item.get("hostname"))
        if name is None:
            continue
        names.add(name)
        address_set = addresses_by_name.setdefault(name, set())
        for address in _normalize_ip_values(item.get("ip")):
            address_set.add(address)
            ips.add(address)

    for address in _normalize_ip_values(metadata_dict.get("ips")):
        ips.add(address)

    ordered_names = tuple(sorted(names))
    ordered_ips = tuple(_sort_ip_addresses(ips))
    ordered_addresses_by_name = {
        name: tuple(_sort_ip_addresses(addresses_by_name.get(name, set())))
        for name in ordered_names
    }
    return _AmassFacts(
        names=ordered_names,
        ips=ordered_ips,
        addresses_by_name=ordered_addresses_by_name,
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _normalize_dns_candidate(value: object) -> str | None:
    try:
        key = build_host_dns_key(value)
    except ValueError:
        return None
    return key.removeprefix("host.dns:")


def _normalize_ip_values(value: object) -> set[str]:
    candidates = value if isinstance(value, list) else [value]
    normalized: set[str] = set()
    for candidate in candidates:
        try:
            key = build_host_ip_key(candidate)
        except ValueError:
            continue
        normalized.add(key.removeprefix("host.ip:"))
    return normalized


def _sort_ip_addresses(values: set[str]) -> list[str]:
    parsed = {ipaddress.ip_address(value) for value in values}
    return [
        str(address)
        for address in sorted(parsed, key=lambda item: (item.version, int(item)))
    ]


def _record_type(address: str) -> str:
    return "A" if ipaddress.ip_address(address).version == 4 else "AAAA"


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
    return 0


__all__ = [
    "AMASS_CAPABILITY_FAMILY",
    "AMASS_EVIDENCE_LIMIT",
    "AMASS_OBSERVATION_DNS_NAME_DISCOVERED",
    "AMASS_OBSERVATION_DNS_RESOLVES_TO",
    "AMASS_OBSERVATION_IP_ADDRESS_RESOLVED",
    "AMASS_RELATIONSHIP_TYPE_RESOLVES_TO",
    "AMASS_SEMANTIC_SCHEMA_VERSION",
    "build_amass_evidence",
    "build_amass_observations",
]
