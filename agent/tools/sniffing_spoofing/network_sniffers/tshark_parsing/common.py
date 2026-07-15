"""Shared TShark parser constants and helpers for the refactored package."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Dict

TSHARK_SCHEMA_VERSION = "tshark.v1"
DEFAULT_MAX_ROWS = 100
SECRET_FINGERPRINT_KEY_ENV = "DROWAI_TSHARK_SECRET_FINGERPRINT_KEY"
DEFAULT_SENSITIVE_PROOF_MODE = "proof_excerpt"
TSHARK_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
TSHARK_FIELD_EXTRACT_ALLOWLIST = frozenset(
    {
        "frame.number",
        "frame.time",
        "frame.time_epoch",
        "frame.len",
        "frame.protocols",
        "eth.src",
        "eth.dst",
        "ip.src",
        "ip.dst",
        "ipv6.src",
        "ipv6.dst",
        "tcp.srcport",
        "tcp.dstport",
        "tcp.stream",
        "tcp.len",
        "udp.srcport",
        "udp.dstport",
        "dns.qry.name",
        "dns.qry.type",
        "dns.a",
        "dns.aaaa",
        "dns.cname",
        "dns.flags.rcode",
        "http.host",
        "http.request.method",
        "http.request.uri",
        "http.response.code",
        "http.user_agent",
        "http.content_type",
        "http.authorization",
        "http.cookie",
        "http.set_cookie",
        "ftp.request.command",
        "ftp.request.arg",
        "ftp.response.code",
        "ftp.response.arg",
        "tls.handshake.type",
        "tls.handshake.version",
        "tls.handshake.ciphersuite",
        "tls.handshake.extensions_server_name",
        "tls.alert_message.desc",
        "tcp.analysis.retransmission",
        "tcp.analysis.fast_retransmission",
        "tcp.analysis.lost_segment",
        "tcp.analysis.duplicate_ack",
        "icmp.type",
        "icmp.code",
        "smtp.req.command",
        "smtp.req.parameter",
        "pop.request.command",
        "pop.request.parameter",
        "imap.request.command",
        "imap.request",
        "data-text-lines",
        "x509sat.printableString",
    }
)


def normalize_tshark_field_extract_fields(fields: Iterable[Any] | None) -> list[str]:
    """Normalize and validate allowlisted field_extract field names."""

    normalized: list[str] = []
    for raw_field in fields or []:
        field_name = str(raw_field or "").strip()
        if field_name not in TSHARK_FIELD_EXTRACT_ALLOWLIST:
            if not TSHARK_FIELD_NAME_RE.fullmatch(field_name):
                raise ValueError(f"Invalid TShark field name: {field_name}")
            raise ValueError(f"TShark field is not allowlisted: {field_name}")
        normalized.append(field_name)
    return normalized


def _normalize_row_limit(max_rows: int | None) -> int:
    try:
        return max(1, int(max_rows or DEFAULT_MAX_ROWS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS


def _bounded_list(
    key: str,
    rows: Iterable[Any],
    max_rows: int,
    limits: Dict[str, Any],
) -> list[Any]:
    materialized = list(rows)
    bounded = materialized[:max_rows]
    truncated = len(materialized) > len(bounded)
    limits["lists"][key] = {
        "limit": max_rows,
        "returned": len(bounded),
        "total": len(materialized),
        "truncated": truncated,
    }
    if truncated:
        limits["truncated"] = True
    return bounded


def _normalize_field_names(fields: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw_field in fields or []:
        field_name = str(raw_field or "").strip()
        if field_name:
            normalized.append(field_name)
    return normalized


def _normalize_sensitive_proof_mode(value: Any) -> str:
    normalized = str(value or DEFAULT_SENSITIVE_PROOF_MODE).strip().lower()
    if normalized in {"metadata_only", "proof_excerpt", "fingerprint"}:
        return normalized
    return DEFAULT_SENSITIVE_PROOF_MODE


def _field_row_context(fields: Mapping[str, Any]) -> dict[str, Any]:
    protocols = [
        item.strip().lower()
        for item in str(fields.get("frame.protocols") or "").split(":")
        if item.strip()
    ]
    inferred_protocol = _infer_protocol_from_fields(fields)
    if inferred_protocol and inferred_protocol not in protocols:
        protocols.append(inferred_protocol)
    protocol = (
        _application_protocol_from_protocols(protocols)
        or inferred_protocol
        or _infer_transport_protocol_from_fields(fields)
    )
    return {
        "frame": _none_if_empty(fields.get("frame.number")),
        "time": _none_if_empty(fields.get("frame.time")) or _none_if_empty(fields.get("frame.time_epoch")),
        "time_epoch": _safe_float(fields.get("frame.time_epoch")),
        "protocols": protocols,
        "protocol": protocol,
        "src": _none_if_empty(fields.get("ip.src")) or _none_if_empty(fields.get("ipv6.src")),
        "dst": _none_if_empty(fields.get("ip.dst")) or _none_if_empty(fields.get("ipv6.dst")),
        "src_port": _none_if_empty(fields.get("tcp.srcport")) or _none_if_empty(fields.get("udp.srcport")),
        "dst_port": _none_if_empty(fields.get("tcp.dstport")) or _none_if_empty(fields.get("udp.dstport")),
        "stream": _none_if_empty(fields.get("tcp.stream")),
        "bytes": _safe_int(fields.get("tcp.len")) or _safe_int(fields.get("frame.len")),
    }


def _infer_protocol_from_fields(fields: Mapping[str, Any]) -> str | None:
    for protocol, prefixes in (
        ("ftp", ("ftp.",)),
        ("http", ("http.",)),
        ("dns", ("dns.",)),
        ("tls", ("tls.", "ssl.", "x509")),
        ("smtp", ("smtp.",)),
        ("pop", ("pop.", "pop3.")),
        ("imap", ("imap.",)),
    ):
        if any(str(key).lower().startswith(prefixes) and _none_if_empty(value) for key, value in fields.items()):
            return protocol
    return None


def _infer_transport_protocol_from_fields(fields: Mapping[str, Any]) -> str | None:
    if any(str(key).startswith("tcp.") and _none_if_empty(value) for key, value in fields.items()):
        return "tcp"
    if any(str(key).startswith("udp.") and _none_if_empty(value) for key, value in fields.items()):
        return "udp"
    return None


def _application_protocol_from_protocols(protocols: Iterable[Any]) -> str | None:
    for protocol in reversed([str(item).strip().lower() for item in protocols]):
        if protocol and protocol not in {
            "eth",
            "sll",
            "ethertype",
            "ip",
            "ipv6",
            "tcp",
            "udp",
            "sctp",
            "data",
            "data-text-lines",
        }:
            return protocol
    return None


def _field_rows_to_packet_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Adapt deterministic `-T fields` rows to decoded-row shape for shared signal extraction."""

    packet_rows: list[Mapping[str, Any]] = []
    for fields in rows:
        layers: dict[str, dict[str, Any]] = {}
        for field_name, raw_value in fields.items():
            value = _none_if_empty(raw_value)
            if value is None:
                continue
            layer_name = _field_layer_name(field_name)
            if not layer_name:
                continue
            layers.setdefault(layer_name, {})[str(field_name)] = value
        if not layers:
            continue
        frame = layers.setdefault("frame", {})
        if "frame.protocols" not in frame:
            inferred = _infer_protocol_from_fields(fields)
            if inferred:
                frame["frame.protocols"] = f"ip:tcp:{inferred}"
        packet_rows.append({"_source": {"layers": layers}})
    return packet_rows


