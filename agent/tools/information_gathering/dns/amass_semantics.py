"""Project normalized Amass DNS metadata into semantic transport rows.

This module owns Amass semantic observations and evidence built only from the
normalized metadata returned by amass_analysis.py. It does not parse raw Amass
output, execute commands, or import backend knowledge services.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.semantic.evidence_vocabulary import SemanticEvidenceType
from runtime_shared.semantic.amass_facts import (
    collect_amass_facts,
    dns_record_type,
)
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
AMASS_EVIDENCE_LIMIT = 8


def build_amass_observations(
    metadata: Mapping[str, object],
) -> list[dict[str, object]]:
    """Project normalized Amass DNS facts into semantic observations."""
    facts = collect_amass_facts(metadata)
    observations: list[dict[str, object]] = []

    for name in facts.names:
        dns_key = build_host_dns_key(name)
        addresses = facts.addresses_by_name.get(name, ())
        payload: dict[str, object] = {
            "tool_source": AMASS_TOOL_SOURCE,
            "dns_name": name,
            "resolved_address_count": len(addresses),
        }
        role = facts.roles_by_name.get(name)
        if role:
            payload["discovery_role"] = role
        result_scope = facts.result_scope_by_name.get(name)
        if result_scope:
            payload["result_scope"] = result_scope
        if addresses:
            payload["record_types"] = sorted(
                {dns_record_type(address) for address in addresses}
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
                    "record_type": dns_record_type(address),
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
                        "record_type": dns_record_type(address),
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
    if "newly_discovered_names_count" in metadata_dict:
        evidence.append(
            {
                "type": SemanticEvidenceType.RESULT_SUMMARY.value,
                "name": "newly_discovered_names",
                "value": _safe_int(metadata_dict.get("newly_discovered_names_count")),
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

    enumeration_status = str(metadata_dict.get("enumeration_status") or "").strip().lower()
    if enumeration_status:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "enumeration_status",
                "value": enumeration_status,
                "detail": {
                    "severity": "info"
                    if enumeration_status == "complete"
                    else "warning"
                },
            }
        )

    return evidence[:AMASS_EVIDENCE_LIMIT]


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
