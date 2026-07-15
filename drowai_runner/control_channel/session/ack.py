"""Inbound ACK construction and classification for the cloud channel pump.

Owns the runner-side ACK concern for the connected-session receive loop:
sending the best-effort rejected ACK for malformed ``tool.command`` messages,
and classifying/caching/sending the default ACK for non-domain inbound
messages (including assignment-probe runtime-job tracking).

Boundary: these helpers operate on call-time collaborators (the websocket, the
channel identity, the per-connection ``ConnectionSessionState``, and a
task/runtime binding lookup). They perform no service composition, no domain
handler routing, and must not import ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Callable

from drowai_runner.protocol_handler import (
    RunnerTaskRuntimeBinding,
    RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
    build_runner_ack_envelope,
    classify_runner_control_inbound_ack,
    is_assignment_probe_message,
)
from runtime_shared.runner_protocol import RunnerEnvelope, RunnerMessageType

from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.session.state import ConnectionSessionState


def _send_ack(
    *,
    websocket,
    identity: CloudChannelIdentity,
    acked_message_id: str,
    status: str,
    error_code: str | None,
    correlation_id: str | None,
) -> None:
    ack = build_runner_ack_envelope(
        tenant_id=identity.tenant_id,
        runner_id=identity.runner_id,
        acked_message_id=acked_message_id,
        status=status,
        error_code=error_code,
        correlation_id=correlation_id,
        protocol_version=identity.protocol_version,
    )
    websocket.send(ack.to_json())


def send_tool_command_parse_error_ack(
    *,
    websocket,
    identity: CloudChannelIdentity,
    raw_message: str | bytes,
) -> None:
    """Best-effort rejected ack for malformed `tool.command` messages."""
    raw_text = raw_message.decode("utf-8", errors="replace") if isinstance(raw_message, bytes) else raw_message
    try:
        payload = json.loads(raw_text)
    except Exception:
        return
    if not isinstance(payload, Mapping):
        return
    if str(payload.get("type") or "").strip() != RunnerMessageType.TOOL_COMMAND.value:
        return
    acked_message_id = str(payload.get("message_id") or "").strip()
    if not acked_message_id:
        return
    correlation_id = str(payload.get("correlation_id") or "").strip() or None
    _send_ack(
        websocket=websocket,
        identity=identity,
        acked_message_id=acked_message_id,
        status="rejected",
        error_code=RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
        correlation_id=correlation_id,
    )


def handle_default_inbound_ack(
    *,
    websocket,
    identity: CloudChannelIdentity,
    inbound: RunnerEnvelope,
    normalized_message_id: str,
    session_state: ConnectionSessionState,
    task_runtime_binding_lookup: Callable[[str], RunnerTaskRuntimeBinding | None],
) -> None:
    """Classify/cache and send the default ACK for non-domain inbound messages."""
    cached_decision = session_state.ack_decisions_by_message_id.get(normalized_message_id)
    if cached_decision is None:
        decision = classify_runner_control_inbound_ack(
            inbound,
            expected_tenant_id=identity.tenant_id,
            expected_runner_id=identity.runner_id,
            assigned_runtime_jobs=session_state.assigned_runtime_jobs,
            task_runtime_binding_lookup=task_runtime_binding_lookup,
        )
        if not decision.should_ack:
            return
        status = str(decision.status or "accepted").strip() or "accepted"
        error_code = (
            str(decision.error_code).strip()
            if decision.error_code is not None and str(decision.error_code).strip()
            else None
        )
        cached_decision = (status, error_code)
        session_state.ack_decisions_by_message_id[normalized_message_id] = cached_decision
        if status == "accepted" and is_assignment_probe_message(inbound):
            runtime_job_id = (inbound.runtime_job_id or "").strip()
            if runtime_job_id:
                session_state.assigned_runtime_jobs[runtime_job_id] = inbound.task_id

    status, error_code = cached_decision
    if status:
        _send_ack(
            websocket=websocket,
            identity=identity,
            acked_message_id=inbound.message_id,
            status=status,
            error_code=error_code,
            correlation_id=inbound.correlation_id,
        )