def _field_layer_name(field_name: Any) -> str | None:
    text = str(field_name or "")
    if text.startswith("frame."):
        return "frame"
    if text.startswith("ip."):
        return "ip"
    if text.startswith("ipv6."):
        return "ipv6"
    if text.startswith("tcp."):
        return "tcp"
    if text.startswith("udp."):
        return "udp"
    for layer in ("ftp", "http", "dns", "tls", "ssl", "smtp", "pop", "imap", "x509sat"):
        if text.startswith(f"{layer}."):
            return layer
    return None


def _mapping_value(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _string_value(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _transport_layer(layers: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    for name in ("tcp", "udp"):
        layer = _mapping_value(layers, name)
        if layer:
            return name, layer
    return None, {}


def _packet_layers(row: Mapping[str, Any]) -> Mapping[str, Any]:
    source = row.get("_source")
    if not isinstance(source, Mapping):
        return {}
    layers = source.get("layers")
    return layers if isinstance(layers, Mapping) else {}


def _packet_context(layers: Mapping[str, Any]) -> dict[str, Any]:
    frame = _mapping_value(layers, "frame")
    ip = _mapping_value(layers, "ip") or _mapping_value(layers, "ipv6")
    transport_name, transport = _transport_layer(layers)
    protocol_text = _string_value(frame, "frame.protocols") or ""
    protocols = [protocol for protocol in protocol_text.split(":") if protocol]
    time_epoch = _float_value(frame, "frame.time_epoch")
    return {
        "frame": _string_value(frame, "frame.number"),
        "time": _string_value(frame, "frame.time") or _string_value(frame, "frame.time_relative"),
        "time_epoch": time_epoch,
        "protocols": protocols,
        "src": _string_value(ip, "ip.src") or _string_value(ip, "ipv6.src"),
        "dst": _string_value(ip, "ip.dst") or _string_value(ip, "ipv6.dst"),
        "protocol": transport_name or _last_protocol(protocols),
        "src_port": _string_value(transport, f"{transport_name}.srcport") if transport_name else None,
        "dst_port": _string_value(transport, f"{transport_name}.dstport") if transport_name else None,
        "stream": _string_value(transport, f"{transport_name}.stream") if transport_name else None,
        "bytes": _int_value(frame, "frame.len"),
    }


def _last_protocol(protocols: list[str]) -> str | None:
    for protocol in reversed(protocols):
        normalized = protocol.strip().lower()
        if normalized:
            return normalized
    return None


def _application_protocol(context: Mapping[str, Any]) -> str | None:
    """Return the highest application-layer protocol in a packet context."""
    for protocol in reversed(context.get("protocols") or []):
        normalized = str(protocol).strip().lower()
        if normalized and normalized not in {"eth", "ip", "ipv6", "tcp", "udp", "sctp", "data"}:
            return normalized
    return context.get("protocol")


def _flow_key(context: Mapping[str, Any]) -> str:
    return (
        f"{context.get('protocol') or 'ip'}:"
        f"{context.get('src') or '?'}:{context.get('src_port') or '?'}->"
        f"{context.get('dst') or '?'}:{context.get('dst_port') or '?'}"
    )


def _normalize_tshark_field_path(field_name: str) -> str:
    parts = [part for part in str(field_name or "").split(".") if part]
    if len(parts) >= 2 and parts[0] == parts[1]:
        parts = parts[1:]
    return ".".join(parts) if parts else str(field_name or "")


def _field_values(mapping: Mapping[str, Any], key: str) -> list[str]:
    value = mapping.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, Mapping):
        return [
            str(item).strip()
            for item in value.values()
            if not isinstance(item, Mapping) and str(item).strip()
        ]
    normalized = str(value).strip()
    return [normalized] if normalized else []


def _first_field_value(mapping: Mapping[str, Any], key: str) -> str | None:
    values = _field_values(mapping, key)
    return values[0] if values else None


def _first_available_field(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _first_field_value(mapping, key)
        if value:
            return value
    return None


def _float_value(mapping: Mapping[str, Any], key: str) -> float | None:
    raw_value = _string_value(mapping, key)
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except ValueError:
        return None


def _int_value(mapping: Mapping[str, Any], key: str) -> int | None:
    raw_value = _string_value(mapping, key)
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


def _none_if_empty(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


__all__ = (
    "DEFAULT_MAX_ROWS",
    "DEFAULT_SENSITIVE_PROOF_MODE",
    "SECRET_FINGERPRINT_KEY_ENV",
    "TSHARK_FIELD_EXTRACT_ALLOWLIST",
    "TSHARK_FIELD_NAME_RE",
    "TSHARK_SCHEMA_VERSION",
    "normalize_tshark_field_extract_fields",
    "_application_protocol",
    "_application_protocol_from_protocols",
    "_bounded_list",
    "_field_layer_name",
    "_field_row_context",
    "_field_rows_to_packet_rows",
    "_field_values",
    "_first_available_field",
    "_first_field_value",
    "_float_value",
    "_flow_key",
    "_infer_protocol_from_fields",
    "_infer_transport_protocol_from_fields",
    "_int_value",
    "_last_protocol",
    "_mapping_value",
    "_none_if_empty",
    "_normalize_field_names",
    "_normalize_row_limit",
    "_normalize_sensitive_proof_mode",
    "_normalize_tshark_field_path",
    "_packet_context",
    "_packet_layers",
    "_safe_float",
    "_safe_int",
    "_string_value",
    "_transport_layer",
)
