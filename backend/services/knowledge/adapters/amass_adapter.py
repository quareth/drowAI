"""Semantic-first knowledge adapter for Amass DNS enumeration results.

This adapter converts Amass semantic transport rows, or compatible normalized
v5 tool metadata, into canonical knowledge observations. It never parses raw
Amass output and does not own persistence or projection behavior.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from runtime_shared.semantic.amass_facts import (
    collect_amass_facts,
    dns_record_type,
)
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
            enriched.append(replace(observation, payload=dict(payload)))
        return enriched

    def _extract_from_tool_metadata(
        self,
        context: AdapterContext,
    ) -> list[ObservationCreate]:
        if not _has_compatible_list_fields(context.tool_metadata):
            return []
        facts = collect_amass_facts(context.tool_metadata)

        evidence_refs = resolve_evidence_refs(context)
        observations: list[ObservationCreate] = []

        for name in facts.names:
            dns_key = build_host_dns_key(name)
            payload: dict[str, Any] = {
                "tool_source": AMASS_SOURCE,
                "dns_name": name,
                "resolved_address_count": len(facts.addresses_by_name.get(name, ())),
            }
            role = facts.roles_by_name.get(name)
            if role:
                payload["discovery_role"] = role
            result_scope = facts.result_scope_by_name.get(name)
            if result_scope:
                payload["result_scope"] = result_scope
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
                "record_type": dns_record_type(address),
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
                    "record_type": dns_record_type(address),
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


__all__ = [
    "AMASS_TOOL_ID",
    "AmassKnowledgeAdapter",
]
