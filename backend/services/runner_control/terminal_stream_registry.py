"""In-memory terminal stream routing for active cloud runner channels.

Responsibilities:
- Track active runner-channel websocket senders for non-durable terminal I/O.
- Buffer stream-mode terminal frames by tenant, runner, task, and session.
- Expose a provider-facing stream client compatible with terminal manager I/O.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RUNNER_TERMINAL_FRAME_MAX_BYTES,
    RunnerEnvelope,
    RunnerMessageType,
)

TERMINAL_STREAM_CAPABILITY = "terminal_stream_v1"
_STREAM_MESSAGE_PREFIX = "terminal-stream-"
_MAX_BUFFER_BYTES = 512 * 1024
_MAX_FRAME_BYTES = RUNNER_TERMINAL_FRAME_MAX_BYTES

ChannelSender = Callable[[RunnerEnvelope], Awaitable[None]]
FrameSink = Callable[..., Awaitable[bool]]


@dataclass(slots=True)
class _ChannelBinding:
    sender: ChannelSender


@dataclass(slots=True)
class _StreamBuffer:
    frames: deque[bytes] = field(default_factory=deque)
    byte_count: int = 0
    event: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False


class CloudTerminalStreamClient:
    """Provider-facing cloud terminal stream backed by the runner channel."""

    push_frames = True

    def __init__(
        self,
        *,
        registry: "RunnerTerminalStreamRegistry",
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
        runtime_job_id: str,
        workspace_id: str,
        runtime_image: str,
    ) -> None:
        self._registry = registry
        self._tenant_id = int(tenant_id)
        self._runner_id = runner_id
        self._task_id = int(task_id)
        self._session_id = str(session_id).strip()
        self._runtime_job_id = str(runtime_job_id).strip()
        self._workspace_id = str(workspace_id).strip() or f"task-{task_id}"
        self._runtime_image = str(runtime_image).strip()
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def closed(self) -> bool:
        return self._closed

    def channel_connected(self) -> bool:
        """Return whether the backing runner channel is currently connected."""
        return self._registry.has_channel(tenant_id=self._tenant_id, runner_id=self._runner_id)

    async def send_input(self, data: str | bytes) -> None:
        """Send terminal input without creating a runtime job."""
        if self._closed:
            return
        text = data.decode("utf-8", errors="surrogateescape") if isinstance(data, bytes) else str(data)
        await self._send_stream_message(
            message_type=RunnerMessageType.TERMINAL_INPUT,
            operation="terminal.input",
            params={"session_id": self._session_id, "data": text},
        )

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the remote PTY without creating a runtime job."""
        if self._closed:
            return
        await self._send_stream_message(
            message_type=RunnerMessageType.TERMINAL_RESIZE,
            operation="terminal.resize",
            params={"session_id": self._session_id, "cols": int(cols), "rows": int(rows)},
        )

    async def read_output(self, size: int = 4096, timeout: float | None = None) -> bytes:
        """Read buffered stream frames up to `size` bytes."""
        return await self._registry.read_stream_output(
            tenant_id=self._tenant_id,
            runner_id=self._runner_id,
            task_id=self._task_id,
            session_id=self._session_id,
            size=size,
            timeout=timeout,
        )

    async def close(self) -> None:
        """Close the in-memory stream binding.

        Durable terminal.close remains responsible for PTY cleanup so the runner
        sees one authoritative lifecycle operation.
        """
        if self._closed:
            return
        self._closed = True
        self._registry.unregister_stream(
            tenant_id=self._tenant_id,
            runner_id=self._runner_id,
            task_id=self._task_id,
            session_id=self._session_id,
        )

    async def _send_stream_message(
        self,
        *,
        message_type: RunnerMessageType,
        operation: str,
        params: dict[str, Any],
    ) -> None:
        payload_params = {
            **params,
            "runtime_job_id": self._runtime_job_id,
            "stream_mode": True,
        }
        envelope = RunnerEnvelope(
            message_id=f"{_STREAM_MESSAGE_PREFIX}{uuid4().hex}",
            message_type=message_type,
            schema_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
            tenant_id=str(self._tenant_id),
            runner_id=str(self._runner_id),
            correlation_id=None,
            runtime_job_id=self._runtime_job_id,
            task_id=self._task_id,
            created_at=datetime.now(tz=UTC).isoformat(),
            payload={
                "runtime_job_id": self._runtime_job_id,
                "operation_id": f"{operation}:{uuid4().hex}",
                "workspace_id": self._workspace_id,
                "runtime_image": self._runtime_image,
                "operation": operation,
                "params": payload_params,
            },
            raw_message_type=message_type.value,
        )
        await self._registry.send_stream_envelope(
            tenant_id=self._tenant_id,
            runner_id=self._runner_id,
            envelope=envelope,
        )


