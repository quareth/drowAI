"""Remote-runtime inbound request handling and operation dispatch.

Owns runtime request classification, ACK classification/sending, duplicate
suppression, stream-mode delegation, operation dispatch, result-event emission,
terminal frame sends, and publisher sync. Mutates only the passed
``ConnectionSessionState`` ACK/processed fields. Must not import
``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Callable

from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.protocol_handler import build_runner_ack_envelope, build_remote_runtime_envelope
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerEnvelope,
    RunnerMessageType,
)

from drowai_runner.control_channel.constants import _REMOTE_RUNTIME_REQUEST_TYPES
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.runtime.operation_map import map_remote_runtime_operation
from drowai_runner.control_channel.runtime.result_event import RemoteRuntimeResultEventBuilder
from drowai_runner.control_channel.runtime.validation import RemoteRuntimeRequestValidator
from drowai_runner.control_channel.session.state import ConnectionSessionState
from drowai_runner.control_channel.terminal.frames import TerminalFrameLifecycle
from drowai_runner.control_channel.terminal.models import _ActiveTerminalSession
from drowai_runner.control_channel.terminal.stream import TerminalStreamHandler


class RemoteRuntimeHandler:
    """Handles inbound remote_runtime requests and dispatches operations."""

    def __init__(
        self,
        *,
        validator: RemoteRuntimeRequestValidator,
        result_event_builder: RemoteRuntimeResultEventBuilder,
        terminal_stream_handler: TerminalStreamHandler,
        frame_lifecycle: TerminalFrameLifecycle,
        operation_service_provider: Callable[[], RunnerOperationService],
        active_terminal_sessions: dict[str, _ActiveTerminalSession],
    ) -> None:
        self._validator = validator
        self._result_event_builder = result_event_builder
        self._terminal_stream_handler = terminal_stream_handler
        self._frame_lifecycle = frame_lifecycle
        self._operation_service_provider = operation_service_provider
        self._active_terminal_sessions = active_terminal_sessions

    def is_request(self, envelope: RunnerEnvelope) -> bool:
        return (
            envelope.schema_version == RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION
            and envelope.message_type in _REMOTE_RUNTIME_REQUEST_TYPES
        )

    def handle(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        inbound: RunnerEnvelope,
        session_state: ConnectionSessionState,
    ) -> None:
        normalized_message_id = str(inbound.message_id).strip()
        cached_decision = session_state.ack_decisions_by_message_id.get(normalized_message_id)
        validated_context: _RemoteRuntimeRequestContext | None = None
        if cached_decision is None:
            status, error_code, validated_context = self._validator.validate(
                identity=identity,
                inbound=inbound,
            )
            status = str(status or "accepted").strip() or "accepted"
            error_code = (
                str(error_code).strip()
                if error_code is not None and str(error_code).strip()
                else None
            )
            cached_decision = (status, error_code)
            session_state.ack_decisions_by_message_id[normalized_message_id] = cached_decision

        status, error_code = cached_decision
        ack = build_runner_ack_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            acked_message_id=inbound.message_id,
            status=status,
            error_code=error_code,
            correlation_id=inbound.correlation_id,
            protocol_version=identity.protocol_version,
        )
        websocket.send(ack.to_json())

        if (
            status != "accepted"
            or normalized_message_id in session_state.processed_runtime_messages
        ):
            return
        session_state.processed_runtime_messages.add(normalized_message_id)

        if validated_context is None:
            validation = self._validator.validate(identity=identity, inbound=inbound)
            if validation[0] != "accepted" or validation[2] is None:
                return
            validated_context = validation[2]

        if self._terminal_stream_handler.is_request(inbound):
            self._terminal_stream_handler.execute(
                websocket=websocket,
                identity=identity,
                inbound=inbound,
                context=validated_context,
            )
            return

        event_type, payload, terminal_frames = self._execute_runtime_operation(
            inbound=inbound,
            context=validated_context,
        )
        event = build_remote_runtime_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            message_type=event_type,
            payload=payload,
            correlation_id=inbound.correlation_id,
            runtime_job_id=inbound.runtime_job_id,
            task_id=inbound.task_id,
        )
        websocket.send(event.to_json())
        self._frame_lifecycle.sync_publisher_for_event(
            websocket=websocket,
            identity=identity,
            event_type=event_type,
            event_payload=payload,
        )
        for frame_payload in terminal_frames:
            frame_session_id = str(frame_payload.get("session_id") or "").strip()
            frame_runtime_job_id = ""
            if frame_session_id:
                active_session = self._active_terminal_sessions.get(frame_session_id)
                if active_session is not None:
                    frame_runtime_job_id = str(active_session.runtime_job_id or "").strip()
            if not frame_runtime_job_id:
                frame_runtime_job_id = str(validated_context.runtime_job_id or "").strip()
            if not frame_runtime_job_id:
                frame_runtime_job_id = str(inbound.runtime_job_id or "").strip()
            frame_event = build_remote_runtime_envelope(
                tenant_id=identity.tenant_id,
                runner_id=identity.runner_id,
                message_type=RunnerMessageType.TERMINAL_FRAME,
                payload=frame_payload,
                correlation_id=inbound.correlation_id,
                runtime_job_id=frame_runtime_job_id,
                task_id=inbound.task_id,
            )
            websocket.send(frame_event.to_json())

    def _execute_runtime_operation(
        self,
        *,
        inbound: RunnerEnvelope,
        context: _RemoteRuntimeRequestContext,
    ) -> tuple[RunnerMessageType, dict[str, object], list[dict[str, object]]]:
        operation_name, operation_params = map_remote_runtime_operation(
            inbound=inbound,
            context=context,
        )
        response = self._operation_service_provider().dispatch_operation(
            operation=operation_name,
            params=operation_params,
        )
        event_type, event_payload = self._result_event_builder.build_result_event(
            inbound=inbound,
            response=response,
            context=context,
        )
        terminal_frames = self._frame_lifecycle.collect_frames(
            inbound=inbound,
            event_type=event_type,
            event_payload=event_payload,
        )
        return event_type, event_payload, terminal_frames
