"""Rich nmap XML parsing and semantic emission helpers.

This companion module to nmap.py owns all rich XML-to-metadata extraction
and semantic observation emission for nmap scans. It keeps nmap.py focused
on command construction, tool registration, and lifecycle, while centralizing
the bounded rich metadata contract, parsing helpers, and observation builders
in one cohesive location.

Responsibility boundaries:
- XML-to-rich-metadata parsing (host scripts, port scripts, OS, traceroute)
- Bounded normalization and deterministic ordering of rich fields
- Semantic observation construction from normalized metadata
- Curated deterministic findings allowlist for risk-bearing script results

This module does NOT own:
- nmap command construction or tool registration (nmap.py)
- Backend knowledge projection or identity merging (backend/services/knowledge/)
- Frontend rendering or formatting (client/)
"""

from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, Mapping, Optional

from agent.semantic.evidence_vocabulary import SemanticEvidenceType
from runtime_shared.semantic.canonical_keys import build_finding_vulnerability_key
from runtime_shared.semantic.network_common import normalize_service_version
from runtime_shared.semantic.service_identity import build_service_socket_key

if TYPE_CHECKING:
    from .nmap import NmapArgs


# ---------------------------------------------------------------------------
# Bounds — keep rich metadata safe for durable persistence in tool_metadata
# ---------------------------------------------------------------------------
MAX_OS_MATCHES = 3
MAX_HOST_SCRIPTS = 10
MAX_PORT_SCRIPTS = 10
MAX_TRACE_HOPS = 30
MAX_SCRIPT_SUMMARY_LEN = 256
NMAP_SEMANTIC_SCHEMA_VERSION = "nmap.v1"
NMAP_CAPABILITY_FAMILY = "network_discovery"


# ---------------------------------------------------------------------------
# Rich metadata shape contract
# ---------------------------------------------------------------------------
#
# The additive rich metadata shape preserves all existing top-level keys
# returned by parse_nmap_xml() and extends the per-host and per-port dicts
# with optional bounded fields.
#
# Top-level (unchanged):
#   open_ports:  list[dict]    -- flat list of open port dicts (backward compat)
#   hosts_up:    int           -- count of hosts that responded
#   hosts_total: int           -- total hosts scanned
#   hosts_down:  int           -- count of hosts that did not respond
#   hosts:       list[dict]    -- per-host detail dicts
#   host_status: str | None    -- status of first host (legacy single-host compat)
#
# Per-host dict (existing + new optional fields):
#   ip:                str              -- host IP address (existing)
#   addr_type:         str              -- address type e.g. "ipv4" (existing)
#   status:            str              -- host status e.g. "up" (existing)
#   ports:             list[dict]       -- open port dicts (existing)
#   hostnames:         list[str]        -- resolved hostnames, sorted (NEW, optional)
#   os_matches:        list[dict]       -- top OS guesses, bounded to MAX_OS_MATCHES (NEW, optional)
#     Each: {"name": str, "accuracy": int | None}
#     Sorted by accuracy descending, then name ascending.
#   os_top_guess:      str | None       -- name of highest-accuracy OS match (NEW, optional)
#   host_scripts:      list[dict]       -- host-level NSE script summaries, bounded (NEW, optional)
#     Each: {"script_id": str, "summary": str}
#     Sorted by script_id. summary truncated to MAX_SCRIPT_SUMMARY_LEN.
#   trace_hops:        list[dict]       -- traceroute hops, bounded to MAX_TRACE_HOPS (NEW, optional)
#     Each: {"ttl": int, "ip": str, "host": str | None, "rtt_ms": float | None}
#     Sorted by ttl ascending.
#
# Per-port dict (existing + new optional fields):
#   port:              int              -- port number (existing)
#   protocol:          str              -- "tcp" / "udp" (existing)
#   service:           str | None       -- service name (existing)
#   product:           str | None       -- product name (existing)
#   version:           str | None       -- version string (existing)
#   service_profile:   dict | None      -- rich service profile (NEW, optional)
#     http_title:        str | None     -- page title from http-title script
#     server_header:     str | None     -- Server header value
#     script_summaries:  list[dict]     -- per-port NSE script summaries, bounded
#       Each: {"script_id": str, "summary": str}
#       Sorted by script_id. summary truncated to MAX_SCRIPT_SUMMARY_LEN.


