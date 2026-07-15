"""Deterministic adapter for masscan network discovery output.

This adapter normalizes masscan metadata/evidence into canonical observations:
- network.host_discovered
- network.open_port
- network.service_detected (only when a concrete service name exists)"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..contracts import ObservationCreate
from .base import AdapterContext
from .network_common import (
    build_host_subject_key,
    build_service_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    normalize_port,
    normalize_transport_protocol,
    resolve_evidence_refs,
)


class MasscanKnowledgeAdapter:
    """Normalize masscan execution payloads into canonical network observations."""

    tool_names = ("information_gathering.network_discovery.masscan",)
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

        host_ips = self._extract_hosts_from_tool_metadata(context.tool_metadata)
        open_ports = self._extract_open_ports_from_tool_metadata(context.tool_metadata)
        ip_resolved_ports = [item for item in open_ports if item.get("ip")]

        if not ip_resolved_ports:
            open_ports = self._extract_open_ports_from_artifact_text(context)
            host_ips.update(item["ip"] for item in open_ports if item.get("ip"))
        else:
            open_ports = ip_resolved_ports

        if open_ports and len(host_ips) == 1:
            # parse_output() may not carry IP per open_port entry; map safely only for single-host runs.
            single_ip = next(iter(host_ips))
            for item in open_ports:
                item.setdefault("ip", single_ip)

        for ip in sorted(host_ips):
            if not ip:
                continue
            host_payload: dict[str, Any] = {"source": "masscan"}
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

        for port_row in open_ports:
            ip = str(port_row.get("ip") or "").strip().lower()
            if not ip:
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
                "source": "masscan",
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
                    "source": "masscan",
                }
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
        allowed_types = {
            "network.host_discovered",
            "network.open_port",
            "network.service_detected",
        }
        for item in context.semantic_observations:
            if not isinstance(item, Mapping):
                continue
            obs_type = str(item.get("observation_type") or "").strip().lower()
            subject_type = str(item.get("subject_type") or "").strip().lower()
            subject_key = str(item.get("subject_key") or "").strip().lower()
            if obs_type not in allowed_types or not subject_type or not subject_key:
                continue
            payload = item.get("payload")
            canonical.append(
                make_observation(
                    context=context,
                    observation_type=obs_type,
                    subject_type=subject_type,
                    subject_key=subject_key,
                    payload=payload if isinstance(payload, Mapping) else {},
                )
            )
        return canonical

    @staticmethod
    def _extract_hosts_from_tool_metadata(tool_metadata: Mapping[str, Any]) -> set[str]:
        hosts_raw = tool_metadata.get("hosts")
        if not isinstance(hosts_raw, list):
            return set()
        hosts: set[str] = set()
        for host_row in hosts_raw:
            if not isinstance(host_row, Mapping):
                continue
            ip = str(host_row.get("ip") or "").strip().lower()
            if ip:
                hosts.add(ip)
        return hosts

    @staticmethod
    def _extract_open_ports_from_tool_metadata(tool_metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        open_ports = tool_metadata.get("open_ports")
        if not isinstance(open_ports, list):
            return rows
        for item in open_ports:
            if not isinstance(item, Mapping):
                continue
            rows.append(
                {
                    "ip": str(item.get("ip") or "").strip().lower(),
                    "port": normalize_port(item.get("port")),
                    "protocol": normalize_transport_protocol(item.get("protocol")),
                    "service": str(item.get("service") or "").strip().lower(),
                }
            )
        return rows

    def _extract_open_ports_from_artifact_text(self, context: AdapterContext) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _artifact_id, content in collect_artifact_text_blobs(context):
            rows.extend(self._parse_masscan_json_objects(content))
        return rows

    @staticmethod
    def _parse_masscan_json_objects(content: str) -> list[dict[str, Any]]:
        text = str(content or "").strip()
        if not text:
            return []

        objects: list[dict[str, Any]] = []
        if text.startswith("["):
            try:
                payload = json.loads(text)
                if isinstance(payload, list):
                    objects.extend(item for item in payload if isinstance(item, dict))
            except json.JSONDecodeError:
                pass

        if not objects:
            for raw_line in text.splitlines():
                line = raw_line.strip().rstrip(",")
                if not line or line in {"[", "]"} or not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    objects.append(row)

        rows: list[dict[str, Any]] = []
        for row in objects:
            ip = str(row.get("ip") or "").strip().lower()
            ports = row.get("ports")
            if not ip or not isinstance(ports, list):
                continue
            for port_row in ports:
                if not isinstance(port_row, Mapping):
                    continue
                status = str(port_row.get("status") or "").strip().lower()
                if status and status != "open":
                    continue
                rows.append(
                    {
                        "ip": ip,
                        "port": normalize_port(port_row.get("port")),
                        "protocol": normalize_transport_protocol(port_row.get("proto")),
                        "service": str(port_row.get("service") or "").strip().lower(),
                    }
                )
        return rows
