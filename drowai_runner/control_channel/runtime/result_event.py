"""Remote-runtime result-event payload assembly and success event-type mapping.

Owns result-event payload construction and success event-type resolution for
remote_runtime operations. Delegates terminal session tracking to injected
``TerminalFrameLifecycle``. Performs no validation, operation dispatch, or
websocket I/O. Must not import ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Mapping

from runtime_shared.runner_protocol import RunnerEnvelope, RunnerMessageType

from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.terminal.frames import TerminalFrameLifecycle


_LIFECYCLE_REQUEST_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_RETIRE,
    }
)


class RemoteRuntimeResultEventBuilder:
    """Builds remote_runtime result-event payloads from operation responses."""

    def __init__(
        self,
        *,
        frame_lifecycle: TerminalFrameLifecycle,
    ) -> None:
        self._frame_lifecycle = frame_lifecycle

    def build_result_event(
        self,
        *,
        inbound: RunnerEnvelope,
        response: dict[str, object],
        context: _RemoteRuntimeRequestContext,
    ) -> tuple[RunnerMessageType, dict[str, object]]:
        payload = inbound.payload
        operation_id = str(getattr(payload, "operation_id", inbound.message_id))
        if inbound.message_type in {
            RunnerMessageType.TERMINAL_OPEN,
            RunnerMessageType.TERMINAL_INPUT,
            RunnerMessageType.TERMINAL_RESIZE,
            RunnerMessageType.TERMINAL_CLOSE,
        }:
            metadata = response.get("metadata")
            terminal_metadata = metadata if isinstance(metadata, dict) else {}
            terminal_operation = {
                RunnerMessageType.TERMINAL_OPEN: "open",
                RunnerMessageType.TERMINAL_INPUT: "input",
                RunnerMessageType.TERMINAL_RESIZE: "resize",
                RunnerMessageType.TERMINAL_CLOSE: "close",
            }[inbound.message_type]
            session_id = str(terminal_metadata.get("session_id") or getattr(payload, "session_id", "") or "")
            event_payload = {
                "operation_id": operation_id,
                "terminal_operation": terminal_operation,
                "session_id": session_id,
                "status": str(response.get("status") or "failed"),
                "sequence": (
                    int(terminal_metadata["sequence"])
                    if isinstance(terminal_metadata.get("sequence"), int)
                    else None
                ),
                "error_code": (
                    str(response.get("error_code"))
                    if response.get("error_code") is not None
                    else None
                ),
                "error_message": (
                    str(response.get("error_message"))
                    if response.get("error_message") is not None
                    else None
                ),
                "result": {
                    "runtime_job_id": context.runtime_job_id,
                    "task_id": context.task_id,
                    "workspace_id": context.workspace_id,
                    **{
                        str(key): value
                        for key, value in terminal_metadata.items()
                    },
                },
            }
            self._frame_lifecycle.track_session_state(
                inbound=inbound,
                context=context,
                event_payload=event_payload,
            )
            return RunnerMessageType.TERMINAL_RESULT, event_payload

        accepted = bool(response.get("accepted"))
        status = str(response.get("status") or "failed")
        error_code = response.get("error_code")
        error_message = response.get("error_message")
        metadata = response.get("metadata")
        result_payload: dict[str, object] = {
            "runtime_job_id": context.runtime_job_id,
            "task_id": context.task_id,
            "workspace_id": context.workspace_id,
        }
        if isinstance(metadata, dict):
            result_payload.update(metadata)

        lifecycle_intent = ""
        payload_params = getattr(payload, "params", {})
        if isinstance(payload_params, Mapping):
            lifecycle_intent = str(payload_params.get("lifecycle_intent") or "").strip().lower()

        success = accepted and status == "succeeded"
        if success:
            event_type = self.success_event_type(inbound.message_type)
            if inbound.message_type is RunnerMessageType.TASK_STOP:
                result_payload["lifecycle_outcome"] = "cancelled" if lifecycle_intent == "cancel" else "stopped"
            elif inbound.message_type is RunnerMessageType.TASK_RETIRE:
                result_payload["lifecycle_outcome"] = "retired"
        elif inbound.message_type in _LIFECYCLE_REQUEST_TYPES:
            event_type = RunnerMessageType.RUNTIME_FAILED
        else:
            event_type = inbound.message_type

        event_payload = {
            "operation_id": operation_id,
            "status": status,
            "error_code": str(error_code) if error_code is not None else None,
            "error_message": str(error_message) if error_message is not None else None,
            "result": result_payload,
        }
        return event_type, event_payload

    @staticmethod
    def success_event_type(message_type: RunnerMessageType) -> RunnerMessageType:
        if message_type is RunnerMessageType.TASK_START:
            return RunnerMessageType.RUNTIME_STARTED
        if message_type is RunnerMessageType.TASK_PAUSE:
            return RunnerMessageType.RUNTIME_PAUSED
        if message_type is RunnerMessageType.TASK_RESUME:
            return RunnerMessageType.RUNTIME_RESUMED
        if message_type is RunnerMessageType.TASK_STOP:
            return RunnerMessageType.RUNTIME_STOPPED
        if message_type is RunnerMessageType.TASK_RETIRE:
            return RunnerMessageType.RUNTIME_RETIRED
        return message_type
