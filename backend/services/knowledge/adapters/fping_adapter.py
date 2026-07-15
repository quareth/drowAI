"""Deterministic adapter for fping host-liveness output.

This adapter normalizes fping execution payloads into canonical host
observations only. fping is a host-liveness tool, so the adapter MUST NOT emit
open ports, service detections, findings, or relationships.

Extraction priority is strict:
1. Accept ``network.host_discovered`` rows from ``context.semantic_observations``.
2. Fallback to alive IPs in ``context.tool_metadata["alive_hosts"]``.
3. Raw artifact text is intentionally not parsed for Knowledge ingestion in MVP;
   raw output remains attached as artifact evidence only.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Iterable, Mapping

from ..contracts import ObservationCreate
from .base import AdapterContext
from .network_common import (
    build_host_subject_key,
    dedupe_observations,
    make_observation,
    resolve_evidence_refs,
)


class FpingKnowledgeAdapter:
    """Normalize fping execution payloads into canonical host observations."""

    tool_names = ("information_gathering.network_discovery.fping",)
    capability_families = ("network_discovery",)

    def supports(self, context: AdapterContext) -> bool:
        source_tool = context.source_tool_name()
        if source_tool in self.tool_names:
            return True
        return context.capability_family() in self.capability_families

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        semantic = self._extract_from_semantic_observations(context)
        if semantic:
            return dedupe_observations(semantic)

        evidence_refs = resolve_evidence_refs(context)
        observations: list[ObservationCreate] = []

        for ip in self._extract_alive_ips_from_tool_metadata(context.tool_metadata):
            payload: dict[str, Any] = {
                "source": "fping",
                "host_status": "up",
                "probe_protocol": "icmp",
            }
            if evidence_refs:
                payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type="network.host_discovered",
                    subject_type="host.ip",
                    subject_key=build_host_subject_key(ip),
                    payload=payload,
                )
            )

        return dedupe_observations(observations)

    def _extract_from_semantic_observations(
        self,
        context: AdapterContext,
    ) -> list[ObservationCreate]:
        canonical: list[ObservationCreate] = []
        evidence_refs = resolve_evidence_refs(context)
        allowed_types = {"network.host_discovered"}
        for item in context.semantic_observations:
            if not isinstance(item, Mapping):
                continue
            obs_type = str(item.get("observation_type") or "").strip().lower()
            subject_type = str(item.get("subject_type") or "").strip().lower()
            subject_key = str(item.get("subject_key") or "").strip().lower()
            if obs_type not in allowed_types or subject_type != "host.ip":
                continue
            ip = self._extract_ip_from_subject_key(subject_key)
            if ip is None:
                continue
            payload_raw = item.get("payload")
            payload = dict(payload_raw) if isinstance(payload_raw, Mapping) else {}
            if evidence_refs and not isinstance(payload.get("evidence_refs"), list):
                payload["evidence_refs"] = list(evidence_refs)
            canonical.append(
                make_observation(
                    context=context,
                    observation_type=obs_type,
                    subject_type="host.ip",
                    subject_key=build_host_subject_key(ip),
                    payload=payload,
                )
            )
        return canonical

    @staticmethod
    def _extract_ip_from_subject_key(subject_key: str) -> str | None:
        if not subject_key.startswith("host.ip:"):
            return None
        candidate = subject_key.split(":", 1)[1].strip().lower()
        if not candidate:
            return None
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _extract_alive_ips_from_tool_metadata(
        tool_metadata: Mapping[str, Any],
    ) -> list[str]:
        alive_raw = tool_metadata.get("alive_hosts")
        if not isinstance(alive_raw, Iterable) or isinstance(alive_raw, (str, bytes)):
            return []
        ordered: list[str] = []
        seen: set[str] = set()
        for value in alive_raw:
            if not isinstance(value, str):
                continue
            candidate = value.strip().lower()
            if not candidate:
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                # MVP: drop non-IP entries (hostnames, dead markers, junk).
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            ordered.append(candidate)
        return ordered
