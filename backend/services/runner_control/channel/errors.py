"""Channel error envelopes and channel-specific audit helpers.

Purpose: build runner websocket-channel error responses and emit accepted,
rejected, and protocol-violation audit events with the exact channel metadata
shape. Scope boundary: this module does not own generic audit redaction,
message routing, inbound ledger decisions, runtime ingest, or session lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

from backend.services.runner_control.audit import RunnerControlAuditService
from backend.services.runner_control.channel.types import RunnerChannelSession
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_SCHEMA_VERSION,
    RunnerEnvelope,
    RunnerErrorPayload,
    RunnerMessageType,
)


def _build_error_envelope(
    *,
    session: RunnerChannelSession,
    error_code: str,
    message: str,
    correlation_id: str | None,
) -> RunnerEnvelope:
    return RunnerEnvelope(
        message_id=str(uuid_lib.uuid4()),
        message_type=RunnerMessageType.ERROR,
        schema_version=RUNNER_PROTOCOL_SCHEMA_VERSION,
        tenant_id=str(session.tenant_id),
        runner_id=str(session.runner_id),
        correlation_id=correlation_id,
        runtime_job_id=None,
        task_id=None,
        created_at=_utcnow().isoformat(),
        payload=RunnerErrorPayload(
            error_code=error_code,
            message=message,
            retryable=False,
        ),
        raw_message_type=RunnerMessageType.ERROR.value,
    )


def _error_code_from_error_envelope(envelope: RunnerEnvelope) -> str:
    payload = envelope.payload
    if isinstance(payload, RunnerErrorPayload):
        normalized = str(payload.error_code or "").strip()
        if normalized:
            return normalized
    return "RUNNER_IDENTITY_MISMATCH"


def _audit_message_accepted(
    *,
    audit: RunnerControlAuditService,
    session: RunnerChannelSession,
    envelope: RunnerEnvelope,
    duplicate: bool,
) -> None:
    audit.emit(
        event_type="runner.message.accepted",
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        task_id=envelope.task_id,
        runtime_job_id=envelope.runtime_job_id,
        correlation_id=envelope.correlation_id,
        metadata={
            "message_id": envelope.message_id,
            "message_type": envelope.type,
            "duplicate": bool(duplicate),
        },
    )


def _audit_message_rejected(
    *,
    audit: RunnerControlAuditService,
    session: RunnerChannelSession,
    envelope: RunnerEnvelope,
    error_code: str,
) -> None:
    audit.emit(
        event_type="runner.message.rejected",
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        task_id=envelope.task_id,
        runtime_job_id=envelope.runtime_job_id,
        correlation_id=envelope.correlation_id,
        metadata={
            "message_id": envelope.message_id,
            "message_type": envelope.type,
            "error_code": error_code,
        },
    )


def _audit_unparsed_message_rejected(
    *,
    audit: RunnerControlAuditService,
    session: RunnerChannelSession,
    error_code: str,
) -> None:
    audit.emit(
        event_type="runner.message.rejected",
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        metadata={
            "message_id": None,
            "message_type": "<unparsed>",
            "error_code": error_code,
        },
    )


def _audit_protocol_violation(
    *,
    audit: RunnerControlAuditService,
    session: RunnerChannelSession,
    error_code: str,
    envelope: RunnerEnvelope | None = None,
    payload_size_bytes: int | None = None,
) -> None:
    metadata: dict[str, object] = {"error_code": error_code}
    if envelope is None:
        metadata["payload_size_bytes"] = payload_size_bytes
        audit.emit(
            event_type="runner.protocol_violation",
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            metadata=metadata,
        )
        return

    metadata["message_type"] = envelope.type
    audit.emit(
        event_type="runner.protocol_violation",
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        task_id=envelope.task_id,
        runtime_job_id=envelope.runtime_job_id,
        correlation_id=envelope.correlation_id,
        metadata=metadata,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
