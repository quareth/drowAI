"""Legacy-compatible HTTP parsing helpers for TShark decoded and field rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict
from urllib.parse import urlsplit

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _field_row_context,
    _field_values,
    _first_field_value,
    _mapping_value,
    _none_if_empty,
    _packet_context,
    _packet_layers,
)


def parse_http_rows(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Parse decoded TShark JSON packet rows into legacy HTTP records."""

    records: list[dict[str, Any]] = []
    for row in rows:
        layers = _packet_layers(row)
        http = _mapping_value(layers, "http")
        if not http:
            continue
        context = _packet_context(layers)
        uri = _first_field_value(http, "http.request.uri")
        record = {
            "frame": context["frame"],
            "time": context["time"],
            "stream": context["stream"],
            "host": _first_field_value(http, "http.host"),
            "method": _first_field_value(http, "http.request.method"),
            "path": _http_path(uri),
            "status": _first_field_value(http, "http.response.code"),
            "user_agent": _first_field_value(http, "http.user_agent"),
            "server": _first_field_value(http, "http.server"),
            "src": context["src"],
            "dst": context["dst"],
        }
        headers = _http_secret_headers(http)
        if headers:
            record["headers"] = headers
        records.append(record)
    return {
        "http": records,
        "warnings": [] if records or not rows else ["No HTTP records found in TShark JSON output."],
    }


def parse_http_field_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Parse deterministic TShark field rows into legacy HTTP records."""

    records: list[dict[str, Any]] = []
    for fields in rows:
        if not any(_none_if_empty(fields.get(key)) for key in ("http.host", "http.request.uri", "http.response.code")):
            continue
        context = _field_row_context(fields)
        record = {
            "frame": context["frame"],
            "time": context["time"],
            "stream": context["stream"],
            "host": _none_if_empty(fields.get("http.host")),
            "method": _none_if_empty(fields.get("http.request.method")),
            "path": _http_path(_none_if_empty(fields.get("http.request.uri"))),
            "status": _none_if_empty(fields.get("http.response.code")),
            "user_agent": _none_if_empty(fields.get("http.user_agent")),
            "content_type": _none_if_empty(fields.get("http.content_type")),
            "src": context["src"],
            "dst": context["dst"],
        }
        headers = {
            key: fields[key]
            for key in ("http.authorization", "http.cookie", "http.set_cookie")
            if _none_if_empty(fields.get(key))
        }
        if headers:
            record["headers"] = headers
        records.append(record)
    return records


def _http_path(uri: str | None) -> str | None:
    if not uri:
        return None
    parsed = urlsplit(uri)
    if parsed.path:
        suffix = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.path}{suffix}"
    return uri


def _http_secret_headers(http: Mapping[str, Any]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for field_name, kind in (
        ("http.authorization", "authorization_header"),
        ("http.proxy_authorization", "authorization_header"),
        ("http.cookie", "cookie"),
        ("http.set_cookie", "cookie"),
    ):
        _ = kind
        values = [value for value in _field_values(http, field_name)]
        if values:
            headers[field_name] = values
    return headers


__all__ = (
    "parse_http_field_rows",
    "parse_http_rows",
)
