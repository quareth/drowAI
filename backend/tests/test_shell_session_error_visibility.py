"""Mechanical error visibility tests for terminal-backed shell sessions.

These tests prove that provider/setup failures and silent non-zero exits expose
stable public diagnostics without leaking private exception details.
"""

from __future__ import annotations

import pytest

from backend.services.terminal.shell_session_service import ShellSessionService
from backend.tests.test_shell_session_service import (
    FakeTerminalManager,
    _config,
    _context,
    _identity,
    _noop_lifecycle_projector,
    _service,
)
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
)
from runtime_shared.shell_session_framing import PTY_EXIT_CODE_MARKER


class _FailingSendTerminalManager(FakeTerminalManager):
    """Fail only the provider input transport operation."""

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        return False


class _SilentFailureTerminalManager(FakeTerminalManager):
    """Complete a command with a non-zero exit and no process output."""

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        sent = await super().send_input(session_id, data)
        payload = data.encode() if isinstance(data, str) else data
        if b"silent-failure" in payload:
            start, end = self.session_markers[session_id]
            self.queues[session_id] = [
                f"{start}\n{end}={PTY_EXIT_CODE_MARKER}7\n".encode()
            ]
        return sent


@pytest.mark.asyncio
async def test_runtime_context_resolution_failure_is_visible_as_runtime_unavailable() -> None:
    manager = FakeTerminalManager()

    async def unavailable_context(_identity: ShellSessionIdentity) -> object:
        raise RuntimeError("private provider detail")

    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(),
        runtime_context_resolver=unavailable_context,
    )

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE
    assert update.stderr == "Shell runtime is unavailable for this task."
    assert "private provider detail" not in update.stderr
    assert manager.prepare_calls == []


@pytest.mark.asyncio
async def test_runtime_context_mismatch_has_a_stable_public_error() -> None:
    manager = FakeTerminalManager()
    service = _service(manager, context=_context(task_id=12))

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.COMMAND_START_FAILED
    assert update.stderr == "Shell command could not be started."
    assert manager.prepare_calls == []


@pytest.mark.asyncio
async def test_initial_transport_failure_returns_visible_mechanical_error() -> None:
    update = await _service(_FailingSendTerminalManager()).execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED
    assert update.stderr == (
        "Shell runtime transport failed while processing the session."
    )


@pytest.mark.asyncio
async def test_silent_nonzero_command_returns_exit_detail_in_stderr() -> None:
    update = await _service(_SilentFailureTerminalManager()).execute(
        identity=_identity(),
        request=ShellExecRequest(command="silent-failure", yield_time_ms=0),
    )

    assert update.success is False
    assert update.process_status is ShellProcessStatus.FAILED
    assert update.exit_code == 7
    assert update.stdout == ""
    assert update.stderr == "Command exited with code 7."
