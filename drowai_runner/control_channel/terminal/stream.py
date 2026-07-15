"""Stream-mode terminal I/O (stream_mode param) for terminal.input/resize/close without
emitting terminal.result.

Uses the remote_runtime operation map and delegates session/publisher cleanup
to TerminalFrameLifecycle. Must not import drowai_runner.cloud_client.
"""

from __future__ import annotations

from typing import Callable, Mapping

from drowai_runner.operation_service import RunnerOperationService
from runtime_shared.runner_protocol import RunnerEnvelope, RunnerMessageType

from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.runtime.operation_map import map_remote_runtime_operation
from drowai_runner.control_channel.terminal.frames import TerminalFrameLifecycle


class TerminalStreamHandler:
    """Stream-mode terminal operation handler for runner cloud control channel."""

    def __init__(
        self,
        *,
        operation_service_provider: Callable[[], RunnerOperationService],
        frame_lifecycle: TerminalFrameLifecycle,
    ) -> None:
        self._operation_service_provider = operation_service_provider
        self._frame_lifecycle = frame_lifecycle

    def is_request(self, inbound: RunnerEnvelope) -> bool:
        """Return whether a terminal operation should use stream-mode semantics."""
        if inbound.message_type not in {
            RunnerMessageType.TERMINAL_INPUT,
            RunnerMessageType.TERMINAL_RESIZE,
            RunnerMessageType.TERMINAL_CLOSE,
        }:
            return False
        params = getattr(inbound.payload, "params", {})
        return isinstance(params, Mapping) and bool(params.get("stream_mode"))

    def execute(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        inbound: RunnerEnvelope,
        context: _RemoteRuntimeRequestContext,
    ) -> None:
        """Execute stream-mode terminal I/O without returning terminal.result."""
        operation_name, operation_params = map_remote_runtime_operation(
            inbound=inbound,
            context=context,
        )
        response = self._operation_service_provider().dispatch_operation(
            operation=operation_name,
            params=operation_params,
        )
        status = str(response.get("status") or "").strip().lower()
        session_id = str(operation_params.get("session_id") or "").strip()
        if inbound.message_type is RunnerMessageType.TERMINAL_CLOSE and session_id:
            self._frame_lifecycle.stop_frame_publisher(session_id)
            self._frame_lifecycle.remove_session_tracking(session_id)
            return
        if status == "succeeded" and session_id:
            self._frame_lifecycle.register_active_session(
                session_id=session_id,
                runtime_job_id=context.runtime_job_id,
                task_id=context.task_id,
            )
