"""Declarative protocol profiles for command/argument credential patterns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .classifiers import credential_event_from_field, normalize_field_path
from .contracts import CredentialEvent, FieldRecord


@dataclass(frozen=True, slots=True)
class ProtocolProfile:
    """Declarative hints for cleartext auth command protocols."""

    name: str
    command_fields: tuple[str, ...]
    argument_fields: tuple[str, ...]
    username_commands: tuple[str, ...]
    password_commands: tuple[str, ...]
    secret_commands: tuple[str, ...]
    success_fields: tuple[str, ...]
    success_values: tuple[str, ...]


PROTOCOL_PROFILES: tuple[ProtocolProfile, ...] = (
    ProtocolProfile(
        name="ftp",
        command_fields=("ftp.request.command",),
        argument_fields=("ftp.request.arg", "ftp.request.command_parameter"),
        username_commands=("USER",),
        password_commands=("PASS",),
        secret_commands=("ACCT", "AUTH"),
        success_fields=("ftp.response.code", "ftp.response.arg"),
        success_values=("230", "login successful"),
    ),
    ProtocolProfile(
        name="pop",
        command_fields=("pop.request.command", "pop.request.command_name", "pop3.request.command"),
        argument_fields=("pop.request.parameter", "pop.request.arg", "pop3.request.parameter", "pop3.request.arg"),
        username_commands=("USER",),
        password_commands=("PASS",),
        secret_commands=("AUTH", "APOP"),
        success_fields=("pop.response", "pop3.response"),
        success_values=("+OK", "logged in"),
    ),
    ProtocolProfile(
        name="imap",
        command_fields=("imap.request.command", "imap.command"),
        argument_fields=("imap.request", "imap.request.line", "imap.argument", "imap.request.argument"),
        username_commands=("LOGIN",),
        password_commands=(),
        secret_commands=("AUTHENTICATE",),
        success_fields=("imap.response", "imap.response.status"),
        success_values=("OK", "LOGIN completed", "AUTHENTICATE completed"),
    ),
    ProtocolProfile(
        name="smtp",
        command_fields=("smtp.req.command", "smtp.request.command"),
        argument_fields=("smtp.req.parameter", "smtp.request.parameter", "smtp.auth.username", "smtp.auth.password"),
        username_commands=("USER", "AUTH"),
        password_commands=("PASS",),
        secret_commands=("AUTH", "AUTHENTICATE"),
        success_fields=("smtp.response.code", "smtp.response"),
        success_values=("235", "authentication successful"),
    ),
)

_USERNAME_COMMANDS = {"USER", "USERNAME", "LOGIN"}
_PASSWORD_COMMANDS = {"PASS", "PASSWORD", "PWD"}
_SECRET_COMMANDS = {"AUTH", "AUTHENTICATE", "APOP"}


def classify_profile_events(fields: Iterable[FieldRecord]) -> list[CredentialEvent]:
    """Classify command/argument credential pairs using profiles and generic fallbacks."""

    records = list(fields)
    by_packet = _group_packet_fields(records)
    events: list[CredentialEvent] = []
    seen: set[tuple[str | None, str, str, str, str]] = set()
    for packet_fields in by_packet.values():
        for event in _profile_events_for_packet(packet_fields):
            key = (event.frame, event.field, event.kind, event.role, event.value)
            if key not in seen:
                seen.add(key)
                events.append(event)
        for event in _generic_command_events_for_packet(packet_fields):
            key = (event.frame, event.field, event.kind, event.role, event.value)
            if key not in seen:
                seen.add(key)
                events.append(event)
    return events


def success_indicators(fields: Iterable[FieldRecord]) -> list[dict[str, str | None]]:
    """Return auth success indicators found through protocol profiles."""

    indicators: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for record in fields:
        field = normalize_field_path(record.field_name).lower()
        value = record.value.strip()
        value_lower = value.lower()
        for profile in PROTOCOL_PROFILES:
            if field not in {item.lower() for item in profile.success_fields}:
                continue
            if not any(expected.lower() in value_lower for expected in profile.success_values):
                continue
            context = record.context
            key = (context.frame, context.stream)
            if key in seen:
                continue
            seen.add(key)
            indicators.append(
                {
                    "frame": context.frame,
                    "time": context.time,
                    "stream": context.stream,
                    "protocol": context.app_protocol or context.protocol,
                    "src": context.src,
                    "dst": context.dst,
                    "flow_key": context.flow_key,
                    "field": normalize_field_path(record.field_name),
                    "value": value,
                }
            )
    return indicators


def _group_packet_fields(records: list[FieldRecord]) -> dict[tuple[str | None, str], list[FieldRecord]]:
    grouped: dict[tuple[str | None, str], list[FieldRecord]] = {}
    for record in records:
        grouped.setdefault((record.context.frame, record.context.flow_key), []).append(record)
    return grouped


def _profile_events_for_packet(records: list[FieldRecord]) -> list[CredentialEvent]:
    events: list[CredentialEvent] = []
    field_map = _field_map(records)
    for profile in PROTOCOL_PROFILES:
        command_records = [
            record
            for name in profile.command_fields
            for record in field_map.get(name.lower(), [])
        ]
        if not command_records:
            continue
        argument_records = [
            record
            for name in profile.argument_fields
            for record in field_map.get(name.lower(), [])
        ]
        for command_record in command_records:
            command = command_record.value.strip().upper()
            for argument_record in argument_records:
                classified = _classify_command_argument(command, profile)
                if classified is None:
                    continue
                kind, role = classified
                events.append(
                    credential_event_from_field(
                        argument_record,
                        kind=kind,
                        role=role,
                        command=command,
                        extraction_filter=f"{normalize_field_path(command_record.field_name)} == {command}",
                    )
                )
    return events


def _generic_command_events_for_packet(records: list[FieldRecord]) -> list[CredentialEvent]:
    events: list[CredentialEvent] = []
    command_records = [
        record
        for record in records
        if _looks_like_command_field(record.field_name)
    ]
    if not command_records:
        return []
    argument_records = [
        record
        for record in records
        if _looks_like_argument_field(record.field_name)
    ]
    for command_record in command_records:
        command = command_record.value.strip().upper()
        classified = _classify_generic_command(command)
        if classified is None:
            continue
        kind, role = classified
        for argument_record in argument_records:
            events.append(
                credential_event_from_field(
                    argument_record,
                    kind=kind,
                    role=role,
                    command=command,
                    extraction_filter=f"{normalize_field_path(command_record.field_name)} == {command}",
                )
            )
    return events


def _field_map(records: list[FieldRecord]) -> dict[str, list[FieldRecord]]:
    result: dict[str, list[FieldRecord]] = {}
    for record in records:
        result.setdefault(normalize_field_path(record.field_name).lower(), []).append(record)
    return result


def _classify_command_argument(
    command: str,
    profile: ProtocolProfile,
) -> tuple[str, str] | None:
    if command in profile.username_commands:
        return "username", "username"
    if command in profile.password_commands:
        return "password", "password"
    if command in profile.secret_commands:
        return "protocol_auth_argument", "secret"
    return None


def _classify_generic_command(command: str) -> tuple[str, str] | None:
    if command in _USERNAME_COMMANDS:
        return "username", "username"
    if command in _PASSWORD_COMMANDS:
        return "password", "password"
    if command in _SECRET_COMMANDS:
        return "protocol_auth_argument", "secret"
    return None


def _looks_like_command_field(field_name: str) -> bool:
    field = normalize_field_path(field_name).lower()
    return field.endswith(".request.command") or field.endswith(".command") or field.endswith(".command_name")


def _looks_like_argument_field(field_name: str) -> bool:
    field = normalize_field_path(field_name).lower()
    return field.endswith((".request.arg", ".request.argument", ".request.parameter", ".command_parameter", ".argument", ".parameter"))
