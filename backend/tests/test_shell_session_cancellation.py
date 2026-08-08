"""Cancellation cleanup tests for terminal-backed shell execution.

Cancellation after a provider session is registered must interrupt and close
the private PTY, remove the public record, and release session capacity.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.terminal import shell_session_service as shell_service_module
from backend.tests.test_shell_session_service import (
    FakeTerminalManager,
    _config,
    _identity,
    _service,
)
from runtime_shared.shell_session_contracts import ShellExecRequest, ShellWriteRequest


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


class _ConcurrentPrepareTerminalManager(FakeTerminalManager):
    """Fail one start while keeping later provider terminal opens pending."""

    def __init__(self) -> None:
        super().__init__()
        self.prepare_count = 0
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.third_started = asyncio.Event()
        self.release_pending = asyncio.Event()

    async def prepare_agent_session(self, **kwargs):
        self.prepare_calls.append(kwargs)
        self.prepare_count += 1
        call_number = self.prepare_count
        if call_number == 1:
            self.first_started.set()
            await self.second_started.wait()
            raise RuntimeError("provider open failed")
        if call_number == 2:
            self.second_started.set()
        elif call_number == 3:
            self.third_started.set()
        await self.release_pending.wait()
        return await super().prepare_agent_session(**kwargs)


class _BlockingControlTerminalManager(FakeTerminalManager):
    """Block continuation input while allowing initial command dispatch."""

    async def send_input(self, session_id: str, data: bytes) -> bool:
        if data == b"answer\n":
            await asyncio.Event().wait()
        return await super().send_input(session_id, data)


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


@pytest.mark.asyncio
async def test_failed_concurrent_start_releases_only_its_own_capacity() -> None:
    manager = _ConcurrentPrepareTerminalManager()
    service = _service(
        manager,
        config=_config(max_active_per_owner=2, max_active_per_task=2),
    )

    first = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(command="first", yield_time_ms=0),
        )
    )
    await asyncio.wait_for(manager.first_started.wait(), timeout=1)
    second = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(command="second", yield_time_ms=0),
        )
    )
    first_result = await first
    assert first_result.success is False
    assert first_result.error_code == "command_start_failed"

    third = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(command="third", yield_time_ms=0),
        )
    )
    await asyncio.wait_for(manager.third_started.wait(), timeout=1)

    rejected = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="fourth", yield_time_ms=0),
    )
    assert rejected.success is False
    assert rejected.error_code == "session_limit_reached"
    assert manager.prepare_count == 3

    second.cancel()
    third.cancel()
    await asyncio.gather(second, third, return_exceptions=True)
    assert service._pending_owner_starts == {}
    assert service._pending_task_starts == {}


@pytest.mark.asyncio
async def test_terminal_preparation_has_a_hard_deadline_and_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ConcurrentPrepareTerminalManager()
    service = _service(
        manager,
        config=_config(max_active_per_owner=1, max_active_per_task=1),
    )
    monkeypatch.setattr(shell_service_module, "SHELL_SESSION_PREPARATION_TIMEOUT_SEC", 0.01)

    result = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="blocked", yield_time_ms=0),
    )

    assert result.success is False
    assert result.error_code == "command_start_failed"
    assert service._pending_owner_starts == {}
    assert service._pending_task_starts == {}


@pytest.mark.asyncio
async def test_continuation_input_has_a_hard_deadline_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _BlockingControlTerminalManager()
    service = _service(manager)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="interactive", yield_time_ms=0),
    )
    assert started.session_id is not None
    monkeypatch.setattr(shell_service_module, "SHELL_SESSION_CONTROL_TIMEOUT_SEC", 0.01)

    result = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=started.session_id,
            chars="answer\n",
            yield_time_ms=0,
        ),
    )

    assert result.success is False
    assert result.error_code == "runtime_transport_failed"
    assert started.session_id not in service._records
    assert manager.closed_sessions == ["terminal-1"]
