"""Cancellation cleanup tests for terminal-backed shell execution.

Cancellation after a provider session is registered must interrupt and close
the private PTY, remove the public record, and release session capacity.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.tests.test_shell_session_service import (
    FakeTerminalManager,
    _config,
    _identity,
    _service,
)
from runtime_shared.shell_session_contracts import ShellExecRequest


class _BlockingReadTerminalManager(FakeTerminalManager):
    """Block after returning the initial interactive output chunk."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked_read_started = asyncio.Event()
        self.release_blocked_read = asyncio.Event()

    async def read_output_result(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ):
        if self.queues.get(session_id):
            return await super().read_output_result(
                session_id,
                size,
                timeout=timeout,
            )
        self.blocked_read_started.set()
        await self.release_blocked_read.wait()
        return await super().read_output_result(
            session_id,
            size,
            timeout=timeout,
        )


@pytest.mark.asyncio
async def test_execute_cancellation_closes_registered_session_and_releases_capacity() -> None:
    manager = _BlockingReadTerminalManager()
    service = _service(
        manager,
        config=_config(max_active_per_owner=1, max_active_per_task=1),
    )
    operation = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(command="interactive", yield_time_ms=30_000),
        )
    )
    await asyncio.wait_for(manager.blocked_read_started.wait(), timeout=1)

    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert service._records == {}
    assert service._pending_owner_starts == {}
    assert service._pending_task_starts == {}
    assert manager.ctrl_c_writes == 1
    assert manager.closed_sessions == ["terminal-1"]

    follow_up = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )
    assert follow_up.success is True
    assert follow_up.exit_code == 0
