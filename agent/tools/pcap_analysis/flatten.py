"""Flatten TShark-decoded packet JSON into field records with context."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import FieldRecord, PacketContext


def flatten_tshark_packets(rows: Iterable[Mapping[str, Any]]) -> list[FieldRecord]:
    """Return scalar TShark fields in deterministic packet traversal order."""

    records: list[FieldRecord] = []
    for row in rows:
        layers = packet_layers(row)
        if not layers:
            continue
        context = packet_context(layers)
        for path, field_name, value in _iter_layer_scalars(layers):
            text = str(value).strip()
            if text:
                records.append(
                    FieldRecord(
                        path=path,
                        field_name=field_name,
                        value=text,
                        context=context,
                    )
                )
    return records


def packet_layers(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the decoded packet layer mapping from a TShark JSON row."""

    source = row.get("_source")
    if not isinstance(source, Mapping):
        return {}
    layers = source.get("layers")
    return layers if isinstance(layers, Mapping) else {}


def packet_context(layers: Mapping[str, Any]) -> PacketContext:
    """Build reusable packet context from common frame/IP/transport fields."""

    frame = _mapping_value(layers, "frame")
    ip = _mapping_value(layers, "ip") or _mapping_value(layers, "ipv6")
    transport_name, transport = _transport_layer(layers)
    protocol_text = _string_value(frame, "frame.protocols") or ""
    protocols = tuple(protocol for protocol in protocol_text.split(":") if protocol)
    app_protocol = _application_protocol(protocols)
    src = _string_value(ip, "ip.src") or _string_value(ip, "ipv6.src")
    dst = _string_value(ip, "ip.dst") or _string_value(ip, "ipv6.dst")
    src_port = _string_value(transport, f"{transport_name}.srcport") if transport_name else None
    dst_port = _string_value(transport, f"{transport_name}.dstport") if transport_name else None
    protocol = transport_name or _last_protocol(protocols)
    flow_key = (
        f"{protocol or 'ip'}:"
        f"{src or '?'}:{src_port or '?'}->"
        f"{dst or '?'}:{dst_port or '?'}"
    )
    return PacketContext(
        frame=_string_value(frame, "frame.number"),
        time=_string_value(frame, "frame.time") or _string_value(frame, "frame.time_relative"),
        time_epoch=_float_value(frame, "frame.time_epoch"),
        protocols=protocols,
        src=src,
        dst=dst,
        protocol=protocol,
        app_protocol=app_protocol or protocol,
        src_port=src_port,
        dst_port=dst_port,
        stream=_string_value(transport, f"{transport_name}.stream") if transport_name else None,
        byte_count=_int_value(frame, "frame.len"),
        flow_key=flow_key,
    )


def _iter_layer_scalars(value: Any, prefix: str = "") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if isinstance(item, Mapping):
                yield from _iter_layer_scalars(item, path)
            elif isinstance(item, list):
                for child in item:
                    yield from _iter_layer_scalars(child, path)
            else:
                yield path, key_text, item
    elif isinstance(value, list):
        for item in value:
            yield from _iter_layer_scalars(item, prefix)
    elif prefix:
        yield prefix, prefix.rsplit(".", 1)[-1], value


def _mapping_value(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _string_value(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None or isinstance(value, Mapping):
        return None
    normalized = str(value).strip()
    return normalized or None


def _transport_layer(layers: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    for name in ("tcp", "udp"):
        layer = _mapping_value(layers, name)
        if layer:
            return name, layer
    return None, {}


def _application_protocol(protocols: tuple[str, ...]) -> str | None:
    for protocol in reversed(protocols):
        normalized = protocol.strip().lower()
        if normalized and normalized not in {"eth", "sll", "ethertype", "ip", "ipv6", "tcp", "udp", "sctp", "data"}:
            return normalized
    return None


def _last_protocol(protocols: tuple[str, ...]) -> str | None:
    for protocol in reversed(protocols):
        normalized = protocol.strip().lower()
        if normalized:
            return normalized
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
