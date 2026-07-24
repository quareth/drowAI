"""Semantic-first knowledge adapter for Amass DNS enumeration results.

This adapter converts Amass semantic transport rows, or compatible normalized
v5 tool metadata, into canonical knowledge observations. It never parses raw
Amass output and does not own persistence or projection behavior.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from runtime_shared.semantic.canonical_keys import (
    build_host_dns_key,
    build_host_ip_key,
    build_relationship_edge_key,
)

from ..contracts import ObservationCreate
from .base import AdapterContext
from .network_common import dedupe_observations, make_observation, resolve_evidence_refs
from .semantic_common import extract_semantic_observations

AMASS_TOOL_ID = "information_gathering.dns.amass"
AMASS_CAPABILITY_FAMILY = "dns_enumeration"

AMASS_OBSERVATION_DNS_NAME_DISCOVERED = "dns.name_discovered"
AMASS_OBSERVATION_IP_ADDRESS_RESOLVED = "dns.address_resolved"
AMASS_OBSERVATION_DNS_RESOLVES_TO = "relationship.resolves_to"

AMASS_RELATIONSHIP_TYPE_RESOLVES_TO = "resolves_to"
AMASS_SOURCE = "amass"

_ALLOWED_SEMANTIC_SUBJECTS: Mapping[str, set[str]] = {
    AMASS_OBSERVATION_DNS_NAME_DISCOVERED: {"host.dns"},
    AMASS_OBSERVATION_IP_ADDRESS_RESOLVED: {"host.ip"},
    AMASS_OBSERVATION_DNS_RESOLVES_TO: {"relationship.edge"},
}


class AmassKnowledgeAdapter:
    """Normalize Amass DNS facts into canonical knowledge observations."""

    tool_names = (AMASS_TOOL_ID,)
    capability_families = (AMASS_CAPABILITY_FAMILY,)

    def supports(self, context: AdapterContext) -> bool:
        return context.source_tool_name() == AMASS_TOOL_ID

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        semantic = self._extract_from_semantic_observations(context)
        if semantic:
            return dedupe_observations(semantic)
        return self._extract_from_tool_metadata(context)

    def _extract_from_semantic_observations(
        self,
        context: AdapterContext,
    ) -> list[ObservationCreate]:
        observations = extract_semantic_observations(
            context,
            allowed_subject_types_by_observation=_ALLOWED_SEMANTIC_SUBJECTS,
        )
        if not observations:
            return []

        evidence_refs = resolve_evidence_refs(context)
        if not evidence_refs:
            return observations

        enriched: list[ObservationCreate] = []
        for observation in observations:
            payload = dict(observation.payload or {})
            if not isinstance(payload.get("evidence_refs"), list):
                payload["evidence_refs"] = list(evidence_refs)
            enriched.append(_replace_payload(observation, payload))
        return enriched

    def _extract_from_tool_metadata(
        self,
        context: AdapterContext,
    ) -> list[ObservationCreate]:
        facts = _collect_metadata_facts(context.tool_metadata)
        if facts is None:
            return []

        evidence_refs = resolve_evidence_refs(context)
        observations: list[ObservationCreate] = []

        for name in facts.names:
            dns_key = build_host_dns_key(name)
            payload: dict[str, Any] = {
                "tool_source": AMASS_SOURCE,
                "dns_name": name,
                "resolved_address_count": len(facts.addresses_by_name.get(name, ())),
            }
            if evidence_refs:
                payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type=AMASS_OBSERVATION_DNS_NAME_DISCOVERED,
                    subject_type="host.dns",
                    subject_key=dns_key,
                    payload=payload,
                )
            )

        for address in facts.ips:
            payload = {
                "tool_source": AMASS_SOURCE,
                "address": address,
                "record_type": _record_type(address),
            }
            if evidence_refs:
                payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type=AMASS_OBSERVATION_IP_ADDRESS_RESOLVED,
                    subject_type="host.ip",
                    subject_key=build_host_ip_key(address),
                    payload=payload,
                )
            )

        for name in facts.names:
            source_key = build_host_dns_key(name)
            for address in facts.addresses_by_name.get(name, ()):
                target_key = build_host_ip_key(address)
                payload = {
                    "source_subject_type": "host.dns",
                    "source_subject_key": source_key,
                    "relationship_type": AMASS_RELATIONSHIP_TYPE_RESOLVES_TO,
                    "target_subject_type": "host.ip",
                    "target_subject_key": target_key,
                    "record_type": _record_type(address),
                    "tool_source": AMASS_SOURCE,
                }
                if evidence_refs:
                    payload["evidence_refs"] = evidence_refs
                observations.append(
                    make_observation(
                        context=context,
                        observation_type=AMASS_OBSERVATION_DNS_RESOLVES_TO,
                        subject_type="relationship.edge",
                        subject_key=build_relationship_edge_key(
                            source_subject_key=source_key,
                            relationship_type=AMASS_RELATIONSHIP_TYPE_RESOLVES_TO,
                            target_subject_key=target_key,
                        ),
                        payload=payload,
                    )
                )

        return dedupe_observations(observations)


class _AmassMetadataFacts:
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


def _collect_metadata_facts(
    tool_metadata: Mapping[str, Any],
) -> _AmassMetadataFacts | None:
    if not isinstance(tool_metadata, Mapping):
        return None
    if not _has_compatible_list_fields(tool_metadata):
        return None

    names: set[str] = set()
    ips: set[str] = set()
    addresses_by_name: dict[str, set[str]] = {}

    for row in _as_list(tool_metadata.get("subdomains")):
        item = _as_mapping(row)
        name = _canonical_dns_name(item.get("subdomain"))
        if name is None:
            continue
        names.add(name)
        address_set = addresses_by_name.setdefault(name, set())
        for address in _canonical_ip_values(item.get("ip")):
            address_set.add(address)
            ips.add(address)

    for row in _as_list(tool_metadata.get("hosts")):
        item = _as_mapping(row)
        name = _canonical_dns_name(item.get("hostname"))
        if name is None:
            continue
        names.add(name)
        address_set = addresses_by_name.setdefault(name, set())
        for address in _canonical_ip_values(item.get("ip")):
            address_set.add(address)
            ips.add(address)

    for address in _canonical_ip_values(tool_metadata.get("ips")):
        ips.add(address)

    ordered_names = tuple(sorted(names))
    ordered_ips = tuple(_sort_ip_addresses(ips))
    ordered_addresses_by_name = {
        name: tuple(_sort_ip_addresses(addresses_by_name.get(name, set())))
        for name in ordered_names
    }
    return _AmassMetadataFacts(
        names=ordered_names,
        ips=ordered_ips,
        addresses_by_name=ordered_addresses_by_name,
    )


def _has_compatible_list_fields(tool_metadata: Mapping[str, Any]) -> bool:
    compatible_field_found = False
    for field_name in ("subdomains", "hosts", "ips"):
        value = tool_metadata.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list):
            return False
        compatible_field_found = True
    return compatible_field_found


def _as_mapping(value: object) -> Mapping[str, Any]:
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


def _sort_ip_addresses(values: set[str]) -> list[str]:
    parsed = {ipaddress.ip_address(value) for value in values}
    return [
        str(address)
        for address in sorted(parsed, key=lambda item: (item.version, int(item)))
    ]


def _record_type(address: str) -> str:
    return "A" if ipaddress.ip_address(address).version == 4 else "AAAA"


def _replace_payload(
    observation: ObservationCreate,
    payload: Mapping[str, Any],
) -> ObservationCreate:
    return ObservationCreate(
        user_id=observation.user_id,
        tenant_id=observation.tenant_id,
        engagement_id=observation.engagement_id,
        task_id=observation.task_id,
        source_execution_id=observation.source_execution_id,
        ingestion_run_id=observation.ingestion_run_id,
        observation_type=observation.observation_type,
        subject_type=observation.subject_type,
        subject_key=observation.subject_key,
        assertion_level=observation.assertion_level,
        payload=dict(payload),
        observation_metadata=dict(observation.observation_metadata or {}),
        observed_at=observation.observed_at,
        dedupe_key=observation.dedupe_key,
    )


__all__ = [
    "AMASS_TOOL_ID",
    "AmassKnowledgeAdapter",
]