class RunnerTerminalStreamRegistry:
    """Process-local terminal stream registry for cloud runner channels."""

    def __init__(self) -> None:
        self._channels: dict[tuple[int, UUID], _ChannelBinding] = {}
        self._buffers: dict[tuple[int, UUID, int, str], _StreamBuffer] = {}
        self._frame_sink: FrameSink | None = None
        self._lock = RLock()

    def register_frame_sink(self, sink: FrameSink) -> None:
        """Register the active backend terminal-session frame sink."""
        with self._lock:
            self._frame_sink = sink

    def unregister_frame_sink(self, sink: FrameSink) -> None:
        """Unregister the active frame sink when it matches the provided sink."""
        with self._lock:
            if self._frame_sink is sink:
                self._frame_sink = None

    def register_channel(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        sender: ChannelSender,
    ) -> None:
        """Register the active websocket sender for one runner channel."""
        with self._lock:
            self._channels[(int(tenant_id), runner_id)] = _ChannelBinding(sender=sender)

    def has_channel(self, *, tenant_id: int, runner_id: UUID) -> bool:
        """Return whether the runner has an active terminal stream channel."""
        with self._lock:
            return (int(tenant_id), runner_id) in self._channels

    def unregister_channel(self, *, tenant_id: int, runner_id: UUID) -> None:
        """Drop channel and close all stream buffers for the runner."""
        key = (int(tenant_id), runner_id)
        with self._lock:
            self._channels.pop(key, None)
            stream_keys = [stream_key for stream_key in self._buffers if stream_key[:2] == key]
            buffers = [self._buffers.pop(stream_key) for stream_key in stream_keys]
        for buffer in buffers:
            buffer.closed = True
            buffer.event.set()

    def register_stream(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
    ) -> None:
        """Create an in-memory stream buffer for a runner terminal session."""
        key = self._stream_key(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            session_id=session_id,
        )
        with self._lock:
            self._buffers.setdefault(key, _StreamBuffer())

    def unregister_stream(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
    ) -> None:
        """Remove one stream buffer and wake any pending readers."""
        key = self._stream_key(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            session_id=session_id,
        )
        with self._lock:
            buffer = self._buffers.pop(key, None)
        if buffer is not None:
            buffer.closed = True
            buffer.event.set()

    async def send_stream_envelope(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        envelope: RunnerEnvelope,
    ) -> None:
        """Send a non-durable terminal stream envelope over the active channel."""
        with self._lock:
            binding = self._channels.get((int(tenant_id), runner_id))
        if binding is None:
            raise RuntimeError("Runner terminal stream channel is not connected.")
        await binding.sender(envelope)

    def handle_stream_ack(self, envelope: RunnerEnvelope) -> bool:
        """Return true when a stream-mode ACK was consumed without persistence."""
        if envelope.message_type is not RunnerMessageType.RUNNER_ACK:
            return False
        payload = envelope.payload
        acked_message_id = str(getattr(payload, "acked_message_id", "") or "").strip()
        return acked_message_id.startswith(_STREAM_MESSAGE_PREFIX)

    def append_stream_frame(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
        data: str,
    ) -> bool:
        """Append a known stream-mode frame and wake readers."""
        key = self._stream_key(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            session_id=session_id,
        )
        encoded = str(data or "").encode("utf-8", errors="replace")
        if len(encoded) > _MAX_FRAME_BYTES:
            return False
        with self._lock:
            buffer = self._buffers.get(key)
            if buffer is None or buffer.closed:
                return False
            buffer.frames.append(encoded)
            buffer.byte_count += len(encoded)
            while buffer.byte_count > _MAX_BUFFER_BYTES and buffer.frames:
                removed = buffer.frames.popleft()
                buffer.byte_count -= len(removed)
        buffer.event.set()
        return True

    async def ingest_stream_frame(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
        data: str,
    ) -> bool:
        """Append a frame and push it directly to the terminal manager when possible."""
        encoded = str(data or "").encode("utf-8", errors="replace")
        accepted = self.append_stream_frame(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            session_id=session_id,
            data=encoded.decode("utf-8", errors="replace"),
        )
        if not accepted:
            return False
        with self._lock:
            sink = self._frame_sink
        if sink is not None:
            try:
                await sink(
                    tenant_id=int(tenant_id),
                    runner_id=runner_id,
                    task_id=int(task_id),
                    provider_session_id=str(session_id).strip(),
                    data=encoded,
                )
            except Exception:
                pass
        return True

    async def read_stream_output(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
        size: int,
        timeout: float | None,
    ) -> bytes:
        """Read buffered stream bytes with optional timeout."""
        key = self._stream_key(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            session_id=session_id,
        )
        deadline = None if timeout is None else asyncio.get_running_loop().time() + max(0.0, float(timeout))
        safe_size = max(1, int(size))
        while True:
            with self._lock:
                buffer = self._buffers.get(key)
                if buffer is None or buffer.closed:
                    return b""
                if buffer.frames:
                    chunks: list[bytes] = []
                    remaining = safe_size
                    while buffer.frames and remaining > 0:
                        chunk = buffer.frames[0]
                        if len(chunk) <= remaining:
                            chunks.append(buffer.frames.popleft())
                            buffer.byte_count -= len(chunk)
                            remaining -= len(chunk)
                            continue
                        chunks.append(chunk[:remaining])
                        buffer.frames[0] = chunk[remaining:]
                        buffer.byte_count -= remaining
                        remaining = 0
                    if not buffer.frames:
                        buffer.event.clear()
                    return b"".join(chunks)
                event = buffer.event

            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return b""
            wait_timeout = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_timeout)
            except TimeoutError:
                return b""
            event.clear()

    @staticmethod
    def _stream_key(
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        session_id: str,
    ) -> tuple[int, UUID, int, str]:
        return (int(tenant_id), runner_id, int(task_id), str(session_id).strip())


_REGISTRY = RunnerTerminalStreamRegistry()


def get_runner_terminal_stream_registry() -> RunnerTerminalStreamRegistry:
    """Return the process-local runner terminal stream registry."""
    return _REGISTRY


__all__ = [
    "CloudTerminalStreamClient",
    "FrameSink",
    "RunnerTerminalStreamRegistry",
    "TERMINAL_STREAM_CAPABILITY",
    "get_runner_terminal_stream_registry",
]
