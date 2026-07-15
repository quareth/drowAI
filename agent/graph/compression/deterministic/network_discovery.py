"""Network-discovery deterministic compression helpers.

This module projects parsed network discovery metadata into compact host
summaries. Nmap uses the masscan-style host/port metadata shape, while fping
uses its host-liveness analysis model. Hidden masscan remains reference-only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS
from agent.tools.information_gathering.network_discovery.fping_analysis import (
    FpingAnalysis,
    analyze_fping_metadata,
)

from .common import (
    as_int,
    compact_evidence_line,
    dedupe_string_list,
    sanitize_artifact_refs,
)
from .contracts import CompressionInput, DeterministicCompressionResult

NMAP_TOOL_ID = "information_gathering.network_discovery.nmap"
FPING_TOOL_ID = "information_gathering.network_discovery.fping"
MASSCAN_REFERENCE_TOOL_ID = "information_gathering.network_discovery.masscan"

_REGISTERED_NETWORK_DISCOVERY_TOOL_IDS: tuple[str, ...] = (NMAP_TOOL_ID, FPING_TOOL_ID)
_HOST_LIMIT = 5
_PORT_LIMIT = 10
_ARTIFACT_REF_LIMIT = 3
_SEMANTIC_EVIDENCE_LIMIT = 5
_SEMANTIC_OBSERVATION_LIMIT = 5


def network_discovery_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project parsed network-discovery metadata into host/port summaries."""

    if input_data.tool_name == FPING_TOOL_ID:
        return _fping_adapter(input_data)

    if input_data.tool_name != NMAP_TOOL_ID:
        return DeterministicCompressionResult.none(
            fallback_reason="unsupported_network_discovery_tool",
        )

    metadata = _network_discovery_metadata(input_data.raw_result)
    raw_metadata = _mapping_or_empty(input_data.raw_result.get("metadata"))
    if not metadata:
        return DeterministicCompressionResult.none(
            fallback_reason="no_network_discovery_metadata",
        )

    error = _text_or_none(metadata.get("error"))
    if error:
        compact_error = compact_evidence_line(error)
        return DeterministicCompressionResult(
            summary=_summary(f"Nmap metadata parse failed: {compact_error}."),
            errors=(compact_error,),
            structured_signals=(
                {
                    "type": "error_context",
                    "message": f"Nmap metadata parse failed: {compact_error}",
                },
            ),
            completeness="partial",
            lossiness_risk="low",
        )

    host_findings = _host_findings(metadata)
    port_summaries = _port_summaries(metadata)
    open_port_count = _open_port_count(metadata, fallback=len(port_summaries))
    hosts_total = as_int(metadata.get("hosts_total"))
    hosts_up = as_int(metadata.get("hosts_up"))
    hosts_down = as_int(metadata.get("hosts_down"))

    if not _has_nmap_metadata(
        hosts_total=hosts_total,
        hosts_up=hosts_up,
        hosts_down=hosts_down,
        host_findings=host_findings,
        port_summaries=port_summaries,
    ):
        return DeterministicCompressionResult.none(
            fallback_reason="no_network_discovery_host_port_metadata",
        )

    artifact_findings = _artifact_findings(input_data.raw_result, metadata=metadata)
    findings = tuple((host_findings + artifact_findings)[: _HOST_LIMIT + _ARTIFACT_REF_LIMIT])
    if not findings and open_port_count == 0:
        findings = ("Nmap metadata contained no hosts and no open ports.",)

    semantic_evidence = _semantic_evidence_lines(raw_metadata, input_data.raw_result)
    semantic_observations = _semantic_observation_lines(
        raw_metadata,
        input_data.raw_result,
    )
    summary = _summary(
        _build_summary(
            hosts_total=hosts_total,
            hosts_up=hosts_up,
            open_port_count=open_port_count,
        )
    )
    evidence = tuple(
        compact_evidence_line(value) for value in (
            port_summaries[:_PORT_LIMIT] + semantic_evidence[:_SEMANTIC_EVIDENCE_LIMIT]
            + semantic_observations[:_SEMANTIC_OBSERVATION_LIMIT]
        )
        if value
    )

    return DeterministicCompressionResult(
        summary=summary,
        key_findings=findings,
        structured_signals=tuple(
            _structured_signals(
                metadata,
                hosts_total=hosts_total,
                hosts_up=hosts_up,
                hosts_down=hosts_down,
                open_port_count=open_port_count,
                raw_result=input_data.raw_result,
            )
        ),
        decision_evidence=evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def registered_network_discovery_tool_ids() -> tuple[str, ...]:
    """Return network-discovery tool ids registered for deterministic MVP coverage."""

    return _REGISTERED_NETWORK_DISCOVERY_TOOL_IDS


def register_network_discovery_adapters() -> None:
    """Register visible network-discovery adapters without exposing masscan."""

    from .registry import register_adapter

    register_adapter(NMAP_TOOL_ID, network_discovery_adapter)
    register_adapter(FPING_TOOL_ID, network_discovery_adapter)


def _fping_adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
    """Project parsed fping metadata into compact host-liveness facts."""

    metadata = _mapping_or_empty(input_data.raw_result.get("metadata"))
    if not _looks_like_fping_metadata(metadata):
        return DeterministicCompressionResult.none(
            fallback_reason="no_fping_metadata",
        )

    analysis = analyze_fping_metadata(metadata)
    findings = _fping_key_findings(analysis)
    if not findings:
        findings = ("fping metadata contained no alive hosts.",)
    findings.extend(_artifact_findings(input_data.raw_result, metadata=metadata))

    evidence = tuple(
        compact_evidence_line(value)
        for value in (
            _fping_decision_evidence(analysis)
            + _semantic_observation_lines(metadata, input_data.raw_result)
        )
        if value
    )

    return DeterministicCompressionResult(
        summary=_summary(_fping_summary(analysis)),
        key_findings=tuple(findings),
        structured_signals=tuple(
            _fping_structured_signals(
                analysis,
                raw_result=input_data.raw_result,
                metadata=metadata,
            )
        ),
        decision_evidence=evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def _network_discovery_metadata(raw_result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return parsed network-discovery metadata from a tool result."""

    metadata = raw_result.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}

    nested_nmap = metadata.get("nmap")
    if isinstance(nested_nmap, Mapping):
        return nested_nmap

    if _looks_like_host_port_metadata(metadata):
        return metadata
    return {}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    """Return mapping values without copying non-mappings."""

    return value if isinstance(value, Mapping) else {}


def _looks_like_host_port_metadata(metadata: Mapping[str, Any]) -> bool:
    """Return whether metadata follows the masscan-style host/port shape."""

    return any(
        key in metadata
        for key in ("hosts", "open_ports", "hosts_up", "hosts_total", "hosts_down")
    )


def _looks_like_fping_metadata(metadata: Mapping[str, Any]) -> bool:
    """Return whether metadata follows the fping host-liveness shape."""

    return any(
        key in metadata
        for key in ("alive_hosts", "alive_count", "unresponsive_count", "diagnostics")
    )


def _fping_summary(analysis: FpingAnalysis) -> str:
    """Build a bounded fping liveness summary."""

    summary = f"fping found {analysis.alive_count} alive hosts"
    if analysis.unresponsive_count is not None:
        summary += f"; {analysis.unresponsive_count} unresponsive hosts"
    else:
        summary += "; unresponsive host count unknown"
    return summary + "."


def _fping_key_findings(analysis: FpingAnalysis) -> list[str]:
    """Return compact fping host-liveness findings."""

    findings: list[str] = []
    if analysis.alive_hosts:
        findings.append(
            "alive hosts: "
            + ", ".join(analysis.alive_hosts[:_HOST_LIMIT])
            + (
                f" (+{len(analysis.alive_hosts) - _HOST_LIMIT} more)"
                if len(analysis.alive_hosts) > _HOST_LIMIT
                else ""
            )
        )
    if analysis.unresponsive_count is not None:
        findings.append(f"unresponsive hosts: {analysis.unresponsive_count}")
    if analysis.diagnostics:
        findings.extend(
            f"diagnostic: {compact_evidence_line(line)}"
            for line in analysis.diagnostics[:_HOST_LIMIT]
        )
    return findings


def _fping_decision_evidence(analysis: FpingAnalysis) -> list[str]:
    """Return bounded fping decision evidence lines."""

    evidence = [
        f"fping liveness: alive={analysis.alive_count}"
        + (
            f" unresponsive={analysis.unresponsive_count}"
            if analysis.unresponsive_count is not None
            else " unresponsive=unknown"
        )
    ]
    for host in analysis.alive_hosts[:_HOST_LIMIT]:
        evidence.append(f"alive host: {host}")
    return evidence


def _fping_structured_signals(
    analysis: FpingAnalysis,
    *,
    raw_result: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return structured signals for parsed fping facts."""

    signals: list[Mapping[str, Any]] = [
        {"type": "kv_pair", "key": "fping_alive_count", "value": analysis.alive_count},
    ]
    if analysis.unresponsive_count is not None:
        signals.append(
            {
                "type": "kv_pair",
                "key": "fping_unresponsive_count",
                "value": analysis.unresponsive_count,
            }
        )
    for host in analysis.alive_hosts[:_HOST_LIMIT]:
        signals.append({"type": "host", "host": host, "status": "up"})
    for ref in _artifact_refs(raw_result, metadata=metadata):
        signals.append(
            {
                "type": "kv_pair",
                "key": "fping_artifact_ref",
                "value": ref["path"],
            }
        )
    return signals


def _has_nmap_metadata(
    *,
    hosts_total: Optional[int],
    hosts_up: Optional[int],
    hosts_down: Optional[int],
    host_findings: list[str],
    port_summaries: list[str],
) -> bool:
    """Return whether parsed metadata contains explicit host/port facts."""

    return any(
        value is not None
        for value in (hosts_total, hosts_up, hosts_down)
    ) or bool(host_findings or port_summaries)


def _host_findings(metadata: Mapping[str, Any]) -> list[str]:
    """Return bounded host-grouped open-port findings from nmap metadata."""

    summaries: list[str] = []
    for host in _mapping_items(metadata.get("hosts")):
        ip = _first_text(host.get("ip"), host.get("address"), host.get("host"))
        if not ip:
            continue
        status = _text_or_none(host.get("status"))
        hostnames = dedupe_string_list(_iterable_or_empty(host.get("hostnames")), limit=3)
        os_top_guess = _text_or_none(host.get("os_top_guess"))
        ports = _mapping_items(host.get("ports"))
        ports_count = as_int(host.get("ports_count"))
        port_count = ports_count if ports_count is not None else len(ports)

        profile_bits: list[str] = []
        if hostnames:
            profile_bits.append(f"names={', '.join(hostnames)}")
        if os_top_guess:
            profile_bits.append(f"os={os_top_guess}")

        host_label = ip
        if status or profile_bits:
            host_label += f" ({', '.join([part for part in (status, *profile_bits) if part])})"

        port_details = [
            _format_host_grouped_port(port)
            for port in ports[:_PORT_LIMIT]
        ]
        port_details = [detail for detail in port_details if detail]

        if port_details:
            summary = (
                f"host {host_label}: {port_count} open ports - "
                f"{', '.join(port_details)}"
            )
        else:
            summary = f"host {host_label}: {port_count} open ports"

        if summary not in summaries:
            summaries.append(summary)
        if len(summaries) >= _HOST_LIMIT:
            break
    return summaries


def _port_summaries(metadata: Mapping[str, Any]) -> list[str]:
    """Return bounded open-port summaries, preferring host-attached ports."""

    with_hosts = _host_attached_port_summaries(metadata)
    if with_hosts:
        return with_hosts
    return _flat_open_port_summaries(metadata)


def _open_port_count(metadata: Mapping[str, Any], *, fallback: int) -> int:
    """Return total open ports from parsed metadata, independent of evidence caps."""

    open_ports = _mapping_items(metadata.get("open_ports"))
    if open_ports:
        return len(open_ports)

    count = 0
    for host in _mapping_items(metadata.get("hosts")):
        count += len(_mapping_items(host.get("ports")))
    return count if count else fallback


def _host_attached_port_summaries(metadata: Mapping[str, Any]) -> list[str]:
    """Return nmap-style host/ip plus port summaries."""

    summaries: list[str] = []
    for host in _mapping_items(metadata.get("hosts")):
        ip = _first_text(host.get("ip"), host.get("address"), host.get("host"))
        for port in _mapping_items(host.get("ports")):
            summary = _format_port_summary(port, ip=ip)
            if summary and summary not in summaries:
                summaries.append(summary)
            if len(summaries) >= _PORT_LIMIT:
                return summaries
    return summaries


def _flat_open_port_summaries(metadata: Mapping[str, Any]) -> list[str]:
    """Return masscan-style flat open port summaries."""

    summaries: list[str] = []
    for port in _mapping_items(metadata.get("open_ports")):
        summary = _format_port_summary(port, ip=None)
        if summary and summary not in summaries:
            summaries.append(summary)
        if len(summaries) >= _PORT_LIMIT:
            break
    return summaries


def _format_port_summary(port: Mapping[str, Any], *, ip: Optional[str]) -> Optional[str]:
    """Format one port record using the shared host/port summary pattern."""

    port_number = as_int(port.get("port"))
    if port_number is None:
        return None
    protocol = _text_or_none(port.get("protocol")) or _text_or_none(port.get("proto")) or "tcp"
    service = _text_or_none(port.get("service")) or "unknown"
    status = _text_or_none(port.get("status")) or "open"
    product = _text_or_none(port.get("product"))
    version = _text_or_none(port.get("version"))

    endpoint = f"{protocol}/{port_number}"
    if ip:
        endpoint = f"{ip}:{endpoint}"

    summary = f"open port: {endpoint} {service} {status}"
    details = " ".join(part for part in (product, version) if part)
    if details:
        summary += f" ({details})"
    return summary


def _format_host_grouped_port(port: Mapping[str, Any]) -> Optional[str]:
    """Format one port record for a host-grouped key finding."""

    port_number = as_int(port.get("port"))
    if port_number is None:
        return None
    protocol = _text_or_none(port.get("protocol")) or _text_or_none(port.get("proto")) or "tcp"
    service = _text_or_none(port.get("service")) or "unknown"
    status = _text_or_none(port.get("status")) or "open"
    product = _text_or_none(port.get("product"))
    version = _text_or_none(port.get("version"))
    profile = _mapping_or_empty(port.get("service_profile"))
    http_title = _text_or_none(profile.get("http_title"))
    server_header = _text_or_none(profile.get("server_header"))

    detail_bits = []
    product_version = " ".join(part for part in (product, version) if part)
    if product_version:
        detail_bits.append(product_version)
    if http_title:
        detail_bits.append(f"title={http_title}")
    if server_header:
        detail_bits.append(f"server={server_header}")

    summary = f"{protocol}/{port_number} {service} {status}"
    if detail_bits:
        summary += f" ({'; '.join(detail_bits)})"
    return summary


def _structured_signals(
    metadata: Mapping[str, Any],
    *,
    hosts_total: Optional[int],
    hosts_up: Optional[int],
    hosts_down: Optional[int],
    open_port_count: int,
    raw_result: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return canonical schema.py structured signals for parsed nmap facts."""

    signals: list[Mapping[str, Any]] = []
    for key, value in (
        ("hosts_total", hosts_total),
        ("hosts_up", hosts_up),
        ("hosts_down", hosts_down),
        ("open_port_count", open_port_count),
    ):
        if value is not None:
            signals.append({"type": "kv_pair", "key": key, "value": value})

    for host in _mapping_items(metadata.get("hosts")):
        ip = _first_text(host.get("ip"), host.get("address"), host.get("host"))
        hostnames = dedupe_string_list(_iterable_or_empty(host.get("hostnames")), limit=3)
        if ip and hostnames:
            signals.append(
                {
                    "type": "kv_pair",
                    "key": f"hostnames:{ip}",
                    "value": ", ".join(hostnames),
                }
            )
        for port in _mapping_items(host.get("ports")):
            signal = _service_signal(port, ip=ip)
            if signal:
                signals.append(signal)
            if len(signals) >= 25:
                return signals

    for ref in _artifact_refs(raw_result, metadata=metadata):
        signals.append(
            {
                "type": "kv_pair",
                "key": "nmap_artifact_ref",
                "value": ref["path"],
            }
        )
        if len(signals) >= 25:
            break

    return signals


def _service_signal(port: Mapping[str, Any], *, ip: Optional[str]) -> Optional[Mapping[str, Any]]:
    """Return one canonical service signal for an open nmap port."""

    port_number = as_int(port.get("port"))
    if port_number is None:
        return None
    protocol = _text_or_none(port.get("protocol")) or _text_or_none(port.get("proto")) or "tcp"
    service = _text_or_none(port.get("service"))
    product = _text_or_none(port.get("product"))
    version = _text_or_none(port.get("version"))
    product_version = " ".join(part for part in (product, version) if part)

    signal: dict[str, Any] = {
        "type": "service",
        "port": port_number,
        "protocol": protocol,
        "state": _text_or_none(port.get("status")) or "open",
    }
    if ip:
        signal["target"] = ip
    if service:
        signal["service"] = service
    if product_version:
        signal["version"] = product_version
    return signal


def _artifact_findings(
    raw_result: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
) -> list[str]:
    """Return bounded sanitized artifact findings for nmap drill-down refs."""

    return [
        f"artifact: {ref['path']}"
        for ref in _artifact_refs(raw_result, metadata=metadata)
    ]


def _artifact_refs(
    raw_result: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Return sanitized artifact refs carried by raw result or metadata."""

    candidates: list[Mapping[str, Any]] = []
    raw_artifacts = raw_result.get("artifacts")
    if isinstance(raw_artifacts, list):
        for artifact in raw_artifacts:
            if isinstance(artifact, Mapping):
                candidates.append(artifact)
            elif isinstance(artifact, str):
                candidates.append({"path": artifact})

    metadata_artifacts = metadata.get("artifacts")
    if isinstance(metadata_artifacts, list):
        for artifact in metadata_artifacts:
            if isinstance(artifact, Mapping):
                candidates.append(artifact)
            elif isinstance(artifact, str):
                candidates.append({"path": artifact})

    return sanitize_artifact_refs(candidates)[:_ARTIFACT_REF_LIMIT]


def _semantic_evidence_lines(
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    """Return bounded semantic evidence facts from runtime metadata envelopes."""

    evidence = metadata.get("semantic_evidence")
    if not isinstance(evidence, list):
        evidence = raw_result.get("semantic_evidence")
    if not isinstance(evidence, list):
        return []

    lines: list[str] = []
    for entry in evidence:
        if not isinstance(entry, Mapping):
            continue
        name = _text_or_none(entry.get("name"))
        if not name:
            continue
        value = entry.get("value")
        value_text = _text_or_none(value)
        if value_text is None and value is not None:
            value_text = str(value)
        line = f"semantic evidence: {name}"
        if value_text:
            line += f"={value_text}"
        line = compact_evidence_line(line)
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= _SEMANTIC_EVIDENCE_LIMIT:
            break
    return lines


def _semantic_observation_lines(
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    """Return bounded semantic observation facts from runtime metadata envelopes."""

    observations = metadata.get("semantic_observations")
    if not isinstance(observations, list):
        observations = raw_result.get("semantic_observations")
    if not isinstance(observations, list):
        return []

    lines: list[str] = []
    for entry in observations:
        if not isinstance(entry, Mapping):
            continue
        observation_type = _text_or_none(entry.get("observation_type"))
        if not observation_type:
            continue
        subject_key = _text_or_none(entry.get("subject_key"))
        line = f"semantic observation: {observation_type}"
        if subject_key:
            line += f" {subject_key}"
        line = compact_evidence_line(line)
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= _SEMANTIC_OBSERVATION_LIMIT:
            break
    return lines


def _build_summary(
    *,
    hosts_total: Optional[int],
    hosts_up: Optional[int],
    open_port_count: int,
) -> str:
    """Build a bounded nmap scan summary."""

    host_bits: list[str] = []
    if hosts_up is not None:
        host_bits.append(f"{hosts_up} hosts up")
    if hosts_total is not None:
        host_bits.append(f"{hosts_total} hosts scanned")
    if not host_bits:
        host_bits.append("host/port metadata parsed")

    return f"Nmap discovered {open_port_count} open ports; {', '.join(host_bits)}."


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    """Return mapping items from a list-like value."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _iterable_or_empty(value: Any) -> Iterable[Any]:
    """Return iterable list/tuple/set values without treating text as iterable."""

    if isinstance(value, (list, tuple, set)):
        return value
    return ()


def _first_text(*values: Any) -> Optional[str]:
    """Return the first stripped non-empty text value."""

    for value in values:
        text = _text_or_none(value)
        if text:
            return text
    return None


def _text_or_none(value: Any) -> Optional[str]:
    """Return stripped text or None."""

    text = str(value or "").strip()
    return text or None


def _summary(value: str) -> str:
    """Bound summaries to the existing compact summary size."""

    return value[:COMPACT_SUMMARY_MAX_CHARS]


register_network_discovery_adapters()
