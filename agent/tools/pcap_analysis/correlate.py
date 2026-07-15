"""Correlate flattened packet fields into credential and auth signals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .classifiers import classify_direct_field, credential_event_from_field
from .contracts import AuthSequence, CredentialEvent, FieldRecord
from .flatten import flatten_tshark_packets
from .profiles import classify_profile_events, success_indicators


def extract_critical_signals(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract credential events and auth sequences from decoded packet rows."""

    fields = flatten_tshark_packets(rows)
    credential_events = _credential_events(fields)
    successes = success_indicators(fields)
    auth_sequences = _auth_sequences(credential_events, successes)
    return {
        "fields": fields,
        "credential_events": [event.to_dict() for event in credential_events],
        "auth_sequences": [sequence.to_dict() for sequence in auth_sequences],
        "auth_success": successes,
    }


def _credential_events(fields: list[FieldRecord]) -> list[CredentialEvent]:
    events: list[CredentialEvent] = []
    seen: set[tuple[str | None, str, str, str, str]] = set()

    for event in classify_profile_events(fields):
        _append_event(events, seen, event)

    for record in fields:
        classification = classify_direct_field(record)
        if classification is None:
            continue
        kind, role = classification
        _append_event(
            events,
            seen,
            credential_event_from_field(record, kind=kind, role=role),
        )

    return events


def _append_event(
    events: list[CredentialEvent],
    seen: set[tuple[str | None, str, str, str, str]],
    event: CredentialEvent,
) -> None:
    key = (event.frame, event.field, event.kind, event.role, event.value)
    if key in seen:
        return
    seen.add(key)
    events.append(event)


def _auth_sequences(
    credential_events: list[CredentialEvent],
    successes: list[Mapping[str, Any]],
) -> list[AuthSequence]:
    grouped: dict[tuple[str | None, str], dict[str, Any]] = {}

    def group_for(stream: str | None, flow_key: str) -> dict[str, Any]:
        key = (stream, "" if stream else flow_key)
        return grouped.setdefault(
            key,
            {
                "events": [],
                "successes": [],
            },
        )

    for event in credential_events:
        group_for(event.stream, event.flow_key)["events"].append(event)
    for success in successes:
        stream = str(success.get("stream") or "").strip() or None
        flow_key = str(success.get("flow_key") or "").strip() or "unknown"
        group_for(stream, flow_key)["successes"].append(success)

    sequences: list[AuthSequence] = []
    for (_stream, _flow_key), group in grouped.items():
        events = [event for event in group["events"] if isinstance(event, CredentialEvent)]
        success_rows = [row for row in group["successes"] if isinstance(row, Mapping)]
        username_events = [event for event in events if event.role == "username"]
        secret_events = [event for event in events if event.role in {"password", "secret"}]
        if not events and not success_rows:
            continue
        first_event = events[0] if events else None
        first_success = success_rows[0] if success_rows else {}
        frames = _unique_ordered(
            [
                *[event.frame for event in events],
                *[str(row.get("frame") or "").strip() for row in success_rows],
            ]
        )
        sequences.append(
            AuthSequence(
                stream=(first_event.stream if first_event else str(first_success.get("stream") or "").strip() or None),
                flow_key=(first_event.flow_key if first_event else str(first_success.get("flow_key") or "").strip() or "unknown"),
                protocol=(first_event.protocol if first_event else str(first_success.get("protocol") or "").strip() or None),
                src=(first_event.src if first_event else str(first_success.get("src") or "").strip() or None),
                dst=(first_event.dst if first_event else str(first_success.get("dst") or "").strip() or None),
                frames=tuple(frames),
                event_count=len(events),
                username_count=len(username_events),
                secret_count=len(secret_events),
                success_count=len(success_rows),
                username_proofs=tuple(_unique_ordered(event.value for event in username_events)),
                secret_proofs=tuple(_unique_ordered(event.value for event in secret_events)),
                success_messages=tuple(_unique_ordered(str(row.get("value") or "").strip() for row in success_rows)),
            )
        )

    sequences.sort(
        key=lambda item: (
            str(item.stream or ""),
            item.flow_key,
            ",".join(item.frames),
        )
    )
    return sequences


def _unique_ordered(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
