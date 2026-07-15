"""Legacy-compatible FTP parsing helpers for TShark field rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _field_row_context,
    _none_if_empty,
)


def parse_ftp_field_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Parse deterministic TShark field rows into legacy FTP records."""

    records: list[dict[str, Any]] = []
    for fields in rows:
        if not any(
            _none_if_empty(fields.get(key))
            for key in (
                "ftp.request.command",
                "ftp.request.arg",
                "ftp.response.code",
                "ftp.response.arg",
            )
        ):
            continue
        context = _field_row_context(fields)
        records.append(
            {
                "frame": context["frame"],
                "time": context["time"],
                "stream": context["stream"],
                "src": context["src"],
                "dst": context["dst"],
                "src_port": context["src_port"],
                "dst_port": context["dst_port"],
                "request_command": _none_if_empty(fields.get("ftp.request.command")),
                "request_arg": _none_if_empty(fields.get("ftp.request.arg")),
                "response_code": _none_if_empty(fields.get("ftp.response.code")),
                "response_arg": _none_if_empty(fields.get("ftp.response.arg")),
                "tcp_len": _none_if_empty(fields.get("tcp.len")),
                "frame_len": _none_if_empty(fields.get("frame.len")),
            }
        )
    return records


def survey_ftp_command_signals(fields: Mapping[str, Any]) -> list[str]:
    """Return legacy survey signals for FTP control/auth commands."""

    signals: list[str] = []
    protocols = str(fields.get("frame.protocols") or "").lower()
    if "ftp" in protocols:
        command = str(fields.get("ftp.request.command") or "").strip().upper()
        if command in {"USER", "PASS"}:
            signals.append("cleartext_ftp_auth_command")
        elif command:
            signals.append("ftp_control_command")
    return signals


__all__ = (
    "parse_ftp_field_rows",
    "survey_ftp_command_signals",
)
