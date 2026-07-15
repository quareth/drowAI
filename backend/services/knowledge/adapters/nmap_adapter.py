"""Deterministic adapter for nmap network discovery output.

This adapter normalizes nmap metadata/evidence into canonical observations:
- network.host_discovered
- network.open_port
- network.service_detected"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from ..contracts import ObservationCreate
from .base import AdapterContext
from .network_common import (
    build_host_subject_key,
    build_service_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    normalize_service_version,
    normalize_port,
    normalize_transport_protocol,
    resolve_evidence_refs,
    split_product_hint,
)


_HOST_LINE_RE = re.compile(r"^Nmap scan report for\s+([^\s(]+)")
_OPEN_PORT_LINE_RE = re.compile(
    r"^\s*(\d{1,5})/([a-z]+)\s+open\s+([^\s]+)(?:\s+(.*))?$",
    re.IGNORECASE,
)


class NmapKnowledgeAdapter:
    """Normalize nmap execution payloads into canonical network observations."""

    tool_names = ("information_gathering.network_discovery.nmap",)
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

        hosts = self._extract_hosts_from_tool_metadata(context.tool_metadata)
        if not hosts:
            hosts = self._extract_hosts_from_artifact_text(context)

        for host in hosts:
            ip = str(host.get("ip") or "").strip().lower()
            if not ip:
                continue
            host_payload = {"source": "nmap"}
            if evidence_refs:
                host_payload["evidence_refs"] = evidence_refs
            observations.append(
                make_observation(
                    context=context,
                    observation_type="network.host_discovered",
                    subject_type="host.ip",
                    subject_key=build_host_subject_key(ip),
                    payload=host_payload,
                )
            )

            for port_row in host.get("ports", []):
                if not isinstance(port_row, Mapping):
                    continue
                port = normalize_port(port_row.get("port"))
                if port is None:
                    continue
                protocol = normalize_transport_protocol(port_row.get("protocol"))
                if protocol is None:
                    continue
                service_name = str(port_row.get("service") or "").strip().lower()
                service_key = build_service_subject_key(ip, protocol, port)

                port_payload: dict[str, Any] = {
                    "ip": ip,
                    "protocol": protocol,
                    "port": port,
                    "source": "nmap",
                }
                if evidence_refs:
                    port_payload["evidence_refs"] = evidence_refs
                observations.append(
                    make_observation(
                        context=context,
                        observation_type="network.open_port",
                        subject_type="service.socket",
                        subject_key=service_key,
                        payload=port_payload,
                    )
                )

                if service_name and service_name not in {"unknown", "?"}:
                    service_payload: dict[str, Any] = {
                        "service_name": service_name,
                        "source": "nmap",
                    }
                    product = str(port_row.get("product") or "").strip()
                    version = str(port_row.get("version") or "").strip()
                    normalized_version = version or None
                    version_raw = str(port_row.get("version_raw") or "").strip() or None
                    version_relation = str(port_row.get("version_relation") or "").strip() or None
                    if normalized_version is None:
                        normalized_version, version_raw, version_relation = normalize_service_version(version)
                    if product:
                        service_payload["product"] = product
                    if normalized_version:
                        service_payload["version"] = normalized_version
                    if version_raw:
                        service_payload["version_raw"] = version_raw
                    if version_relation:
                        service_payload["version_relation"] = version_relation
                    product_hint = str(port_row.get("product_hint") or "").strip()
                    if product_hint:
                        service_payload["product_hint"] = product_hint
                    if evidence_refs:
                        service_payload["evidence_refs"] = evidence_refs
                    observations.append(
                        make_observation(
                            context=context,
                            observation_type="network.service_detected",
                            subject_type="service.socket",
                            subject_key=service_key,
                            payload=service_payload,
                        )
                    )

        return dedupe_observations(observations)

    def _extract_from_semantic_observations(
        self,
        context: AdapterContext,
    ) -> list[ObservationCreate]:
        canonical: list[ObservationCreate] = []
        evidence_refs = resolve_evidence_refs(context)
        allowed_types = {
            "network.host_discovered",
            "network.open_port",
            "network.service_detected",
            "network.host_profiled",
            "network.service_profiled",
            "finding.vulnerability_detected",
        }
        for item in context.semantic_observations:
            if not isinstance(item, Mapping):
                continue
            obs_type = str(item.get("observation_type") or "").strip().lower()
            subject_type = str(item.get("subject_type") or "").strip().lower()
            subject_key = str(item.get("subject_key") or "").strip().lower()
            if obs_type not in allowed_types or not subject_type or not subject_key:
                continue
            payload_raw = item.get("payload")
            payload = dict(payload_raw) if isinstance(payload_raw, Mapping) else {}
            if evidence_refs and not isinstance(payload.get("evidence_refs"), list):
                payload["evidence_refs"] = list(evidence_refs)
            canonical.append(
                make_observation(
                    context=context,
                    observation_type=obs_type,
                    subject_type=subject_type,
                    subject_key=subject_key,
                    payload=payload,
                )
            )
        return canonical

    @staticmethod
    def _extract_hosts_from_tool_metadata(tool_metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        hosts_raw = tool_metadata.get("hosts")
        if not isinstance(hosts_raw, list):
            return []

        hosts: list[dict[str, Any]] = []
        for host_row in hosts_raw:
            if not isinstance(host_row, Mapping):
                continue
            ip = str(host_row.get("ip") or "").strip().lower()
            if not ip:
                continue
            ports_raw = host_row.get("ports")
            parsed_ports: list[dict[str, Any]] = []
            if isinstance(ports_raw, list):
                for port_row in ports_raw:
                    if not isinstance(port_row, Mapping):
                        continue
                    raw_version = str(port_row.get("version") or "").strip()
                    normalized_version, normalized_raw, version_relation = normalize_service_version(raw_version)
                    parsed_ports.append(
                        {
                            "port": normalize_port(port_row.get("port")),
                            "protocol": normalize_transport_protocol(port_row.get("protocol")),
                            "service": str(port_row.get("service") or "").strip().lower(),
                            "product": str(port_row.get("product") or "").strip(),
                            "version": normalized_version or raw_version,
                            "version_raw": normalized_raw or "",
                            "version_relation": version_relation or "",
                            "product_hint": " ".join(
                                part
                                for part in (
                                    str(port_row.get("product") or "").strip(),
                                    raw_version,
                                )
                                if part
                            ).strip(),
                        }
                    )
            hosts.append({"ip": ip, "ports": parsed_ports})
        return hosts

    def _extract_hosts_from_artifact_text(self, context: AdapterContext) -> list[dict[str, Any]]:
        parsed_hosts: dict[str, dict[str, Any]] = {}
        for _artifact_id, content in collect_artifact_text_blobs(context):
            self._merge_hosts_from_nmap_text(parsed_hosts, content.splitlines())
        return list(parsed_hosts.values())

    @staticmethod
    def _merge_hosts_from_nmap_text(
        parsed_hosts: dict[str, dict[str, Any]],
        lines: Iterable[str],
    ) -> None:
        current_host: str | None = None
        for raw_line in lines:
            line = str(raw_line or "").strip()
            if not line:
                continue
            host_match = _HOST_LINE_RE.match(line)
            if host_match:
                current_host = host_match.group(1).strip().lower()
                if current_host and current_host not in parsed_hosts:
                    parsed_hosts[current_host] = {"ip": current_host, "ports": []}
                continue
            if current_host is None:
                continue
            port_match = _OPEN_PORT_LINE_RE.match(line)
            if not port_match:
                continue
            port = normalize_port(port_match.group(1))
            if port is None:
                continue
            protocol = normalize_transport_protocol(port_match.group(2))
            if protocol is None:
                continue
            service_name = str(port_match.group(3) or "").strip().lower()
            product_hint = str(port_match.group(4) or "").strip()
            product, version = split_product_hint(product_hint)
            normalized_version, _, _ = normalize_service_version(version or "")
            parsed_hosts[current_host]["ports"].append(
                {
                    "port": port,
                    "protocol": protocol,
                    "service": service_name,
                    "product": product or "",
                    "version": normalized_version or version or "",
                    "product_hint": product_hint,
                }
            )
