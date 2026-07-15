"""Legacy-compatible TLS/SSL parsing helpers for TShark decoded and field rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _field_row_context,
    _field_values,
    _first_available_field,
    _mapping_value,
    _none_if_empty,
    _packet_context,
    _packet_layers,
)


def parse_tls_rows(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Parse decoded TShark JSON packet rows into legacy TLS records."""

    records: list[dict[str, Any]] = []
    for row in rows:
        layers = _packet_layers(row)
        tls = _mapping_value(layers, "tls") or _mapping_value(layers, "ssl")
        if not tls:
            continue
        context = _packet_context(layers)
        records.append(
            {
                "frame": context["frame"],
                "time": context["time"],
                "stream": context["stream"],
                "sni": _first_available_field(
                    tls,
                    "tls.handshake.extensions_server_name",
                    "ssl.handshake.extensions_server_name",
                ),
                "alpn": _field_values(tls, "tls.handshake.extensions_alpn_str"),
                "subject": _first_matching_field_value(layers, "subject"),
                "issuer": _first_matching_field_value(layers, "issuer"),
                "versions": sorted(
                    set(
                        [
                            *_field_values(tls, "tls.handshake.version"),
                            *_field_values(tls, "tls.record.version"),
                            *_field_values(tls, "ssl.handshake.version"),
                            *_field_values(tls, "ssl.record.version"),
                        ]
                    )
                ),
                "src": context["src"],
                "dst": context["dst"],
            }
        )
    return {
        "tls": records,
        "warnings": [] if records or not rows else ["No TLS records found in TShark JSON output."],
    }


def parse_tls_field_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Parse deterministic TShark field rows into legacy TLS records."""

    records: list[dict[str, Any]] = []
    for fields in rows:
        if not any(
            _none_if_empty(fields.get(key))
            for key in (
                "tls.handshake.extensions_server_name",
                "tls.handshake.type",
                "tls.handshake.version",
                "tls.alert_message.desc",
                "x509sat.printableString",
            )
        ):
            continue
        context = _field_row_context(fields)
        records.append(
            {
                "frame": context["frame"],
                "time": context["time"],
                "stream": context["stream"],
                "sni": _none_if_empty(fields.get("tls.handshake.extensions_server_name")),
                "subject": _none_if_empty(fields.get("x509sat.printableString")),
                "issuer": None,
                "versions": [
                    value
                    for value in [_none_if_empty(fields.get("tls.handshake.version"))]
                    if value
                ],
                "src": context["src"],
                "dst": context["dst"],
            }
        )
    return records


def _first_matching_field_value(value: Any, needle: str) -> str | None:
    lowered_needle = needle.lower()
    for field_name, field_value in _iter_leaf_values(value):
        if lowered_needle in field_name.lower():
            return str(field_value).strip() or None
    return None


def _iter_leaf_values(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_leaf_values(item, path)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_leaf_values(item, prefix)
    else:
        yield prefix, value


__all__ = (
    "parse_tls_field_rows",
    "parse_tls_rows",
)
