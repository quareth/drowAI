"""
Tests for the Metasploit interactive executor.

These tests exercise the PTY-facing execution path with a fake terminal
session, without requiring a local Metasploit installation.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.tools.exploitation_tools.metasploit.interactive_executor import (
    InteractiveExecutor,
    result_to_dict,
)
from agent.tools.exploitation_tools.metasploit.session_state import SessionStateManager


class FakePtySession:
    """Minimal async PTY session fake used by interactive executor tests."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.writes: list[bytes] = []
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(0.01)
        return b""

    async def close(self) -> None:
        self.closed = True


async def fake_pty_factory(fake_session: FakePtySession, _task_id: str) -> FakePtySession:
    """Return a fake PTY session through the executor's async factory contract."""
    return fake_session


@pytest.fixture
def state_manager(tmp_path):
    """Create an isolated session state manager."""
    return SessionStateManager(workspace_root=str(tmp_path))


@pytest.mark.asyncio
async def test_execute_uses_pty_factory_and_updates_session_state(state_manager):
    """Interactive execution should write to PTY and parse session output."""
    fake_session = FakePtySession(
        [
            b"[*] Started reverse TCP handler on 192.168.1.10:4444\n",
            (
                b"[+] Meterpreter session 1 opened "
                b"(192.168.1.10:4444 -> 192.168.1.20:49158)\n"
                b"meterpreter > "
            ),
        ]
    )
    executor = InteractiveExecutor(
        state_manager=state_manager,
        prompt_timeout=0.1,
        command_timeout=1.0,
        lock_timeout=1.0,
    )
    executor.set_pty_session_factory(
        lambda task_id: fake_pty_factory(fake_session, task_id)
    )

    result = await executor.execute(
        task_id="task-msf",
        command="exploit",
        timeout_sec=1.0,
    )

    assert result.success is True
    assert result.prompt_detected == "meterpreter"
    assert fake_session.writes == [b"exploit\n"]
    assert result.session_state is not None
    assert result.session_state["sessions"][1]["type"] == "meterpreter"
    assert result_to_dict(result)["session_state"]["command_history"] == ["exploit"]


@pytest.mark.asyncio
async def test_execute_returns_error_without_pty_factory(state_manager, monkeypatch):
    """Interactive execution should fail clearly when PTY is unavailable."""
    monkeypatch.delenv("ENABLE_PTY_EXECUTION", raising=False)
    executor = InteractiveExecutor(
        state_manager=state_manager,
        prompt_timeout=0.1,
        command_timeout=1.0,
        lock_timeout=1.0,
    )

    result = await executor.execute(
        task_id="task-msf",
        command="sessions -l",
        timeout_sec=0.1,
    )

    assert result.success is False
    assert result.exit_code == -1
    assert result.errors
    assert "PTY session not available" in result.errors[0]


@pytest.mark.asyncio
async def test_start_and_close_session_manage_fake_pty(state_manager):
    """Session lifecycle helpers should read a prompt and close cleanly."""
    fake_session = FakePtySession([b"msf6 > "])
    executor = InteractiveExecutor(
        state_manager=state_manager,
        prompt_timeout=0.1,
        command_timeout=1.0,
        lock_timeout=1.0,
    )
    executor.set_pty_session_factory(
        lambda task_id: fake_pty_factory(fake_session, task_id)
    )

    started = await executor.start_session("task-msf")
    await executor.close_session("task-msf")

    assert started is True
    assert b"exit -y\n" in fake_session.writes
    assert fake_session.closed is True
