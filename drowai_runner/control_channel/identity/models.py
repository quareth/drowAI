"""Immutable identity/registration DTOs. Data only.

Holds the frozen dataclasses describing resolved channel identity and the
registration request/response payloads. No logic, no I/O, no protocol behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CloudChannelIdentity:
    """Resolved identity required to authenticate channel websocket sessions."""

    tenant_id: int
    runner_id: str
    credential_secret: str
    channel_endpoint: str
    protocol_version: str
    heartbeat_interval_seconds: int


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    """Registration payload sent to the cloud control-plane endpoint."""

    install_token: str
    runner_name: str
    runner_version: str
    labels: dict[str, str]
    capabilities: list[str]
    tenant_id: int | None = None


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """Registration response payload persisted by the runner process."""

    runner_id: str
    tenant_id: int
    credential_secret: str
    channel_endpoint: str
    protocol_version: str
    heartbeat_interval_seconds: int