def _truncate(text: str, max_len: int = MAX_SCRIPT_SUMMARY_LEN) -> str:
    """Truncate text to max_len, appending ellipsis if truncated."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _parse_hostnames(host_el: ET.Element) -> list[str]:
    """Extract sorted unique hostnames from a host element."""
    names: set[str] = set()
    hostnames_el = host_el.find("hostnames")
    if hostnames_el is not None:
        for hn in hostnames_el.findall("hostname"):
            name = str(hn.attrib.get("name") or "").strip()
            if name:
                names.add(name)
    return sorted(names)


def _parse_os_matches(host_el: ET.Element) -> list[dict[str, Any]]:
    """Extract bounded, sorted OS match guesses from a host element.

    Returns at most MAX_OS_MATCHES entries sorted by accuracy descending,
    then name ascending for deterministic ordering.
    """
    matches: list[dict[str, Any]] = []
    for osmatch in host_el.findall("os/osmatch"):
        name = str(osmatch.attrib.get("name") or "").strip()
        accuracy_text = str(osmatch.attrib.get("accuracy") or "").strip()
        if not name:
            continue
        matches.append(
            {
                "name": name,
                "accuracy": int(accuracy_text) if accuracy_text.isdigit() else None,
            }
        )
    # Sort: highest accuracy first, then alphabetical name for ties
    matches.sort(key=lambda m: (-(m["accuracy"] or 0), m["name"]))
    return matches[:MAX_OS_MATCHES]


def _parse_script_summaries(
    parent_el: ET.Element,
    max_scripts: int,
) -> list[dict[str, str]]:
    """Extract bounded, sorted script summaries from an element's <script> children.

    Each summary is truncated to MAX_SCRIPT_SUMMARY_LEN.
    """
    summaries: list[dict[str, str]] = []
    for script_el in parent_el.findall("script"):
        script_id = str(script_el.attrib.get("id") or "").strip()
        if not script_id:
            continue
        # Use the 'output' attribute first; fall back to element text
        raw_output = str(script_el.attrib.get("output") or "").strip()
        if not raw_output:
            raw_output = str(script_el.text or "").strip()
        summary = _truncate(raw_output) if raw_output else ""
        summaries.append({"script_id": script_id, "summary": summary})
    summaries.sort(key=lambda s: s["script_id"])
    return summaries[:max_scripts]


def _parse_trace_hops(host_el: ET.Element) -> list[dict[str, Any]]:
    """Extract bounded traceroute hops from a host element.

    Returns at most MAX_TRACE_HOPS entries sorted by TTL ascending.
    """
    hops: list[dict[str, Any]] = []
    trace_el = host_el.find("trace")
    if trace_el is None:
        return hops
    for hop_el in trace_el.findall("hop"):
        ttl_text = str(hop_el.attrib.get("ttl") or "").strip()
        ip_addr = str(hop_el.attrib.get("ipaddr") or "").strip()
        if not ttl_text.isdigit() or not ip_addr:
            continue
        rtt_text = str(hop_el.attrib.get("rtt") or "").strip()
        rtt_ms: Optional[float] = None
        if rtt_text:
            try:
                rtt_ms = float(rtt_text)
            except ValueError:
                pass
        host_name = str(hop_el.attrib.get("host") or "").strip() or None
        hops.append(
            {
                "ttl": int(ttl_text),
                "ip": ip_addr,
                "host": host_name,
                "rtt_ms": rtt_ms,
            }
        )
    hops.sort(key=lambda h: h["ttl"])
    return hops[:MAX_TRACE_HOPS]


def _build_service_profile(port_el: ET.Element) -> Optional[dict[str, Any]]:
    """Build a bounded service profile from per-port script output.

    Returns None if there is no rich data to add beyond basic service fields.
    """
    script_summaries = _parse_script_summaries(port_el, MAX_PORT_SCRIPTS)

    http_title: Optional[str] = None
    server_header: Optional[str] = None

    for s in script_summaries:
        sid = s["script_id"]
        summary = s["summary"]
        if sid == "http-title" and summary:
            http_title = summary
        elif sid == "http-server-header" and summary:
            server_header = summary

    if not script_summaries and not http_title and not server_header:
        return None

    profile: dict[str, Any] = {}
    if http_title is not None:
        profile["http_title"] = http_title
    if server_header is not None:
        profile["server_header"] = server_header
    if script_summaries:
        profile["script_summaries"] = script_summaries
    return profile


def enrich_host(host_el: ET.Element, host_info: dict[str, Any]) -> None:
    """Add rich metadata fields to an existing host_info dict in-place.

    This is the single authority for host-level rich field extraction.
    It mutates host_info to add optional bounded fields when the XML
    contains the relevant data.
    """
    # Hostnames
    hostnames = _parse_hostnames(host_el)
    if hostnames:
        host_info["hostnames"] = hostnames

    # OS matches
    os_matches = _parse_os_matches(host_el)
    if os_matches:
        host_info["os_matches"] = os_matches
        host_info["os_top_guess"] = os_matches[0]["name"]

    # Host-level scripts
    host_scripts = _parse_script_summaries(host_el, MAX_HOST_SCRIPTS)
    if host_scripts:
        host_info["host_scripts"] = host_scripts

    # Traceroute
    trace_hops = _parse_trace_hops(host_el)
    if trace_hops:
        host_info["trace_hops"] = trace_hops


def enrich_port(port_el: ET.Element, port_info: dict[str, Any]) -> None:
    """Add rich service profile to an existing port_info dict in-place.

    This is the single authority for port-level rich field extraction.
    """
    profile = _build_service_profile(port_el)
    if profile is not None:
        port_info["service_profile"] = profile


# ---------------------------------------------------------------------------
# Semantic observation builders
# ---------------------------------------------------------------------------

def build_host_profiled_observation(
    ip: str,
    host_info: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Build a network.host_profiled observation from enriched host metadata.

    Returns None if there is no rich profiling data beyond basic inventory.
    """
    payload: dict[str, Any] = {}

    host_status = host_info.get("status")
    if host_status:
        payload["host_status"] = host_status

    hostnames = host_info.get("hostnames")
    if hostnames:
        payload["hostnames"] = hostnames

    os_top_guess = host_info.get("os_top_guess")
    if os_top_guess:
        payload["os_top_guess"] = os_top_guess

    os_matches = host_info.get("os_matches")
    if os_matches:
        payload["os_matches"] = os_matches

    host_scripts = host_info.get("host_scripts")
    if host_scripts:
        payload["host_script_summaries"] = host_scripts

    trace_hops = host_info.get("trace_hops")
    if trace_hops:
        payload["trace_summary"] = {
            "hop_count": len(trace_hops),
            "hops": trace_hops,
        }

    # Only emit if we have enrichment beyond basic host_status
    rich_keys = {"hostnames", "os_top_guess", "os_matches", "host_script_summaries", "trace_summary"}
    if not any(k in payload for k in rich_keys):
        return None

    return {
        "observation_type": "network.host_profiled",
        "subject_type": "host.ip",
        "subject_key": f"host.ip:{ip}",
        "payload": payload,
    }


