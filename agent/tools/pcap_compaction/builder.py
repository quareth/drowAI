"""Build deterministic compact envelopes from parsed PCAP analysis metadata.

This module intentionally consumes already-normalized metadata. It does not
execute packet-analysis tools, inspect raw PCAP bytes, or own tool-specific
command construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from .contracts import (
    PCAP_COMPACT_EVIDENCE_LIMIT,
    PCAP_COMPACT_FINDING_LIMIT,
    PCAP_COMPACT_LIST_KEYS,
    PCAP_COMPACT_PIVOT_LIMIT,
    PCAP_COMPACT_SCHEMA_VERSION,
    PCAP_COMPACT_SECTION_LIMIT,
    PCAP_COMPACT_TEXT_LIMIT,
    PCAP_PROTOCOL_EVIDENCE_KEYS,
)


def build_pcap_compaction(
    metadata: Mapping[str, Any] | None,
    *,
    source_tool: str,
) -> dict[str, Any]:
    """Return deterministic PCAP compact output and compressor-facing fields."""

    metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
    compact = _build_compact_envelope(metadata_map, source_tool=source_tool)
    key_findings = list(compact["key_findings"])
    decision_evidence = _build_decision_evidence(compact)
    summary = _build_summary(compact)
    return {
        "pcap_compact": compact,
        "compact_summary": summary,
        "compact_key_findings": key_findings,
        "compact_decision_evidence": decision_evidence,
    }


def render_pcap_compact_json(compact: Mapping[str, Any] | None) -> str:
    """Render a stable model-visible JSON representation of a compact envelope."""

    payload = dict(compact) if isinstance(compact, Mapping) else _build_compact_envelope(
        {},
        source_tool="unknown",
    )
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _build_compact_envelope(metadata: Mapping[str, Any], *, source_tool: str) -> dict[str, Any]:
    analysis_mode = _text(metadata.get("analysis_mode")) or "unknown"
    limits = _mapping(metadata.get("limits"))
    coverage = _build_coverage(metadata, limits)
    summary_counts = _build_summary_counts(metadata)
    flows = _section("conversations", metadata.get("conversations"), limits)
    protocol_evidence = {
        key: _section(key, metadata.get(key), limits)
        for key in PCAP_PROTOCOL_EVIDENCE_KEYS
    }
    security_signals = {
        "credential_events": _section("credential_events", metadata.get("credential_events"), limits),
        "auth_sequences": _section("auth_sequences", metadata.get("auth_sequences"), limits),
        "secret_exposure": _section("secret_exposure", metadata.get("secret_exposure"), limits),
        "diagnostics": {
            "warnings": _section("warnings", metadata.get("warnings"), limits),
            "errors": _section("errors", metadata.get("errors"), limits),
        },
    }
    compact: dict[str, Any] = {
        "schema_version": PCAP_COMPACT_SCHEMA_VERSION,
        "source_tool": _text(source_tool) or "unknown",
        "analysis_mode": analysis_mode,
        "output_format": _none_if_empty(metadata.get("output_format")),
        "pcap": _build_pcap_metadata(metadata),
        "coverage": coverage,
        "summary_counts": summary_counts,
        "key_findings": [],
        "flows": flows,
        "protocol_evidence": protocol_evidence,
        "security_signals": security_signals,
        "next_pivots": [],
        "omissions": _build_omissions(metadata, limits),
    }
    compact["key_findings"] = _build_key_findings(compact)
    compact["next_pivots"] = _build_next_pivots(compact)
    return compact


def _build_pcap_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    pcap = _mapping(metadata.get("pcap"))
    return {
        "input_file": _none_if_empty(pcap.get("input_file")),
        "artifact_sha256": _none_if_empty(pcap.get("artifact_sha256")),
        "packet_count": _int_or_zero(pcap.get("packet_count")),
        "duration_seconds": _none_if_empty(pcap.get("duration_seconds")),
    }


def _build_coverage(metadata: Mapping[str, Any], limits: Mapping[str, Any]) -> dict[str, Any]:
    lists = _mapping(limits.get("lists"))
    sections: dict[str, dict[str, Any]] = {}
    analyzed = _analyzed_sections(metadata)
    for key in PCAP_COMPACT_LIST_KEYS:
        raw = _mapping(lists.get(key))
        returned = _int_or_zero(raw.get("returned")) if raw else len(_list(metadata.get(key)))
        total = _int_or_none(raw.get("total")) if raw else returned
        sections[key] = {
            "status": "analyzed" if key in analyzed else "not_analyzed_by_mode",
            "returned": returned,
            "total": total,
            "truncated": bool(raw.get("truncated")) if raw else False,
            "limit": _int_or_none(raw.get("limit")) if raw else None,
        }
    return {
        "max_rows": _int_or_none(limits.get("max_rows")),
        "truncated": bool(limits.get("truncated")),
        "input_rows_truncated": bool(limits.get("input_rows_truncated")),
        "warnings_count": len(_list(metadata.get("warnings"))),
        "errors_count": len(_list(metadata.get("errors"))),
        "sections": sections,
    }


def _build_summary_counts(metadata: Mapping[str, Any]) -> dict[str, int]:
    return {
        "protocols": len(_list(metadata.get("protocols"))),
        "hosts": len(_list(metadata.get("hosts"))),
        "conversations": len(_list(metadata.get("conversations"))),
        "dns": len(_list(metadata.get("dns"))),
        "http": len(_list(metadata.get("http"))),
        "tls": len(_list(metadata.get("tls"))),
        "ftp": len(_list(metadata.get("ftp"))),
        "auth_indicators": len(_list(metadata.get("auth_indicators"))),
        "secret_exposure": len(_list(metadata.get("secret_exposure"))),
        "credential_events": len(_list(metadata.get("credential_events"))),
        "auth_sequences": len(_list(metadata.get("auth_sequences"))),
        "field_extract": len(_list(metadata.get("field_extract"))),
    }


def _section(key: str, value: Any, limits: Mapping[str, Any]) -> dict[str, Any]:
    rows = _list(value)
    limit_info = _mapping(_mapping(limits.get("lists")).get(key))
    total = _int_or_none(limit_info.get("total"))
    if total is None:
        total = len(rows)
    items = [_sanitize_row(row) for row in rows[:PCAP_COMPACT_SECTION_LIMIT]]
    returned = len(items)
    truncated = bool(limit_info.get("truncated")) or len(rows) > returned
    omitted = max(0, total - returned)
    return {
        "items": items,
        "returned": returned,
        "total": total,
        "omitted": omitted,
        "truncated": truncated,
        "source_limit": _int_or_none(limit_info.get("limit")),
    }


def _build_omissions(metadata: Mapping[str, Any], limits: Mapping[str, Any]) -> dict[str, Any]:
    omitted: dict[str, Any] = {}
    for key in PCAP_COMPACT_LIST_KEYS:
        section = _section(key, metadata.get(key), limits)
        if section["omitted"] or section["truncated"]:
            omitted[key] = {
                "omitted": section["omitted"],
                "returned": section["returned"],
                "total": section["total"],
                "truncated": section["truncated"],
                "reason": "section_limit_or_parser_limit",
            }
    not_analyzed = [
        key
        for key, value in _build_coverage(metadata, limits)["sections"].items()
        if value["status"] == "not_analyzed_by_mode"
    ]
    if not_analyzed:
        omitted["not_analyzed_by_mode"] = sorted(not_analyzed)
    return omitted


def _build_key_findings(compact: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    for event in _items(compact, "security_signals", "credential_events"):
        _append_unique(findings, _credential_event_finding(event))
    for sequence in _items(compact, "security_signals", "auth_sequences"):
        _append_unique(findings, _auth_sequence_finding(sequence))
    for exposure in _items(compact, "security_signals", "secret_exposure"):
        _append_unique(findings, _secret_finding(exposure))
    for indicator in _items(compact, "protocol_evidence", "auth_indicators"):
        _append_unique(findings, _auth_finding(indicator))
    for diagnostic in _items(compact, "security_signals", "diagnostics", "errors"):
        _append_unique(findings, f"PCAP parser error: {_truncate(diagnostic.get('value'))}")
    for diagnostic in _items(compact, "security_signals", "diagnostics", "warnings"):
        _append_unique(findings, f"PCAP parser warning: {_truncate(diagnostic.get('value'))}")
    for row in _items(compact, "protocol_evidence", "tls"):
        _append_unique(findings, _tls_finding(row))
    for row in _items(compact, "protocol_evidence", "dns"):
        _append_unique(findings, _dns_finding(row))
    for row in _items(compact, "protocol_evidence", "http"):
        _append_unique(findings, _http_finding(row))
    for row in _items(compact, "protocol_evidence", "ftp"):
        _append_unique(findings, _ftp_finding(row))
    for row in _items(compact, "flows"):
        _append_unique(findings, _flow_finding(row))
    for row in _items(compact, "protocol_evidence", "field_extract"):
        _append_unique(findings, _field_extract_finding(row))
    if not findings:
        packet_count = _mapping(compact.get("pcap")).get("packet_count")
        _append_unique(findings, f"PCAP analysis parsed {_int_or_zero(packet_count)} packets.")
    return findings[:PCAP_COMPACT_FINDING_LIMIT]


def _build_decision_evidence(compact: Mapping[str, Any]) -> list[str]:
    evidence: list[str] = []
    for finding in _list(compact.get("key_findings")):
        _append_unique(evidence, finding)
        if len(evidence) >= PCAP_COMPACT_EVIDENCE_LIMIT:
            return evidence
    return evidence


def _build_next_pivots(compact: Mapping[str, Any]) -> list[dict[str, str]]:
    pivots: list[dict[str, str]] = []
    for event in _items(compact, "security_signals", "credential_events"):
        _append_pivot(pivots, "credential_event", _text(event.get("extraction_filter")))
        _append_pivot(pivots, "credential_frame", _frame_filter(event))
        stream = _none_if_empty(event.get("stream"))
        if stream is not None:
            _append_pivot(pivots, "credential_tcp_stream", f"tcp.stream == {stream}")
    for sequence in _items(compact, "security_signals", "auth_sequences"):
        stream = _none_if_empty(sequence.get("stream"))
        if stream is not None:
            _append_pivot(pivots, "auth_sequence_tcp_stream", f"tcp.stream == {stream}")
    for exposure in _items(compact, "security_signals", "secret_exposure"):
        _append_pivot(pivots, "secret_exposure", _text(exposure.get("extraction_filter")))
        _append_pivot(pivots, "secret_frame", _frame_filter(exposure))
        stream = _none_if_empty(exposure.get("stream"))
        if stream is not None:
            _append_pivot(pivots, "secret_tcp_stream", f"tcp.stream == {stream}")
    for row in _items(compact, "protocol_evidence", "tls"):
        if _none_if_empty(row.get("stream")) is not None:
            _append_pivot(pivots, "tls_tcp_stream", f"tcp.stream == {row['stream']}")
    for row in _items(compact, "flows"):
        flow_filter = _flow_filter(row)
        if flow_filter:
            _append_pivot(pivots, "flow", flow_filter)
    return pivots[:PCAP_COMPACT_PIVOT_LIMIT]


def _build_summary(compact: Mapping[str, Any]) -> str:
    counts = _mapping(compact.get("summary_counts"))
    pcap = _mapping(compact.get("pcap"))
    parts = [
        f"PCAP compact analysis parsed {_int_or_zero(pcap.get('packet_count'))} packets",
        f"{_int_or_zero(counts.get('hosts'))} hosts",
        f"{_int_or_zero(counts.get('conversations'))} conversations",
    ]
    secret_count = _int_or_zero(counts.get("secret_exposure"))
    credential_count = _int_or_zero(counts.get("credential_events"))
    auth_sequence_count = _int_or_zero(counts.get("auth_sequences"))
    auth_count = _int_or_zero(counts.get("auth_indicators"))
    if credential_count:
        parts.append(f"{credential_count} credential events")
    if auth_sequence_count:
        parts.append(f"{auth_sequence_count} auth sequences")
    if secret_count:
        parts.append(f"{secret_count} secret exposures")
    elif auth_count:
        parts.append(f"{auth_count} auth indicators")
    return ", ".join(parts) + "."


def _analyzed_sections(metadata: Mapping[str, Any]) -> set[str]:
    analyzed = {"conversations", "credential_events", "auth_sequences", "warnings", "errors"}
    mode = _text(metadata.get("analysis_mode"))
    if mode == "field_extract":
        analyzed.add("field_extract")
    elif mode in PCAP_PROTOCOL_EVIDENCE_KEYS:
        analyzed.add(mode)
    elif mode == "secret_exposure":
        analyzed.add("secret_exposure")
    elif mode in {"pcap_summary", "conversations"}:
        pass
    for key in PCAP_COMPACT_LIST_KEYS:
        if _list(metadata.get(key)):
            analyzed.add(key)
    return analyzed


def _items(compact: Mapping[str, Any], *path: str) -> list[dict[str, Any]]:
    value: Any = compact
    for key in path:
        value = _mapping(value).get(key)
    if path and path[-1] in {"warnings", "errors"}:
        return [{"value": item} for item in _list(_mapping(value).get("items"))]
    return [item for item in _list(_mapping(value).get("items")) if isinstance(item, dict)]


def _secret_finding(row: Mapping[str, Any]) -> str:
    proof = row.get("proof_excerpt") or row.get("fingerprint")
    proof_text = f" proof={_truncate(proof, limit=120)}" if proof else ""
    return (
        f"Secret exposure: {_text(row.get('kind')) or 'secret'} in "
        f"{_text(row.get('field')) or 'unknown_field'}"
        f"{_frame_suffix(row)}{_flow_suffix(row)}.{proof_text}"
    ).strip()


def _credential_event_finding(row: Mapping[str, Any]) -> str:
    proof = row.get("proof_excerpt") or row.get("fingerprint")
    proof_text = f" proof={_truncate(proof, limit=120)}" if proof else ""
    command = _text(row.get("command"))
    command_text = f" command={command}" if command else ""
    return (
        f"Credential event: {_text(row.get('role')) or _text(row.get('kind')) or 'credential'} "
        f"in {_text(row.get('field')) or 'unknown_field'}"
        f"{_frame_suffix(row)}{_flow_suffix(row)}{command_text}.{proof_text}"
    ).strip()


def _auth_sequence_finding(row: Mapping[str, Any]) -> str:
    stream = _text(row.get("stream")) or "unknown_stream"
    username_count = _int_or_zero(row.get("username_count"))
    secret_count = _int_or_zero(row.get("secret_count"))
    success_count = _int_or_zero(row.get("success_count"))
    suffix_parts = [
        f"usernames={username_count}",
        f"secrets={secret_count}",
        f"successes={success_count}",
    ]
    if _list(row.get("frames")):
        suffix_parts.append(f"frames={','.join(_text(item) for item in _list(row.get('frames')) if _text(item))}")
    return f"Auth sequence: stream={stream} {' '.join(suffix_parts)}{_flow_suffix(row)}.".strip()


def _auth_finding(row: Mapping[str, Any]) -> str:
    value = row.get("value")
    value_text = f" value={_truncate(value, limit=120)}" if value else ""
    return (
        f"Auth indicator: {_text(row.get('mechanism')) or 'auth'} in "
        f"{_text(row.get('field')) or 'unknown_field'}{_frame_suffix(row)}.{value_text}"
    ).strip()


def _tls_finding(row: Mapping[str, Any]) -> str:
    sni = _text(row.get("sni")) or "unknown SNI"
    versions = ", ".join(_text(item) for item in _list(row.get("versions")) if _text(item))
    suffix = f" versions={versions}" if versions else ""
    return f"TLS evidence: {sni}{_frame_suffix(row)}{_flow_suffix(row)}.{suffix}".strip()


def _dns_finding(row: Mapping[str, Any]) -> str:
    query = _text(row.get("query")) or "unknown query"
    answers = ", ".join(_text(item) for item in _list(row.get("answers")) if _text(item))
    suffix = f" answers={answers}" if answers else ""
    return f"DNS evidence: {query}{_frame_suffix(row)}.{suffix}".strip()


def _http_finding(row: Mapping[str, Any]) -> str:
    method = _text(row.get("method")) or "HTTP"
    host = _text(row.get("host")) or "unknown_host"
    path = _text(row.get("path")) or ""
    status = _text(row.get("status"))
    suffix = f" status={status}" if status else ""
    return f"HTTP evidence: {method} {host}{path}{_frame_suffix(row)}.{suffix}".strip()


def _ftp_finding(row: Mapping[str, Any]) -> str:
    command = _text(row.get("request_command"))
    argument = _text(row.get("request_arg"))
    code = _text(row.get("response_code"))
    response = _text(row.get("response_arg"))
    if command:
        argument_text = f" arg={_truncate(argument, limit=120)}" if argument else ""
        return (
            f"FTP evidence: command={command}{argument_text}"
            f"{_frame_suffix(row)}{_flow_suffix(row)}."
        ).strip()
    if code:
        response_text = f" response={_truncate(response, limit=120)}" if response else ""
        return (
            f"FTP evidence: response_code={code}{response_text}"
            f"{_frame_suffix(row)}{_flow_suffix(row)}."
        ).strip()
    return f"FTP evidence{_frame_suffix(row)}{_flow_suffix(row)}.".strip()


def _flow_finding(row: Mapping[str, Any]) -> str:
    src = _text(row.get("src")) or "unknown_src"
    dst = _text(row.get("dst")) or "unknown_dst"
    protocol = _text(row.get("protocol")) or "unknown_protocol"
    packets = _int_or_zero(row.get("packet_count"))
    bytes_value = _int_or_zero(row.get("bytes"))
    return f"Flow evidence: {src} -> {dst} {protocol} packets={packets} bytes={bytes_value}."


def _field_extract_finding(row: Mapping[str, Any]) -> str:
    return f"Field extract evidence: {_truncate(json.dumps(row, sort_keys=True, ensure_ascii=True))}"


def _frame_suffix(row: Mapping[str, Any]) -> str:
    frame = _none_if_empty(row.get("frame"))
    return f" frame={frame}" if frame is not None else ""


def _flow_suffix(row: Mapping[str, Any]) -> str:
    src = _none_if_empty(row.get("src"))
    dst = _none_if_empty(row.get("dst"))
    if src is None and dst is None:
        return ""
    return f" flow={src or '?'}->{dst or '?'}"


def _frame_filter(row: Mapping[str, Any]) -> str | None:
    frame = _none_if_empty(row.get("frame"))
    return f"frame.number == {frame}" if frame is not None else None


def _flow_filter(row: Mapping[str, Any]) -> str | None:
    src = _none_if_empty(row.get("src"))
    dst = _none_if_empty(row.get("dst"))
    clauses = []
    if src is not None:
        clauses.append(f"ip.addr == {src}")
    if dst is not None:
        clauses.append(f"ip.addr == {dst}")
    return " && ".join(clauses) if clauses else None


def _append_pivot(pivots: list[dict[str, str]], reason: str, display_filter: str | None) -> None:
    normalized = _text(display_filter)
    if not normalized:
        return
    candidate = {"reason": reason, "display_filter": normalized}
    if candidate not in pivots:
        pivots.append(candidate)


def _append_unique(values: list[str], value: Any) -> None:
    text = _truncate(value)
    if text and text not in values:
        values.append(text)


def _sanitize_row(row: Any) -> Any:
    if isinstance(row, Mapping):
        return {
            str(key): _sanitize_row(value)
            for key, value in sorted(row.items(), key=lambda item: str(item[0]))
            if value not in (None, "", [])
        }
    if isinstance(row, list):
        return [_sanitize_row(item) for item in row]
    return row


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _none_if_empty(value: Any) -> Any:
    text = _text(value)
    if not text:
        return None
    return value


def _truncate(value: Any, *, limit: int = PCAP_COMPACT_TEXT_LIMIT) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
