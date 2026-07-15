"""Terminal frame read limits, sequence numbering, background frame publisher threads,
session tracking, emit-for-active-sessions, and disconnect/reset cleanup.

Owns mutations to injected client-lifetime terminal dicts (active_terminal_sessions,
terminal_frame_sequences, terminal_frame_publishers) by reference. Performs websocket
sends only through call-time websocket objects. Must not import drowai_runner.cloud_client.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Mapping

from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.protocol_handler import build_remote_runtime_envelope
from runtime_shared.runner_protocol import RunnerEnvelope, RunnerMessageType

from drowai_runner.control_channel.constants import (
    _TERMINAL_FRAME_MAX_BYTES,
    _TERMINAL_FRAME_MAX_FRAMES_PER_OPERATION,
    _TERMINAL_FRAME_MAX_TOTAL_BYTES_PER_OPERATION,
)
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.terminal.models import (
    _ActiveTerminalSession,
    _TerminalFramePublisher,
)


class TerminalFrameLifecycle:
    """Terminal frame/session lifecycle for runner cloud control channel."""

    def __init__(
        self,
        *,
        active_terminal_sessions: dict[str, _ActiveTerminalSession],
        terminal_frame_sequences: dict[str, int],
        terminal_frame_publishers: dict[str, _TerminalFramePublisher],
        operation_service_provider: Callable[[], RunnerOperationService],
    ) -> None:
        self._active_terminal_sessions = active_terminal_sessions
        self._terminal_frame_sequences = terminal_frame_sequences
        self._terminal_frame_publishers = terminal_frame_publishers
        self._operation_service_provider = operation_service_provider

    def remove_session_tracking(self, session_id: str) -> None:
        """Drop client-lifetime session and frame-sequence state for one session."""
        self._active_terminal_sessions.pop(session_id, None)
        self._terminal_frame_sequences.pop(session_id, None)

    def register_active_session(
        self,
        *,
        session_id: str,
        runtime_job_id: str,
        task_id: int,
    ) -> None:
        """Record an active terminal session for frame publishing and stream mode."""
        self._active_terminal_sessions[session_id] = _ActiveTerminalSession(
            runtime_job_id=runtime_job_id,
            task_id=task_id,
        )

    def reset_session_state(self) -> None:
        self._active_terminal_sessions.clear()
        self._terminal_frame_sequences.clear()
        self.stop_frame_publishers()

    def collect_frames(
        self,
        *,
        inbound: RunnerEnvelope,
        event_type: RunnerMessageType,
        event_payload: Mapping[str, object],
    ) -> list[dict[str, object]]:
        if event_type is not RunnerMessageType.TERMINAL_RESULT:
            return []
        if inbound.message_type not in {RunnerMessageType.TERMINAL_OPEN, RunnerMessageType.TERMINAL_INPUT}:
            return []
        if str(event_payload.get("status") or "").strip().lower() != "succeeded":
            return []
        session_id = str(event_payload.get("session_id") or "").strip()
        if not session_id:
            return []
        frames, should_drop_session = self.read_terminal_frames(session_id=session_id)
        if should_drop_session:
            self._active_terminal_sessions.pop(session_id, None)
            self._terminal_frame_sequences.pop(session_id, None)
        return frames

    def next_frame_sequence(self, session_id: str) -> int:
        current = int(self._terminal_frame_sequences.get(session_id, -1))
        next_sequence = current + 1
        self._terminal_frame_sequences[session_id] = next_sequence
        return next_sequence

    def read_terminal_frames(
        self, *, session_id: str
    ) -> tuple[list[dict[str, object]], bool]:
        frames: list[dict[str, object]] = []
        bytes_remaining = _TERMINAL_FRAME_MAX_TOTAL_BYTES_PER_OPERATION
        should_drop_session = False
        while len(frames) < _TERMINAL_FRAME_MAX_FRAMES_PER_OPERATION and bytes_remaining > 0:
            read_response = self._operation_service_provider().dispatch_operation(
                operation="terminal_read",
                params={
                    "session_id": session_id,
                    "max_bytes": min(_TERMINAL_FRAME_MAX_BYTES, bytes_remaining),
                },
            )
            status = str(read_response.get("status") or "").strip().lower()
            if status != "succeeded":
                error_code = str(read_response.get("error_code") or "").strip()
                if error_code == "RUNNER_TERMINAL_SESSION_NOT_FOUND":
                    should_drop_session = True
                break
            metadata = read_response.get("metadata")
            if not isinstance(metadata, Mapping):
                break
            raw_data = metadata.get("output")
            data = str(raw_data) if isinstance(raw_data, str) else ""
            if not data:
                break
            encoded = data.encode("utf-8", errors="replace")
            start = 0
            while (
                start < len(encoded)
                and len(frames) < _TERMINAL_FRAME_MAX_FRAMES_PER_OPERATION
                and bytes_remaining > 0
            ):
                chunk = encoded[start : start + min(_TERMINAL_FRAME_MAX_BYTES, bytes_remaining)]
                start += len(chunk)
                bytes_remaining -= len(chunk)
                if not chunk:
                    continue
                frames.append(
                    {
                        "session_id": session_id,
                        "sequence": self.next_frame_sequence(session_id),
                        "stream": "stdout",
                        "data": chunk.decode("utf-8", errors="replace"),
                    }
                )
        return frames, should_drop_session

    def sync_publisher_for_event(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        event_type: RunnerMessageType,
        event_payload: Mapping[str, object],
    ) -> None:
        """Start or stop push frame publishing based on terminal lifecycle events."""
        if event_type is not RunnerMessageType.TERMINAL_RESULT:
            return
        operation = str(event_payload.get("terminal_operation") or "").strip().lower()
        session_id = str(event_payload.get("session_id") or "").strip()
        if not session_id:
            return
        if operation == "close":
            self.stop_frame_publisher(session_id)
            return
        if operation == "open":
            active_session = self._active_terminal_sessions.get(session_id)
            if active_session is not None:
                self.start_frame_publisher(
                    websocket=websocket,
                    identity=identity,
                    session_id=session_id,
                )

    def start_frame_publisher(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
        session_id: str,
    ) -> None:
        """Start the per-session PTY output publisher if it is not already running."""
        if session_id in self._terminal_frame_publishers:
            return
        stop_event = threading.Event()

        def _publish() -> None:
            while not stop_event.is_set():
                active_session = self._active_terminal_sessions.get(session_id)
                if active_session is None:
                    break
                frame_payloads, should_drop = self.read_terminal_frames(session_id=session_id)
                if should_drop:
                    self._active_terminal_sessions.pop(session_id, None)
                    self._terminal_frame_sequences.pop(session_id, None)
                    break
                if not frame_payloads:
                    time.sleep(0.005)
                    continue
                for frame_payload in frame_payloads:
                    if stop_event.is_set():
                        break
                    frame_event = build_remote_runtime_envelope(
                        tenant_id=identity.tenant_id,
                        runner_id=identity.runner_id,
                        message_type=RunnerMessageType.TERMINAL_FRAME,
                        payload=frame_payload,
                        runtime_job_id=active_session.runtime_job_id,
                        task_id=active_session.task_id,
                    )
                    try:
                        websocket.send(frame_event.to_json())
                    except Exception:
                        stop_event.set()
                        break
            self._terminal_frame_publishers.pop(session_id, None)

        thread = threading.Thread(
            target=_publish,
            name=f"drowai-terminal-publisher-{session_id}",
            daemon=True,
        )
        self._terminal_frame_publishers[session_id] = _TerminalFramePublisher(
            stop_event=stop_event,
            thread=thread,
        )
        thread.start()

    def stop_frame_publisher(self, session_id: str) -> None:
        publisher = self._terminal_frame_publishers.pop(session_id, None)
        if publisher is None:
            return
        publisher.stop_event.set()
        publisher.thread.join(timeout=1.0)

    def stop_frame_publishers(self) -> None:
        for session_id in list(self._terminal_frame_publishers):
            self.stop_frame_publisher(session_id)

    def emit_for_active_sessions(
        self,
        *,
        websocket: object,
        identity: CloudChannelIdentity,
    ) -> None:
        if not self._active_terminal_sessions:
            return
        stale_session_ids: list[str] = []
        for session_id, session in list(self._active_terminal_sessions.items()):
            frame_payloads, should_drop = self.read_terminal_frames(session_id=session_id)
            if should_drop:
                stale_session_ids.append(session_id)
            for frame_payload in frame_payloads:
                frame_event = build_remote_runtime_envelope(
                    tenant_id=identity.tenant_id,
                    runner_id=identity.runner_id,
                    message_type=RunnerMessageType.TERMINAL_FRAME,
                    payload=frame_payload,
                    runtime_job_id=session.runtime_job_id,
                    task_id=session.task_id,
                )
                websocket.send(frame_event.to_json())
        for session_id in stale_session_ids:
            self._active_terminal_sessions.pop(session_id, None)
            self._terminal_frame_sequences.pop(session_id, None)

    def close_active_sessions(self) -> None:
        """Best-effort disconnect cleanup for runner-owned PTY sessions."""
        if not self._active_terminal_sessions:
            self.stop_frame_publishers()
            return
        for session_id in list(self._active_terminal_sessions):
            self.stop_frame_publisher(session_id)
            try:
                self._operation_service_provider().dispatch_operation(
                    operation="terminal_close",
                    params={"session_id": session_id},
                )
            except Exception:
                pass
            self._active_terminal_sessions.pop(session_id, None)
            self._terminal_frame_sequences.pop(session_id, None)
        self.stop_frame_publishers()

    def track_session_state(
        self,
        *,
        inbound: RunnerEnvelope,
        context: _RemoteRuntimeRequestContext,
        event_payload: Mapping[str, object],
    ) -> None:
        status = str(event_payload.get("status") or "").strip().lower()
        session_id = str(event_payload.get("session_id") or "").strip()
        if status != "succeeded" or not session_id:
            return
        runtime_job_id = ""
        result = event_payload.get("result")
        if isinstance(result, Mapping):
            runtime_job_id = str(result.get("runtime_job_id") or "").strip()
        if not runtime_job_id:
            runtime_job_id = str(context.runtime_job_id or "").strip()
        if not runtime_job_id:
            runtime_job_id = str(inbound.runtime_job_id or "").strip()
        if not runtime_job_id:
            return
        terminal_operation = str(event_payload.get("terminal_operation") or "").strip().lower()
        if terminal_operation == "close":
            self.stop_frame_publisher(session_id)
            self._active_terminal_sessions.pop(session_id, None)
            self._terminal_frame_sequences.pop(session_id, None)
            return
        if terminal_operation in {"open", "input", "resize"}:
            self._active_terminal_sessions[session_id] = _ActiveTerminalSession(
                runtime_job_id=runtime_job_id,
                task_id=int(context.task_id),
            )
