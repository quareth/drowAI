"""Replay and buffer behavior tests for the terminal session manager."""

import asyncio
import pytest

from backend.services.terminal_session_manager import TerminalSessionManager, TerminalSession


class FakeWebSocket:
    def __init__(self):
        self.texts = []
        self.bytes_list = []

    async def send_text(self, text: str):
        self.texts.append(text)

    async def send_bytes(self, data: bytes):
        # simulate network send by recording the payload
        self.bytes_list.append(bytes(data))


@pytest.mark.asyncio
async def test_attach_replay_batches_small_chunks():
    mgr = TerminalSessionManager()
    session = TerminalSession(
        session_id="s1",
        task_id=1,
        user_id=1,
        container_name="c1",
        connection_type="docker_exec",
    )
    # simulate buffered output: 100 small chunks of 10 bytes
    payload = b"0123456789\n"
    for _ in range(100):
        session.output_buffer.append(payload)
        session.buffer_bytes += len(payload)

    mgr.sessions[session.session_id] = session
    ws = FakeWebSocket()

    ok = await mgr.attach_websocket(session.session_id, ws)
    assert ok is True
    # first message is a JSON 'session_created'
    assert ws.texts, "session_created should be sent"
    # then batched binary frames
    assert ws.bytes_list, "replay bytes should be sent"
    # ensure the total bytes equal the concatenated buffer
    total_sent = sum(len(b) for b in ws.bytes_list)
    assert total_sent == session.buffer_bytes
    # ensure batching occurred (not all payloads individually)
    assert len(ws.bytes_list) < 100


def test_append_to_buffer_trims_max():
    mgr = TerminalSessionManager()
    session = TerminalSession(
        session_id="s2",
        task_id=1,
        user_id=1,
        container_name="c1",
        connection_type="docker_exec",
        max_buffer_bytes=16,
    )
    # Append more than max
    mgr._append_to_buffer(session, b"A" * 32)
    assert session.buffer_bytes <= session.max_buffer_bytes
    # Oversized single chunks are dropped entirely by the existing trim logic.
    assert list(session.output_buffer) == []
