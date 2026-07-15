"""Shared DTOs for authenticated runner websocket-channel orchestration.

Purpose: define lightweight data-transfer objects shared by the channel facade,
router websocket loop, and focused channel collaborators. Scope boundary: this
module owns DTO shapes only and must not perform database, websocket, audit, or
runtime-job side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from runtime_shared.runner_protocol import RunnerEnvelope


@dataclass(slots=True)
class RunnerChannelSession:
    """Live runner websocket session state tracked by the backend channel manager."""

    tenant_id: int
    runner_id: UUID
    credential_id: UUID
    connection_id: str
    allowed_protocol_versions: tuple[str, ...]
    hello_received: bool = False


@dataclass(frozen=True, slots=True)
class RunnerChannelHandleResult:
    """Outcome for one inbound runner message."""

    response_envelopes: tuple[RunnerEnvelope, ...]
    ack_observation: RunnerAckObservation | None = None
    should_close: bool = False
    close_code: int = 1000
    close_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RunnerAckObservation:
    """Validated runner-ack observation surfaced for websocket ack waiters."""

    acked_message_id: str
    status: str
    error_code: str | None
