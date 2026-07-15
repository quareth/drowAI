"""Legacy-compatible TShark JSON packet and fallback text parsing helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Dict

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _flow_key,
    _normalize_row_limit,
    _packet_context,
    _packet_layers,
)


def load_json_packets(
    stdout: str,
    *,
    max_rows: int | None = None,
) -> tuple[list[Mapping[str, Any]] | None, bool]:
    """Load TShark JSON rows with legacy-compatible truncation behavior."""

    stripped = stdout.strip()
    if not stripped:
        return None, False

    row_limit = _normalize_row_limit(max_rows)
    decoder = json.JSONDecoder()
    if stripped.startswith("["):
        rows: list[Mapping[str, Any]] = []
        index = 1
        truncated = False
        while True:
            index = _skip_json_whitespace(stripped, index)
            if index >= len(stripped):
                return None, False
            if stripped[index] == "]":
                return rows, truncated

            try:
                loaded, index = decoder.raw_decode(stripped, index)
            except json.JSONDecodeError:
                if rows:
                    return rows, True
                return None, False
            if isinstance(loaded, Mapping):
                if len(rows) >= row_limit:
                    truncated = True
                    return rows, truncated
                rows.append(loaded)

            index = _skip_json_whitespace(stripped, index)
            if index >= len(stripped):
                return (rows, True) if rows else (None, False)
            if stripped[index] == ",":
                index += 1
                if _skip_json_whitespace(stripped, index) >= len(stripped):
                    return (rows, True) if rows else (None, False)
                continue
            if stripped[index] == "]":
                return rows, truncated
            return (rows, True) if rows else (None, False)

    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(loaded, list):
        return None, False
    materialized = [row for row in loaded if isinstance(row, Mapping)]
    return materialized[:row_limit], len(materialized) > row_limit


def _skip_json_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def parse_json_packet_summary(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return legacy-compatible summary metadata for decoded packet rows."""

    protocols: set[str] = set()
    hosts: set[str] = set()
    conversations: dict[tuple[str, str, str | None, str | None, str | None], dict[str, Any]] = {}
    timestamps: list[float] = []

    for row in rows:
        layers = _packet_layers(row)
        if not isinstance(layers, Mapping):
            continue
        context = _packet_context(layers)
        for protocol in context["protocols"]:
            normalized = protocol.strip().lower()
            if normalized:
                protocols.add(normalized)
        for host_key in ("src", "dst"):
            if context[host_key]:
                hosts.add(context[host_key])
        if context["time_epoch"] is not None:
            timestamps.append(context["time_epoch"])

        if context["src"] and context["dst"]:
            key = (
                context["src"],
                context["dst"],
                context["protocol"],
                context["src_port"],
                context["dst_port"],
            )
            conversation = conversations.setdefault(
                key,
                {
                    "flow_key": _flow_key(context),
                    "src": context["src"],
                    "dst": context["dst"],
                    "protocol": context["protocol"],
                    "src_port": context["src_port"],
                    "dst_port": context["dst_port"],
                    "packet_count": 0,
                    "bytes": 0,
                },
            )
            conversation["packet_count"] += 1
            conversation["bytes"] += context["bytes"] or 0

    duration_seconds = None
    if len(timestamps) >= 2:
        duration_seconds = round(max(timestamps) - min(timestamps), 6)

    return {
        "packet_count": len(rows),
        "duration_seconds": duration_seconds,
        "protocols": sorted(protocols),
        "hosts": sorted(hosts),
        "conversations": sorted(
            conversations.values(),
            key=lambda item: (
                str(item.get("src") or ""),
                str(item.get("dst") or ""),
                str(item.get("protocol") or ""),
                str(item.get("src_port") or ""),
                str(item.get("dst_port") or ""),
            ),
        ),
    }


def parse_conversation_rows(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return legacy-compatible conversation rows for decoded packet rows."""

    return {
        "conversations": parse_json_packet_summary(rows)["conversations"],
        "warnings": [],
    }


def parse_text_packets(stdout: str) -> Dict[str, Any]:
    """Return legacy-compatible summary metadata for non-JSON TShark text output."""

    lines = stdout.splitlines()
    packet_lines = [line for line in lines if re.match(r"^\s*\d+", line)]
    protocols: set[str] = set()
    hosts = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", stdout))

    for line in packet_lines:
        protocol_matches = re.findall(r"\b([A-Z][A-Z0-9]{1,12})\b", line)
        for match in protocol_matches:
            protocols.add(match.lower())

    return {
        "packet_count": len(packet_lines),
        "protocols": sorted(protocols),
        "hosts": sorted(hosts),
        "conversations": [],
    }


__all__ = (
    "load_json_packets",
    "parse_conversation_rows",
    "parse_json_packet_summary",
    "parse_text_packets",
)
