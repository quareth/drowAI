"""Legacy-compatible SMTP/POP/IMAP signal helpers for TShark field rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _none_if_empty,
)

MAIL_PROTOCOLS = frozenset({"smtp", "pop", "imap"})
_MAIL_AUTH_COMMANDS = frozenset({"AUTH", "AUTHENTICATE", "LOGIN", "USER", "PASS"})


def survey_mail_auth_command_signals(fields: Mapping[str, Any]) -> list[str]:
    """Return legacy survey auth-command signals for mail protocols."""

    signals: list[str] = []
    for protocol, key in (
        ("smtp", "smtp.req.command"),
        ("pop", "pop.request.command"),
        ("imap", "imap.request.command"),
    ):
        command = str(fields.get(key) or "").strip().upper()
        if command in _MAIL_AUTH_COMMANDS:
            signals.append(f"{protocol}_auth_command")
    return signals


def mail_protocol_hint(value: Mapping[str, Any]) -> str | None:
    """Return the legacy mail protocol hint from protocols or destination port."""

    protocols = value.get("protocols") or []
    if isinstance(protocols, set):
        protocols = sorted(protocols)
    for protocol in reversed([str(item).lower() for item in protocols]):
        if protocol in MAIL_PROTOCOLS:
            return protocol
    port = _none_if_empty(value.get("dst_port"))
    return well_known_mail_protocol_for_port(port)


def well_known_mail_protocol_for_port(port: Any) -> str | None:
    """Return the legacy mail protocol hint for well-known cleartext/TLS ports."""

    return {
        "25": "smtp",
        "110": "pop",
        "143": "imap",
        "465": "smtp",
        "587": "smtp",
        "993": "imap",
        "995": "pop",
    }.get(str(port or "").strip())


def survey_mail_reason_and_intent(
    protocol_hint: str | None,
    signals: list[str],
) -> tuple[str, str]:
    """Return the legacy survey reason/intent for mail protocol hints."""

    if any(signal.endswith("_auth_command") for signal in signals):
        return (
            f"{(protocol_hint or 'cleartext protocol').upper()} authentication command observed.",
            "find_security_relevant_artifacts",
        )
    if protocol_hint in MAIL_PROTOCOLS:
        return (
            f"{protocol_hint.upper()} is a cleartext-capable application protocol.",
            "find_security_relevant_artifacts",
        )
    return ("Application protocol traffic observed.", "investigate_protocol")


__all__ = (
    "MAIL_PROTOCOLS",
    "mail_protocol_hint",
    "survey_mail_reason_and_intent",
    "survey_mail_auth_command_signals",
    "well_known_mail_protocol_for_port",
)
