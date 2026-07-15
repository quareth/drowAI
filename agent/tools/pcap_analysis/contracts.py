"""Internal contracts for generic decoded-PCAP critical signal extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PacketContext:
    """Packet-level context shared by flattened fields and extracted events."""

    frame: str | None
    time: str | None
    time_epoch: float | None
    protocols: tuple[str, ...]
    src: str | None
    dst: str | None
    protocol: str | None
    app_protocol: str | None
    src_port: str | None
    dst_port: str | None
    stream: str | None
    byte_count: int | None
    flow_key: str


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """One scalar field from a decoded packet plus packet context."""

    path: str
    field_name: str
    value: str
    context: PacketContext


@dataclass(frozen=True, slots=True)
class CredentialEvent:
    """A security-relevant credential or auth-adjacent packet fact."""

    frame: str | None
    time: str | None
    stream: str | None
    protocol: str | None
    src: str | None
    dst: str | None
    flow_key: str
    field: str
    kind: str
    role: str
    value: str
    command: str | None
    extraction_filter: str

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic mapping representation."""

        return {
            "frame": self.frame,
            "time": self.time,
            "stream": self.stream,
            "protocol": self.protocol,
            "src": self.src,
            "dst": self.dst,
            "flow_key": self.flow_key,
            "field": self.field,
            "kind": self.kind,
            "role": self.role,
            "value": self.value,
            "command": self.command,
            "extraction_filter": self.extraction_filter,
        }


@dataclass(frozen=True, slots=True)
class AuthSequence:
    """Credential events and success indicators correlated by stream/flow."""

    stream: str | None
    flow_key: str
    protocol: str | None
    src: str | None
    dst: str | None
    frames: tuple[str, ...]
    event_count: int
    username_count: int
    secret_count: int
    success_count: int
    username_proofs: tuple[str, ...]
    secret_proofs: tuple[str, ...]
    success_messages: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic mapping representation."""

        return {
            "stream": self.stream,
            "flow_key": self.flow_key,
            "protocol": self.protocol,
            "src": self.src,
            "dst": self.dst,
            "frames": list(self.frames),
            "event_count": self.event_count,
            "username_count": self.username_count,
            "secret_count": self.secret_count,
            "success_count": self.success_count,
            "username_proofs": list(self.username_proofs),
            "secret_proofs": list(self.secret_proofs),
            "success_messages": list(self.success_messages),
        }