def build_service_profiled_observation(
    ip: str,
    port_info: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Build a network.service_profiled observation from enriched port metadata.

    Returns None if there is no rich profile data beyond basic service fields.
    """
    service_profile = port_info.get("service_profile")
    if not service_profile:
        return None

    port = port_info.get("port")
    protocol = port_info.get("protocol", "tcp")

    payload: dict[str, Any] = {}

    service_name = port_info.get("service")
    if service_name:
        payload["service_name"] = service_name

    product = port_info.get("product")
    if product:
        payload["product"] = product

    version = port_info.get("version")
    if version:
        payload["version"] = version

    # Merge service_profile fields into payload
    for key in ("http_title", "server_header", "script_summaries"):
        value = service_profile.get(key)
        if value:
            payload[key] = value

    try:
        subject_key = build_service_socket_key(ip=ip, protocol=protocol, port=port)
    except ValueError:
        return None

    return {
        "observation_type": "network.service_profiled",
        "subject_type": "service.socket",
        "subject_key": subject_key,
        "payload": payload,
    }


def build_service_detected_payload(port_info: dict[str, Any]) -> dict[str, Any]:
    """Build canonical service-detected payload with normalized version detail."""
    payload: dict[str, Any] = {"source": "nmap"}

    service_name = str(port_info.get("service") or "").strip()
    if service_name:
        payload["service_name"] = service_name

    product = str(port_info.get("product") or "").strip()
    if product:
        payload["product"] = product

    version_text = str(port_info.get("version") or "").strip()
    normalized_version, version_raw, version_relation = normalize_service_version(version_text)
    if normalized_version:
        payload["version"] = normalized_version
    if version_raw:
        payload["version_raw"] = version_raw
    if version_relation:
        payload["version_relation"] = version_relation
    if product or version_text:
        payload["product_hint"] = " ".join(part for part in (product, version_text) if part).strip()

    return payload


def build_semantic_transport_markers() -> dict[str, str]:
    """Return durable semantic transport markers for nmap executions."""
    return {
        "semantic_schema_version": NMAP_SEMANTIC_SCHEMA_VERSION,
        "capability_family": NMAP_CAPABILITY_FAMILY,
    }


def _safe_int(value: Any) -> int:
    """Best-effort integer coercion for parsed metadata counters."""
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


def _split_targets(target: str) -> list[str]:
    """Split comma/space-separated nmap targets into non-empty tokens."""
    return [item.strip() for item in target.replace(",", " ").split() if item.strip()]


def _mask_target_identifier(target: str) -> str:
    """Mask hostname-like targets while preserving IP/network literals."""
    value = target.strip()
    if not value:
        return "<redacted-target>"

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass

    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        pass

    if ":" in value and not value.startswith("["):
        host_part, _, port_part = value.rpartition(":")
        if host_part and port_part.isdigit():
            try:
                return f"{ipaddress.ip_address(host_part)}:{port_part}"
            except ValueError:
                return f"<redacted-host>:{port_part}"

    return "<redacted-target>"


def _infer_variant_label(args: Any) -> str:
    """Infer an nmap scan variant label from args without importing nmap.py."""
    labels: list[str] = []

    scan_types = getattr(args, "scan_types", []) or []
    for scan_type in scan_types:
        candidate = getattr(scan_type, "value", scan_type)
        candidate_text = str(candidate).strip()
        if candidate_text:
            labels.append(candidate_text)

    if bool(getattr(args, "aggressive", False)):
        labels.append("-A")
    if bool(getattr(args, "service_detection", False)) and "-sV" not in labels:
        labels.append("-sV")
    if bool(getattr(args, "default_scripts", False)) and "-sC" not in labels:
        labels.append("-sC")
    scripts = getattr(args, "scripts", None)
    if isinstance(scripts, list) and scripts:
        normalized_scripts = [
            str(script).strip()
            for script in scripts
            if isinstance(script, str) and script.strip()
        ]
        if normalized_scripts:
            labels.append(f"--script={','.join(normalized_scripts[:3])}")

    if not labels:
        return "default_scan"
    return ", ".join(labels[:6])


def build_nmap_semantic_evidence(
    metadata: Mapping[str, Any],
    args: "NmapArgs",
) -> list[dict[str, Any]]:
    """Build vocabulary-conformant semantic evidence entries for nmap metadata."""
    evidence: list[dict[str, Any]] = []
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}

    evidence.append(
        {
            "type": SemanticEvidenceType.VARIANT.value,
            "name": "scan_variant",
            "value": _infer_variant_label(args),
        }
    )

    timing = getattr(args, "timing", None)
    timing_value = getattr(timing, "value", timing) if timing is not None else None
    if isinstance(timing_value, str) and timing_value.strip():
        evidence.append(
            {
                "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                "name": "timing_template",
                "value": timing_value.strip(),
            }
        )

    ports = getattr(args, "ports", None)
    if isinstance(ports, str) and ports.strip():
        evidence.append(
            {
                "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                "name": "port_range",
                "value": ports.strip(),
            }
        )
    else:
        evidence.append(
            {
                "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                "name": "top_ports",
                "value": 1000,
                "detail": {"unit": "ports"},
            }
        )

    script_categories = getattr(args, "script_categories", None)
    if isinstance(script_categories, list) and script_categories:
        normalized_categories = [str(item).strip() for item in script_categories if str(item).strip()]
        if normalized_categories:
            evidence.append(
                {
                    "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                    "name": "script_categories",
                    "value": ", ".join(normalized_categories[:8]),
                }
            )

    hosts_up = _safe_int(metadata_dict.get("hosts_up"))
    hosts_total = _safe_int(metadata_dict.get("hosts_total"))
    evidence.append(
        {
            "type": SemanticEvidenceType.RESULT_SUMMARY.value,
            "name": "hosts_up",
            "value": hosts_up,
            "detail": {
                "before_filter_count": hosts_total,
                "after_filter_count": hosts_up,
                "unit": "hosts",
            },
        }
    )

    open_ports = metadata_dict.get("open_ports")
    open_ports_count = len(open_ports) if isinstance(open_ports, list) else 0
    evidence.append(
        {
            "type": SemanticEvidenceType.RESULT_SUMMARY.value,
            "name": "open_ports_count",
            "value": open_ports_count,
            "detail": {"unit": "ports"},
        }
    )

    target_tokens = _split_targets(str(getattr(args, "target", "") or ""))
    evidence.append(
        {
            "type": SemanticEvidenceType.TARGET_TEMPLATE.value,
            "name": "target_count",
            "value": len(target_tokens),
        }
    )
    if target_tokens:
        evidence.append(
            {
                "type": SemanticEvidenceType.TARGET_TEMPLATE.value,
                "name": "target_sample",
                "value": ", ".join(_mask_target_identifier(token) for token in target_tokens[:2]),
            }
        )

    hosts_down = _safe_int(metadata_dict.get("hosts_down"))
    if hosts_down > 0:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "hosts_down",
                "value": hosts_down,
                "detail": {"severity": "info"},
            }
        )

    hosts = metadata_dict.get("hosts")
    host_entries = hosts if isinstance(hosts, list) else []
    os_guess_hosts = sum(
        1
        for host in host_entries
        if isinstance(host, Mapping) and isinstance(host.get("os_matches"), list) and host.get("os_matches")
    )
    if host_entries and os_guess_hosts == 0:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "os_matches_absent",
                "value": len(host_entries),
                "detail": {"severity": "info", "note": "no_os_guesses"},
            }
        )
    elif host_entries and os_guess_hosts < len(host_entries):
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "os_matches_partial",
                "value": os_guess_hosts,
                "detail": {"severity": "info", "note": "partial_os_guesses"},
            }
        )

    return evidence


# ---------------------------------------------------------------------------
# Curated findings allowlist
# ---------------------------------------------------------------------------
#
# Only script results that are clearly risk-bearing and deterministically
# classifiable produce findings. Descriptive metadata stays in host/service
# profiles — it does NOT become a finding.
#
# Each entry maps a script_id to a classifier function that inspects the
# script summary and returns a finding observation dict or None.

def _build_curated_finding(
    *,
    detector_id: str,
    title: str,
    severity: str,
    script_id: str,
    summary: str,
    affected_subject_key: str,
) -> dict[str, Any]:
    """Build one deterministic nmap finding observation on the finding domain."""
    finding_key = build_finding_vulnerability_key(
        subject_key=affected_subject_key,
        detector_id=detector_id,
    )
    return {
        "observation_type": "finding.vulnerability_detected",
        "subject_type": "finding.vulnerability",
        "subject_key": finding_key,
        "payload": {
            "detector_id": detector_id,
            "title": title,
            "severity": severity,
            "script_id": script_id,
            "summary": _truncate(summary),
            "subject_key": affected_subject_key,
            "source": "nmap",
            "source_tool": "nmap",
        },
    }


def _affected_service_key(ip: str, port: int, protocol: str) -> str | None:
    try:
        return build_service_socket_key(ip=ip, protocol=protocol, port=port)
    except ValueError:
        return None


def _classify_ftp_anon(summary: str, ip: str, port: int, protocol: str) -> Optional[dict[str, Any]]:
    """Classify anonymous FTP access finding."""
    lower = summary.lower()
    if "anonymous" not in lower:
        return None
    # Only flag as finding if login is allowed
    if "allowed" not in lower and "logged in" not in lower:
        return None
    affected_subject_key = _affected_service_key(ip, port, protocol)
    if affected_subject_key is None:
        return None
    return _build_curated_finding(
        detector_id="nmap/ftp-anon",
        title="Anonymous FTP login allowed",
        severity="medium",
        script_id="ftp-anon",
        summary=summary,
        affected_subject_key=affected_subject_key,
    )


def _classify_smb_signing(summary: str, ip: str, port: int, protocol: str) -> Optional[dict[str, Any]]:
    """Classify SMB signing disabled finding."""
    lower = summary.lower()
    if "signing" not in lower:
        return None
    if "disabled" not in lower and "not required" not in lower:
        return None
    affected_subject_key = _affected_service_key(ip, port, protocol)
    if affected_subject_key is None:
        return None
    return _build_curated_finding(
        detector_id="nmap/smb-signing-disabled",
        title="SMB message signing not required",
        severity="medium",
        script_id="smb-security-mode",
        summary=summary,
        affected_subject_key=affected_subject_key,
    )


def _classify_ssl_cert_expired(summary: str, ip: str, port: int, protocol: str) -> Optional[dict[str, Any]]:
    """Classify expired TLS/SSL certificate finding."""
    lower = summary.lower()
    if "expired" not in lower and "not valid" not in lower:
        return None
    affected_subject_key = _affected_service_key(ip, port, protocol)
    if affected_subject_key is None:
        return None
    return _build_curated_finding(
        detector_id="nmap/ssl-cert-expired",
        title="TLS/SSL certificate expired",
        severity="medium",
        script_id="ssl-cert",
        summary=summary,
        affected_subject_key=affected_subject_key,
    )


# Maps script_id -> classifier function
FINDING_ALLOWLIST: dict[str, Any] = {
    "ftp-anon": _classify_ftp_anon,
    "smb-security-mode": _classify_smb_signing,
    "ssl-cert": _classify_ssl_cert_expired,
}


def classify_script_findings(
    ip: str,
    port: int,
    protocol: str,
    script_summaries: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Run curated deterministic classifiers against port script summaries.

    Only scripts in FINDING_ALLOWLIST are evaluated. All other scripts
    remain descriptive metadata in service profiles, not findings.
    """
    findings: list[dict[str, Any]] = []
    for s in script_summaries:
        script_id = s.get("script_id", "")
        classifier = FINDING_ALLOWLIST.get(script_id)
        if classifier is None:
            continue
        result = classifier(s.get("summary", ""), ip, port, protocol)
        if result is not None:
            findings.append(result)
    return findings
