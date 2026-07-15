"""Legacy-compatible DNS parsing helpers for TShark decoded and field rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _field_row_context,
    _field_values,
    _first_field_value,
    _mapping_value,
    _none_if_empty,
    _packet_context,
    _packet_layers,
)


def parse_dns_rows(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Parse decoded TShark JSON packet rows into legacy DNS records."""

    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        layers = _packet_layers(row)
        dns = _mapping_value(layers, "dns")
        if not dns:
            continue
        context = _packet_context(layers)
        queries = _field_values(dns, "dns.qry.name")
        query_types = _field_values(dns, "dns.qry.type")
        answers = [
            *_field_values(dns, "dns.a"),
            *_field_values(dns, "dns.aaaa"),
            *_field_values(dns, "dns.cname"),
            *_field_values(dns, "dns.resp.name"),
        ]
        rcode = _first_field_value(dns, "dns.flags.rcode")
        if not queries and answers:
            queries = _field_values(dns, "dns.resp.name")
        for index, query in enumerate(queries or [None]):
            record = {
                "frame": context["frame"],
                "time": context["time"],
                "query": query,
                "qtype": query_types[index] if index < len(query_types) else (query_types[0] if query_types else None),
                "answers": sorted(set(answers)),
                "rcode": rcode,
                "src": context["src"],
                "dst": context["dst"],
            }
            dedupe_key = (
                record["frame"],
                record["query"],
                record["qtype"],
                tuple(record["answers"]),
                record["rcode"],
            )
            if dedupe_key not in seen:
                records.append(record)
                seen.add(dedupe_key)
    return {
        "dns": records,
        "warnings": [] if records or not rows else ["No DNS records found in TShark JSON output."],
    }


def parse_dns_field_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Parse deterministic TShark field rows into legacy DNS records."""

    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for fields in rows:
        query = _none_if_empty(fields.get("dns.qry.name"))
        answers = [
            value
            for value in (
                _none_if_empty(fields.get("dns.a")),
                _none_if_empty(fields.get("dns.aaaa")),
                _none_if_empty(fields.get("dns.cname")),
            )
            if value
        ]
        if not query and not answers:
            continue
        context = _field_row_context(fields)
        record = {
            "frame": context["frame"],
            "time": context["time"],
            "query": query,
            "qtype": _none_if_empty(fields.get("dns.qry.type")),
            "answers": sorted(set(answers)),
            "rcode": _none_if_empty(fields.get("dns.flags.rcode")),
            "src": context["src"],
            "dst": context["dst"],
        }
        key = (record["frame"], record["query"], record["qtype"], tuple(record["answers"]), record["rcode"])
        if key not in seen:
            seen.add(key)
            records.append(record)
    return records


__all__ = (
    "parse_dns_field_rows",
    "parse_dns_rows",
)
