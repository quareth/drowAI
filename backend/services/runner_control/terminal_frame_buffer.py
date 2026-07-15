"""In-memory terminal frame buffering for runner-backed terminal polling reads.

Scope:
- Buffer validated `terminal.frame` events by tenant/task/runtime-job/session identity.
- Enforce monotonic per-session frame ordering and bounded memory usage.
- Provide bounded compatibility reads for provider `read_terminal_output` polling.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Any

from runtime_shared.runner_protocol import RUNNER_TERMINAL_FRAME_MAX_BYTES


@dataclass(frozen=True, slots=True)
class TerminalFrame:
    """One buffered terminal frame."""

    sequence: int
    stream: str
    data: str
    byte_count: int


@dataclass(slots=True)
class _SessionBuffer:
    frames: deque[TerminalFrame]
    total_bytes: int = 0
    last_sequence: int = -1
    updated_at_monotonic: float = 0.0


class RunnerTerminalFrameBuffer:
    """Bounded terminal frame buffer keyed by runner task/session scope."""

    def __init__(
        self,
        *,
        max_sessions: int = 1024,
        max_frames_per_session: int = 512,
        max_bytes_per_session: int = 512 * 1024,
        max_frame_bytes: int = RUNNER_TERMINAL_FRAME_MAX_BYTES,
    ) -> None:
        self._max_sessions = max(1, int(max_sessions))
        self._max_frames_per_session = max(1, int(max_frames_per_session))
        self._max_bytes_per_session = max(1, int(max_bytes_per_session))
        self._max_frame_bytes = max(1, int(max_frame_bytes))
        self._buffers: dict[tuple[int, int, str, str], _SessionBuffer] = {}
        self._session_last_sequences: dict[tuple[int, int, str], int] = {}
        self._bound_sessions_by_runtime_job: dict[tuple[int, int, str], str] = {}
        self._lock = RLock()

    def bind_terminal_session(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: str,
        session_id: str,
    ) -> bool:
        """Bind one task/runtime-job route to its active terminal session id."""
        normalized_runtime_job_id = str(runtime_job_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_runtime_job_id or not normalized_session_id:
            return False

        route_key = (int(tenant_id), int(task_id), normalized_runtime_job_id)
        with self._lock:
            self._bound_sessions_by_runtime_job[route_key] = normalized_session_id
        return True

    def is_terminal_session_bound(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: str,
        session_id: str,
    ) -> bool:
        """Return whether the given route is currently bound to the provided session id."""
        normalized_runtime_job_id = str(runtime_job_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_runtime_job_id or not normalized_session_id:
            return False
        route_key = (int(tenant_id), int(task_id), normalized_runtime_job_id)
        with self._lock:
            return self._bound_sessions_by_runtime_job.get(route_key) == normalized_session_id

    def unbind_terminal_session(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Remove active terminal-session bindings by runtime route and/or session id."""
        scoped_tenant_id = int(tenant_id)
        scoped_task_id = int(task_id)
        normalized_runtime_job_id = str(runtime_job_id or "").strip()
        normalized_session_id = str(session_id or "").strip()

        with self._lock:
            if normalized_runtime_job_id:
                route_key = (scoped_tenant_id, scoped_task_id, normalized_runtime_job_id)
                if normalized_session_id:
                    if self._bound_sessions_by_runtime_job.get(route_key) == normalized_session_id:
                        self._bound_sessions_by_runtime_job.pop(route_key, None)
                else:
                    self._bound_sessions_by_runtime_job.pop(route_key, None)
            if normalized_session_id:
                keys_to_remove = [
                    key
                    for key, bound_session_id in self._bound_sessions_by_runtime_job.items()
                    if key[0] == scoped_tenant_id
                    and key[1] == scoped_task_id
                    and bound_session_id == normalized_session_id
                ]
                for key in keys_to_remove:
                    self._bound_sessions_by_runtime_job.pop(key, None)

    def append_frame(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: str,
        session_id: str,
        sequence: int,
        stream: str,
        data: str,
    ) -> bool:
        """Append one frame when it is in-order and within configured limits."""
        if sequence < 0:
            return False
        normalized_runtime_job_id = str(runtime_job_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_runtime_job_id or not normalized_session_id:
            return False

        encoded = str(data or "").encode("utf-8", errors="replace")
        if len(encoded) > self._max_frame_bytes:
            return False
        frame_data = encoded.decode("utf-8", errors="replace")
        frame_bytes = len(encoded)
        key = (int(tenant_id), int(task_id), normalized_runtime_job_id, normalized_session_id)
        session_key = (int(tenant_id), int(task_id), normalized_session_id)

        with self._lock:
            session_last_sequence = int(self._session_last_sequences.get(session_key, -1))
            if sequence <= session_last_sequence:
                return False

            buffer = self._buffers.get(key)
            if buffer is None:
                self._trim_sessions_for_new_key()
                buffer = _SessionBuffer(frames=deque())
                self._buffers[key] = buffer

            frame = TerminalFrame(
                sequence=sequence,
                stream=str(stream or "stdout"),
                data=frame_data,
                byte_count=frame_bytes,
            )
            buffer.frames.append(frame)
            buffer.total_bytes += frame_bytes
            buffer.last_sequence = sequence
            buffer.updated_at_monotonic = monotonic()
            self._session_last_sequences[session_key] = sequence
            self._trim_session_buffer(buffer)
            return True

    def read_frames(
        self,
        *,
        tenant_id: int,
        task_id: int,
        session_id: str,
        runtime_job_id: str | None = None,
        after_sequence: int | None = None,
        max_bytes: int = 32768,
        max_frames: int = 128,
    ) -> dict[str, Any]:
        """Return bounded ordered frames after `after_sequence` for one scoped session."""
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return {
                "session_id": normalized_session_id,
                "runtime_job_id": None,
                "frames": [],
                "data": "",
                "next_sequence": after_sequence if isinstance(after_sequence, int) else -1,
            }
        safe_after = int(after_sequence) if isinstance(after_sequence, int) else -1
        safe_max_bytes = max(1, int(max_bytes))
        safe_max_frames = max(1, int(max_frames))

        with self._lock:
            key = self._resolve_read_key(
                tenant_id=int(tenant_id),
                task_id=int(task_id),
                session_id=normalized_session_id,
                runtime_job_id=runtime_job_id,
            )
            if key is None:
                return {
                    "session_id": normalized_session_id,
                    "runtime_job_id": str(runtime_job_id or "").strip() or None,
                    "frames": [],
                    "data": "",
                    "next_sequence": safe_after,
                }

            buffer = self._buffers.get(key)
            if buffer is None:
                return {
                    "session_id": normalized_session_id,
                    "runtime_job_id": key[2],
                    "frames": [],
                    "data": "",
                    "next_sequence": safe_after,
                }

            collected: list[TerminalFrame] = []
            byte_count = 0
            next_sequence = safe_after
            for frame in buffer.frames:
                if frame.sequence <= safe_after:
                    continue
                if len(collected) >= safe_max_frames:
                    break
                if byte_count + frame.byte_count > safe_max_bytes:
                    break
                collected.append(frame)
                byte_count += frame.byte_count
                next_sequence = frame.sequence

            return {
                "session_id": normalized_session_id,
                "runtime_job_id": key[2],
                "frames": [
                    {
                        "sequence": frame.sequence,
                        "stream": frame.stream,
                        "data": frame.data,
                    }
                    for frame in collected
                ],
                "data": "".join(frame.data for frame in collected),
                "next_sequence": next_sequence,
            }

    def clear_session(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: str,
        session_id: str,
    ) -> None:
        """Drop one terminal session buffer by exact scoped key."""
        key = (
            int(tenant_id),
            int(task_id),
            str(runtime_job_id or "").strip(),
            str(session_id or "").strip(),
        )
        with self._lock:
            self._remove_buffer_key(key)

    def clear_terminal_session(
        self,
        *,
        tenant_id: int,
        task_id: int,
        session_id: str,
    ) -> None:
        """Drop all terminal frame buffers for one tenant/task/session scope."""
        scoped_tenant_id = int(tenant_id)
        scoped_task_id = int(task_id)
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        with self._lock:
            keys_to_remove = [
                key
                for key in self._buffers
                if key[0] == scoped_tenant_id
                and key[1] == scoped_task_id
                and key[3] == normalized_session_id
            ]
            for key in keys_to_remove:
                self._remove_buffer_key(key)
            self._session_last_sequences.pop(
                (scoped_tenant_id, scoped_task_id, normalized_session_id),
                None,
            )
            self.unbind_terminal_session(
                tenant_id=scoped_tenant_id,
                task_id=scoped_task_id,
                session_id=normalized_session_id,
            )

    def clear_task(self, *, task_id: int, tenant_id: int | None = None) -> None:
        """Drop all buffered sessions for one task, optionally scoped to one tenant."""
        scoped_task_id = int(task_id)
        scoped_tenant_id = int(tenant_id) if tenant_id is not None else None
        with self._lock:
            keys_to_remove = []
            for key in self._buffers:
                key_tenant_id, key_task_id, _, _ = key
                if key_task_id != scoped_task_id:
                    continue
                if scoped_tenant_id is not None and key_tenant_id != scoped_tenant_id:
                    continue
                keys_to_remove.append(key)
            for key in keys_to_remove:
                self._remove_buffer_key(key)
            if scoped_tenant_id is None:
                binding_keys = [
                    key
                    for key in self._bound_sessions_by_runtime_job
                    if key[1] == scoped_task_id
                ]
            else:
                binding_keys = [
                    key
                    for key in self._bound_sessions_by_runtime_job
                    if key[0] == scoped_tenant_id and key[1] == scoped_task_id
                ]
            for key in binding_keys:
                self._bound_sessions_by_runtime_job.pop(key, None)

    def reset(self) -> None:
        """Clear all buffered session state (test utility)."""
        with self._lock:
            self._buffers.clear()
            self._session_last_sequences.clear()
            self._bound_sessions_by_runtime_job.clear()

    def _resolve_read_key(
        self,
        *,
        tenant_id: int,
        task_id: int,
        session_id: str,
        runtime_job_id: str | None,
    ) -> tuple[int, int, str, str] | None:
        normalized_runtime_job_id = str(runtime_job_id or "").strip()
        if normalized_runtime_job_id:
            key = (tenant_id, task_id, normalized_runtime_job_id, session_id)
            if key in self._buffers:
                return key

        best_key: tuple[int, int, str, str] | None = None
        best_updated_at = -1.0
        for key, buffer in self._buffers.items():
            key_tenant_id, key_task_id, _, key_session_id = key
            if key_tenant_id != tenant_id or key_task_id != task_id or key_session_id != session_id:
                continue
            if buffer.updated_at_monotonic >= best_updated_at:
                best_key = key
                best_updated_at = buffer.updated_at_monotonic
        return best_key

    def _trim_session_buffer(self, buffer: _SessionBuffer) -> None:
        while len(buffer.frames) > self._max_frames_per_session and buffer.frames:
            dropped = buffer.frames.popleft()
            buffer.total_bytes = max(0, buffer.total_bytes - dropped.byte_count)
        while buffer.total_bytes > self._max_bytes_per_session and buffer.frames:
            dropped = buffer.frames.popleft()
            buffer.total_bytes = max(0, buffer.total_bytes - dropped.byte_count)

    def _trim_sessions_for_new_key(self) -> None:
        if len(self._buffers) < self._max_sessions:
            return
        oldest_key = None
        oldest_timestamp = float("inf")
        for key, buffer in self._buffers.items():
            if buffer.updated_at_monotonic < oldest_timestamp:
                oldest_timestamp = buffer.updated_at_monotonic
                oldest_key = key
        if oldest_key is not None:
            self._remove_buffer_key(oldest_key)

    def _remove_buffer_key(self, key: tuple[int, int, str, str]) -> None:
        self._buffers.pop(key, None)
        self._drop_session_sequence_if_unused(
            tenant_id=key[0],
            task_id=key[1],
            session_id=key[3],
        )

    def _drop_session_sequence_if_unused(self, *, tenant_id: int, task_id: int, session_id: str) -> None:
        session_key = (tenant_id, task_id, session_id)
        for existing_key in self._buffers:
            if existing_key[0] == tenant_id and existing_key[1] == task_id and existing_key[3] == session_id:
                return
        self._session_last_sequences.pop(session_key, None)


_BUFFER_SINGLETON = RunnerTerminalFrameBuffer()


def get_runner_terminal_frame_buffer() -> RunnerTerminalFrameBuffer:
    """Return process-wide terminal frame buffer used by runtime provider/event ingest."""
    return _BUFFER_SINGLETON


__all__ = ["RunnerTerminalFrameBuffer", "get_runner_terminal_frame_buffer"]
