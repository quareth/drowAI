"""Cancellation and capacity tests for dedicated shell command sessions."""

from __future__ import annotations

import asyncio

import pytest

from backend.tests.test_shell_session_service import (
    _CommandTerminalManager,
    _identity,
    _service,
)
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import ShellExecRequest


class _BlockingReadManager(_CommandTerminalManager):
    def __init__(self) -> None:
        super().__init__()
        self.read_started = asyncio.Event()

    async def read_output_result(self, *args, **kwargs):
        self.read_started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_execute_cancellation_interrupts_and_closes_dedicated_exec() -> None:
    manager = _BlockingReadManager()
    service = _service(manager)
    task = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(
                command="silent-running",
                interactive=True,
                yield_time_ms=30_000,
            ),
            capability=ShellCapability.UTILITY,
        )
    )
    await asyncio.wait_for(manager.read_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.sent_inputs == [("terminal-1", b"\x03")]
    assert manager.closed_sessions == ["terminal-1"]


class _BlockingOpenManager(_CommandTerminalManager):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def create_agent_command_session(self, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancelled_start_releases_reserved_capacity() -> None:
    manager = _BlockingOpenManager()
    service = _service(manager)
    task = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(command="blocked", yield_time_ms=0),
            capability=ShellCapability.UTILITY,
        )
    )
    await asyncio.wait_for(manager.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    replacement = _CommandTerminalManager()
    service._terminal_manager = replacement
    result = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    assert result.success is True
