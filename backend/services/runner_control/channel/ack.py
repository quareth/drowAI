"""Runner websocket-channel ACK handling helpers.

Purpose: apply authenticated runner ACK messages to outbound delivery state and
runtime-job transition state. Scope boundary: this module owns runner ACK
idempotency keys, ACK observations, outbound ACK/failure marking, and ACK-driven
runtime-job transitions only; it must not route inbound messages, dispatch
outbound messages, ingest runtime events/artifacts, or manage heartbeat/lifecycle
state.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import RunnerControlMessage
from backend.services.runner_control.channel.types import RunnerAckObservation, RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.runtime_job_service import RuntimeJobService, RuntimeJobServiceError
from runtime_shared.runner_protocol import RunnerAckPayload, RunnerEnvelope

logger = logging.getLogger("backend.services.runner_control.channel_manager")


def _build_runner_ack_idempotency_key(*, envelope: RunnerEnvelope) -> str | None:
    payload = envelope.payload
    if not isinstance(payload, RunnerAckPayload):
        return None
    acked_message_id = str(payload.acked_message_id or "").strip()
    if not acked_message_id:
        return None
    return f"runner_ack:{str(envelope.tenant_id).strip()}:{str(envelope.runner_id).strip()}:{acked_message_id}"


def _build_runner_ack_observation(*, envelope: RunnerEnvelope) -> RunnerAckObservation | None:
    payload = envelope.payload
    if not isinstance(payload, RunnerAckPayload):
        return None
    acked_message_id = str(payload.acked_message_id or "").strip()
    if not acked_message_id:
        return None
    normalized_status = str(payload.status or "accepted").strip().lower() or "accepted"
    return RunnerAckObservation(
        acked_message_id=acked_message_id,
        status=normalized_status,
        error_code=str(payload.error_code).strip() if payload.error_code is not None else None,
    )


def _apply_runner_ack(
    *,
    db: Session,
    coordination_store: RunnerCoordinationStore,
    session: RunnerChannelSession,
    envelope: RunnerEnvelope,
) -> None:
    payload = envelope.payload
    if not isinstance(payload, RunnerAckPayload):
        return

    acked_message_id = str(payload.acked_message_id).strip()
    if not acked_message_id:
        return

    normalized_status = str(payload.status or "").strip().lower()
    outbound_message = db.execute(
        select(RunnerControlMessage).where(
            RunnerControlMessage.tenant_id == session.tenant_id,
            RunnerControlMessage.runner_id == session.runner_id,
            RunnerControlMessage.direction == "outbound",
            RunnerControlMessage.message_id == acked_message_id,
        )
    ).scalar_one_or_none()
    runtime_job_id = outbound_message.runtime_job_id if outbound_message is not None else None
    if normalized_status in {"failed", "error", "rejected"}:
        coordination_store.mark_outbound_message_failed(
            tenant_id=session.tenant_id,
            runner_id=session.runner_id,
            message_id=acked_message_id,
            error_code=payload.error_code,
            error_message="Runner reported message acknowledgment failure.",
        )
        _transition_runtime_job_from_ack(
            db=db,
            session=session,
            runtime_job_id=runtime_job_id,
            next_status="failed",
            ack_status=normalized_status or "failed",
            error_code=payload.error_code or "RUNNER_ACK_FAILED",
            error_message="Runner reported message acknowledgment failure.",
        )
        return

    coordination_store.mark_outbound_message_acked(
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        message_id=acked_message_id,
    )
    _transition_runtime_job_from_ack(
        db=db,
        session=session,
        runtime_job_id=runtime_job_id,
        next_status="acknowledged",
        ack_status=normalized_status or "accepted",
        )


def _transition_runtime_job_from_ack(
    *,
    db: Session,
    session: RunnerChannelSession,
    runtime_job_id: UUID | None,
    next_status: str,
    ack_status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if runtime_job_id is None:
        return
    try:
        RuntimeJobService(db).transition_runtime_job(
            tenant_id=session.tenant_id,
            runtime_job_id=runtime_job_id,
            next_status=next_status,
            result_json={"source": "runner_ack", "ack_status": ack_status},
            error_code=error_code,
            error_message=error_message,
        )
    except RuntimeJobServiceError as exc:
        if exc.error_code in {"RUNTIME_JOB_TRANSITION_STALE", "RUNTIME_JOB_TRANSITION_INVALID"}:
            return
        logger.warning(
            "runner_control.runtime_job_ack_transition_failed tenant_id=%s runner_id=%s runtime_job_id=%s next_status=%s error_code=%s",
            session.tenant_id,
            session.runner_id,
            runtime_job_id,
            next_status,
            exc.error_code,
        )
