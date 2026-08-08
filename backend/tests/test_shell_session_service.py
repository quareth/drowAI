"""Tests for the terminal-backed shell session service.

These tests use a fake terminal manager to prove session lifecycle, ownership,
limits, and PTY result mapping without opening a real provider terminal.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from backend import config as backend_config
from backend.services.metrics import metrics
from backend.services.runtime_provider import RuntimeCallScope
from backend.services.terminal import manager as terminal_manager_module
from backend.services.terminal.manager import TerminalSessionManager
from backend.services.terminal.shell_session_service import (
    ShellSessionService,
    ShellSessionServiceConfig,
)
from agent.graph.subgraphs.tool_execution_runtime.result_state_projection import (
    preserve_shell_session_result_fields,
)
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellWriteRequest,
)
from runtime_shared.shell_session_framing import PTY_EXIT_CODE_MARKER
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.terminal_contracts import TerminalReadResult

_PUBLIC_ASSERT_FIELDS = {
    "success",
    "process_status",
    "session_id",
    "exit_code",
    "stdout",
    "stdin_available",
    "truncated",
    "error_code",
}


class FakeTerminalManager:
    """Small fake for TerminalSessionManager shell-session I/O methods."""

    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self.sent_inputs: list[tuple[str, bytes]] = []
        self.closed_sessions: list[str] = []
        self.ctrl_c_writes = 0
        self.queues: dict[str, list[bytes]] = {}
        self.deferred_queues: dict[str, list[bytes]] = {}
        self.quiet_interrupt_sessions: set[str] = set()
        self.session_counter = 0
        self.session_markers: dict[str, tuple[str, str]] = {}
        self.fail_read = False
        self.eof_read = False
        self.read_result_calls = 0

    async def prepare_agent_session(self, **kwargs):
        self.prepare_calls.append(kwargs)
        self.session_counter += 1
        session_id = f"terminal-{self.session_counter}"
        self.queues[session_id] = []
        return SimpleNamespace(session_id=session_id)

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        payload = data.encode() if isinstance(data, str) else data
        self.sent_inputs.append((session_id, payload))
        text = payload.decode("utf-8", errors="replace")
        if "__DROWAI_CMD_START_" in text:
            start = text.split("printf '", 1)[1].split("\\n';", 1)[0]
            end = text.split("\\n__DROWAI_CMD_END_", 1)[1].split("=", 1)[0]
            end = f"__DROWAI_CMD_END_{end}"
            self.session_markers[session_id] = (start, end)
            if "echo quick" in text:
                self.queues[session_id].append(
                    f"{start}\nquick\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "echo echoed" in text:
                self.queues[session_id].append(
                    (
                        f"{text}"
                        f"{start}\nechoed\n{end}={PTY_EXIT_CODE_MARKER}0\n"
                    ).encode()
                )
            elif "echo-wrapper-markers-yield" in text:
                self.queues[session_id].append(
                    (
                        "echoed wrapper mentions "
                        f"{start} and {end}={PTY_EXIT_CODE_MARKER}0\n"
                    ).encode()
                )
                self.deferred_queues[session_id] = [
                    f"{start}\nreal output\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                ]
            elif "incomplete-exit-yields" in text:
                self.queues[session_id].append(
                    f"{start}\npartial\n{end}={PTY_EXIT_CODE_MARKER}".encode()
                )
                self.deferred_queues[session_id] = [b"0\n"]
            elif "malformed-complete-exit" in text:
                self.queues[session_id].append(
                    f"{start}\npartial\n{end}=not-an-exit-code\n".encode()
                )
            elif "byte-split-protocol" in text:
                protocol = f"{start}\ncaf\u00e9\n{end}={PTY_EXIT_CODE_MARKER}0\n"
                self.queues[session_id].extend(
                    bytes([byte]) for byte in protocol.encode()
                )
            elif "invalid-utf8-completes" in text:
                self.queues[session_id].append(
                    f"{start}\nbefore:".encode()
                    + b"\xff"
                    + f":after\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "oversized-completes" in text:
                self.queues[session_id].append((f"{start}\n" + ("x" * 5000)).encode())
                self.queues[session_id].append(
                    f"\ncomplete\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "medium-output" in text:
                self.queues[session_id].append(
                    f"{start}\n{'y' * 900}\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "split-immediate-completes" in text:
                self.queues[session_id].append(f"{start}\nhello".encode())
                self.queues[session_id].append(
                    f"\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "quiet-interrupt" in text:
                self.quiet_interrupt_sessions.add(session_id)
                self.queues[session_id].append(f"{start}\nwaiting\n".encode())
            elif "delayed" in text:
                self.queues[session_id].append(f"{start}\nstarted\n".encode())
                self.deferred_queues[session_id] = [
                    f"done\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                ]
            elif "utf8-split" in text:
                utf8_bytes = "caf\u00e9".encode()
                self.queues[session_id].append(f"{start}\n".encode() + utf8_bytes[:-1])
                self.deferred_queues[session_id] = [
                    utf8_bytes[-1:] + f"\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                ]
            elif "interactive" in text:
                self.queues[session_id].append(f"{start}\nwaiting\n".encode())
            else:
                self.queues[session_id].append(
                    f"{start}\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
        elif payload == b"\x03":
            self.ctrl_c_writes += 1
            if session_id not in self.quiet_interrupt_sessions:
                self.queues[session_id].append(b"interrupted during cleanup\n")
        elif payload != b"\x03":
            _start, end = self.session_markers[session_id]
            self.queues[session_id].append(
                b"answer:" + payload + f"{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
            )
        return True

    async def read_output_result(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ) -> TerminalReadResult:
        del size, timeout
        self.read_result_calls += 1
        if self.fail_read:
            return TerminalReadResult(ok=False, error_code="provider_lost")
        if self.eof_read:
            return TerminalReadResult(ok=True, eof=True)
        queue = self.queues.get(session_id, [])
        if queue:
            return TerminalReadResult(ok=True, data=queue.pop(0))
        deferred = self.deferred_queues.pop(session_id, [])
        if deferred:
            self.queues.setdefault(session_id, []).extend(deferred)
        return TerminalReadResult(ok=True, data=b"")

    async def close_session(self, session_id: str) -> bool:
        self.closed_sessions.append(session_id)
        return True


class GatedPrepareTerminalManager(FakeTerminalManager):
    """Fake terminal manager that pauses the first provider terminal open."""

    def __init__(self) -> None:
        super().__init__()
        self.prepare_started = asyncio.Event()
        self.allow_prepare = asyncio.Event()

    async def prepare_agent_session(self, **kwargs):
        self.prepare_calls.append(kwargs)
        self.prepare_started.set()
        await self.allow_prepare.wait()
        self.session_counter += 1
        session_id = f"terminal-{self.session_counter}"
        self.queues[session_id] = []
        return SimpleNamespace(session_id=session_id)


class ProviderBoundaryTerminal:
    """Provider-operation fake used behind a real TerminalSessionManager."""

    def __init__(self) -> None:
        self.sent_inputs: list[tuple[str, bytes]] = []
        self.closed_sessions: list[str] = []
        self.queues: dict[str, list[bytes]] = {}
        self.deferred_queues: dict[str, list[bytes]] = {}
        self.session_markers: dict[str, tuple[str, str]] = {}
        self.operations: list[str] = []
        self.ctrl_c_writes = 0
        self.cursor = 0

    async def run(self, *, session, operation: str, payload=None, **_kwargs):
        self.operations.append(operation)
        payload = dict(payload or {})
        if operation == "get_runtime_status":
            return SimpleNamespace(ok=True, metadata={"delegate_result": "running"})
        if operation == "open_terminal_session":
            provider_session_id = f"provider-{session.task_id}-{len(self.queues) + 1}"
            self.queues[provider_session_id] = [b"__DROWAI_PROMPT__> "]
            return SimpleNamespace(
                ok=True,
                error_message=None,
                metadata={
                    "delegate_result": {
                        "session_id": provider_session_id,
                        "runtime_job_id": "task-runtime-job",
                    }
                },
            )
        if operation == "send_terminal_input":
            provider_session_id = str(payload.get("session_id") or session.exec_id)
            raw = payload.get("data", b"")
            data = raw.encode() if isinstance(raw, str) else bytes(raw)
            self.sent_inputs.append((provider_session_id, data))
            self._enqueue_shell_output(provider_session_id, data)
            return SimpleNamespace(ok=True, metadata={"delegate_result": {}})
        if operation == "read_terminal_output":
            provider_session_id = str(payload.get("session_id") or session.exec_id)
            queue = self.queues.setdefault(provider_session_id, [])
            data = queue.pop(0) if queue else b""
            if not data:
                deferred = self.deferred_queues.pop(provider_session_id, [])
                if deferred:
                    queue.extend(deferred)
            self.cursor += 1
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "data": data,
                        "next_cursor": self.cursor,
                    }
                },
            )
        if operation == "close_terminal_session":
            provider_session_id = str(payload.get("session_id") or session.exec_id)
            self.closed_sessions.append(provider_session_id)
            return SimpleNamespace(ok=True, metadata={"delegate_result": {}})
        raise AssertionError(operation)

    def _enqueue_shell_output(self, provider_session_id: str, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        if data == b"\x03":
            self.ctrl_c_writes += 1
            self.queues[provider_session_id].append(b"managed interrupted\n")
            return
        if "__DROWAI_CMD_START_" not in text:
            if provider_session_id in self.session_markers and data not in {
                b"export PS1='__DROWAI_PROMPT__> '\n",
                b"cd /workspace 2>/dev/null || true\n",
                b"unset HISTFILE\n",
            }:
                _start, end = self.session_markers[provider_session_id]
                self.queues[provider_session_id].append(
                    b"managed answer:"
                    + data
                    + f"{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            return
        start = text.split("printf '", 1)[1].split("\\n';", 1)[0]
        end = text.split("\\n__DROWAI_CMD_END_", 1)[1].split("=", 1)[0]
        end = f"__DROWAI_CMD_END_{end}"
        self.session_markers[provider_session_id] = (start, end)
        if "printf quick" in text:
            self.queues[provider_session_id].append(
                f"{start}\nquick\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
            )
        elif "delayed-provider" in text:
            self.queues[provider_session_id].append(f"{start}\nstarted\n".encode())
            self.deferred_queues[provider_session_id] = [
                f"done\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
            ]
        elif "oversized-provider" in text:
            self.queues[provider_session_id].append(
                (f"{start}\n" + ("x" * 5000)).encode()
            )
        elif "interactive-provider" in text:
            self.queues[provider_session_id].append(f"{start}\nmanaged waiting\n".encode())


def _provider_bound_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_placement_mode: str = "runner",
):
    manager = TerminalSessionManager()
    provider = ProviderBoundaryTerminal()
    runner_id = "runner-1" if runtime_placement_mode == "runner" else None
    execution_site_id = "site-1" if runtime_placement_mode == "runner" else None
    monkeypatch.setattr(
        manager,
        "_resolve_internal_runtime_context",
        lambda *, task_id, session_name: SimpleNamespace(
            task_id=task_id,
            tenant_id=7,
            user_id=3,
            runtime_placement_mode=runtime_placement_mode,
            workspace_id=f"task-{task_id}",
            workspace_path="/workspace",
            runner_id=runner_id,
            execution_site_id=execution_site_id,
            runtime_call_scope=RuntimeCallScope.PRODUCT_TASK,
        ),
    )

    class _ProviderBackedRuntimeOperations:
        def __init__(self, _db) -> None:
            pass

        def context_for_internal_task(self, **_kwargs):
            return SimpleNamespace(
                task_id=11,
                tenant_id=7,
                user_id=3,
                runtime_placement_mode=runtime_placement_mode,
                workspace_id="workspace-11",
                workspace_path="/workspace",
                runner_id=runner_id,
                execution_site_id=execution_site_id,
                runtime_call_scope=RuntimeCallScope.PRODUCT_TASK,
            )

        async def run_for_context(
            self,
            *,
            context,
            operation: str,
            call,
            payload=None,
            metadata=None,
        ):
            del call, metadata
            return await provider.run(
                session=context,
                operation=operation,
                payload=payload,
            )

    monkeypatch.setattr(
        terminal_manager_module,
        "RuntimeOperationService",
        _ProviderBackedRuntimeOperations,
    )
    monkeypatch.setattr(
        terminal_manager_module,
        "SessionLocal",
        lambda: SimpleNamespace(close=lambda: None),
    )
    service = ShellSessionService(
        terminal_manager=manager,
        config=_config(terminal_io_grace_sec=0),
        runtime_context_resolver=lambda _identity: _context(
            runtime_placement_mode=runtime_placement_mode,
            runner_id=runner_id,
            execution_site_id=execution_site_id,
        ),
    )
    return service, provider


def _identity(**overrides: object) -> ShellSessionIdentity:
    values = {
        "tenant_id": 7,
        "task_id": 11,
        "execution_owner_id": "main:turn-123",
        "runtime_placement_mode": "runner",
        "workspace_id": "workspace-11",
        "workspace_path": "/workspace",
        "runner_id": "runner-1",
        "execution_site_id": "site-1",
    }
    values.update(overrides)
    return ShellSessionIdentity(**values)


def _context(**overrides: object):
    values = {
        "tenant_id": 7,
        "task_id": 11,
        "runtime_placement_mode": "runner",
        "workspace_id": "workspace-11",
        "workspace_path": "/workspace",
        "runner_id": "runner-1",
        "execution_site_id": "site-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**overrides: object) -> ShellSessionServiceConfig:
    values = {
        "max_active_per_owner": 4,
        "max_active_per_task": 16,
        "idle_timeout_sec": 300.0,
        "cleanup_interval_sec": 60.0,
        "termination_grace_sec": 0.0,
        "terminal_io_grace_sec": 0.0,
    }
    values.update(overrides)
    return ShellSessionServiceConfig(**values)


def _service(
    terminal_manager: FakeTerminalManager,
    *,
    context=None,
    config: ShellSessionServiceConfig | None = None,
) -> ShellSessionService:
    return ShellSessionService(
        terminal_manager=terminal_manager,
        config=config or _config(),
        runtime_context_resolver=lambda _identity: context or _context(),
    )


class MutableClock:
    """Monotonic test clock controlled by the test."""

    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.mark.asyncio
async def test_quick_command_returns_completed_update_and_closes_terminal() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.success is True
    assert update.exit_code == 0
    assert update.session_id is None
    assert update.stdout == "quick"
    assert manager.prepare_calls[0]["workspace_path"] == "/workspace"
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_local_host_workspace_is_translated_for_terminal_prepare() -> None:
    manager = FakeTerminalManager()
    host_workspace = "/host/project/agent/workspaces/task-11"
    service = _service(
        manager,
        context=_context(
            runtime_placement_mode="local",
            workspace_path=host_workspace,
            runner_id=None,
            execution_site_id=None,
        ),
    )

    update = await service.execute(
        identity=_identity(
            runtime_placement_mode="local",
            workspace_path=host_workspace,
            runner_id=None,
            execution_site_id=None,
        ),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert manager.prepare_calls[0]["workspace_path"] == "/workspace"


@pytest.mark.asyncio
async def test_live_session_capability_is_owner_bound_and_removed_with_session() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    owner = _identity()

    started = await service.execute(
        identity=owner,
        request=ShellExecRequest(command="interactive", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )

    assert started.session_id is not None
    assert (
        await service.get_session_capability(
            identity=owner,
            public_session_id=started.session_id,
        )
        is ShellCapability.UTILITY
    )
    assert (
        await service.get_session_capability(
            identity=_identity(execution_owner_id="subagent:other"),
            public_session_id=started.session_id,
        )
        is None
    )

    await service.write_stdin(
        identity=owner,
        request=ShellWriteRequest(session_id=started.session_id, chars="\u0003"),
    )
    assert (
        await service.get_session_capability(
            identity=owner,
            public_session_id=started.session_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_quick_command_with_no_stdout_completes_successfully() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="true", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.exit_code == 0
    assert update.success is True
    assert update.session_id is None
    assert update.stdout == ""
    assert update.error_code is None
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_command_echoed_wrapper_does_not_break_completion_parsing() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo echoed", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.success is True
    assert update.exit_code == 0
    assert update.session_id is None
    assert update.stdout == "echoed"
    assert "__DROWAI_CMD_" not in update.stdout
    assert PTY_EXIT_CODE_MARKER not in update.stdout
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_echoed_wrapper_marker_literals_do_not_start_or_complete() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="echo-wrapper-markers-yield",
            yield_time_ms=0,
        ),
    )

    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.session_id is not None
    assert first.stdout == ""
    assert first.error_code is None

    second = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert second.process_status is ShellProcessStatus.COMPLETED
    assert second.stdout == "real output"
    assert second.exit_code == 0
    assert "__DROWAI_CMD_" not in second.stdout


@pytest.mark.asyncio
async def test_incomplete_exit_record_yields_until_structurally_complete() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="incomplete-exit-yields", yield_time_ms=0),
    )

    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.session_id is not None
    assert first.stdout == "partial"
    assert first.error_code is None

    second = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert second.process_status is ShellProcessStatus.COMPLETED
    assert second.exit_code == 0
    assert second.stdout == ""


@pytest.mark.asyncio
async def test_complete_malformed_exit_record_returns_invalid_output_error() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="malformed-complete-exit", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.COMMAND_OUTPUT_INVALID
    assert update.stdout == ""
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_delayed_command_yields_public_session_id_and_later_completes() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.session_id is not None
    assert first.session_id.startswith("shs_")
    assert len(first.session_id.removeprefix("shs_")) >= 16
    assert first.stdin_available is True
    assert first.stdout == "started"
    record = service._records[first.session_id]
    assert record.terminal_session_id == "terminal-1"
    assert not hasattr(record, "socket")
    assert not hasattr(record, "provider_session_id")
    assert not hasattr(record, "command")

    second = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert second.process_status is ShellProcessStatus.COMPLETED
    assert second.session_id is None
    assert second.exit_code == 0
    assert second.stdout == "done"
    assert first.session_id not in service._records


@pytest.mark.asyncio
async def test_local_provider_bound_quick_command_completes_without_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, provider = _provider_bound_service(
        monkeypatch,
        runtime_placement_mode="local",
    )

    update = await service.execute(
        identity=_identity(
            runtime_placement_mode="local",
            runner_id=None,
            execution_site_id=None,
        ),
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.success is True
    assert update.exit_code == 0
    assert update.session_id is None
    assert update.stdout == "quick"
    assert provider.closed_sessions == ["provider-11-1"]


@pytest.mark.asyncio
async def test_local_provider_bound_delayed_command_yields_then_polls_bounded_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, provider = _provider_bound_service(
        monkeypatch,
        runtime_placement_mode="local",
    )
    identity = _identity(
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
    )

    first = await service.execute(
        identity=identity,
        request=ShellExecRequest(
            command="delayed-provider",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )
    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.session_id is not None
    assert first.stdout == "started"
    assert len(first.stdout) <= 1024

    second = await service.write_stdin(
        identity=identity,
        request=ShellWriteRequest(
            session_id=first.session_id,
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert second.process_status is ShellProcessStatus.COMPLETED
    assert second.session_id is None
    assert second.stdout == "done"
    assert provider.closed_sessions == ["provider-11-1"]


@pytest.mark.asyncio
async def test_oversized_output_drains_to_completion_without_extra_poll() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="oversized-completes",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.session_id is None
    assert update.exit_code == 0
    assert update.truncated is True
    assert "complete" in update.stdout
    assert len(update.stdout) <= 1024
    assert service._records == {}
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_provider_buffer_loss_sets_public_truncation_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    original_read = manager.read_output_result

    async def _read_with_loss(*args, **kwargs) -> TerminalReadResult:
        result = await original_read(*args, **kwargs)
        return TerminalReadResult(
            ok=result.ok,
            data=result.data,
            error_code=result.error_code,
            truncated=bool(result.data),
        )

    monkeypatch.setattr(manager, "read_output_result", _read_with_loss)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.truncated is True


@pytest.mark.asyncio
async def test_untruncated_output_remains_exact_above_head_tail_split() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="medium-output",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.truncated is False
    assert update.stdout == "y" * 900


@pytest.mark.asyncio
async def test_split_untruncated_immediate_output_drains_to_completion() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="split-immediate-completes",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.session_id is None
    assert update.exit_code == 0
    assert update.truncated is False
    assert update.stdout == "hello"
    assert service._records == {}
    assert manager.closed_sessions == ["terminal-1"]
    assert manager.queues["terminal-1"] == []


@pytest.mark.asyncio
async def test_protocol_records_and_utf8_output_may_split_at_byte_boundaries() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="byte-split-protocol",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.exit_code == 0
    assert update.stdout == "caf\u00e9"
    assert update.truncated is False
    assert "\ufffd" not in update.stdout


@pytest.mark.asyncio
async def test_invalid_utf8_preserves_valid_suffix_and_completion_marker() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="invalid-utf8-completes",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.exit_code == 0
    assert update.stdout == "before:\ufffd:after"
    assert update.truncated is False


@pytest.mark.asyncio
async def test_managed_provider_contract_matches_local_public_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, provider = _provider_bound_service(monkeypatch)
    identity = _identity()

    quick = await service.execute(
        identity=identity,
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
    )
    delayed = await service.execute(
        identity=identity,
        request=ShellExecRequest(
            command="delayed-provider",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )
    assert delayed.session_id is not None
    completed = await service.write_stdin(
        identity=identity,
        request=ShellWriteRequest(
            session_id=delayed.session_id,
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )
    interactive = await service.execute(
        identity=identity,
        request=ShellExecRequest(
            command="interactive-provider",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )
    assert interactive.session_id is not None
    answered = await service.write_stdin(
        identity=identity,
        request=ShellWriteRequest(
            session_id=interactive.session_id,
            chars="managed input\n",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )
    interruptible = await service.execute(
        identity=identity,
        request=ShellExecRequest(
            command="interactive-provider",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )
    assert interruptible.session_id is not None
    interrupted = await service.write_stdin(
        identity=identity,
        request=ShellWriteRequest(
            session_id=interruptible.session_id,
            chars="\u0003",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert quick.model_dump(include=_PUBLIC_ASSERT_FIELDS) == {
        "success": True,
        "process_status": ShellProcessStatus.COMPLETED,
        "session_id": None,
        "exit_code": 0,
        "stdout": "quick",
        "stdin_available": False,
        "truncated": False,
        "error_code": None,
    }
    assert delayed.process_status is ShellProcessStatus.RUNNING
    assert delayed.session_id is not None
    assert delayed.session_id.startswith("shs_")
    assert delayed.stdout == "started"
    assert completed.process_status is ShellProcessStatus.COMPLETED
    assert completed.session_id is None
    assert completed.stdout == "done"
    assert answered.process_status is ShellProcessStatus.COMPLETED
    assert answered.stdout == "managed answer:managed input"
    assert interrupted.process_status is ShellProcessStatus.TERMINATED
    assert interrupted.session_id is None
    assert interrupted.stdout == "managed interrupted"
    assert provider.ctrl_c_writes == 1
    assert provider.closed_sessions == [
        "provider-11-1",
        "provider-11-2",
        "provider-11-3",
        "provider-11-4",
    ]
    assert "open_terminal_session" in provider.operations
    assert "send_terminal_input" in provider.operations
    assert "read_terminal_output" in provider.operations
    assert "close_terminal_session" in provider.operations


@pytest.mark.asyncio
async def test_local_provider_bound_oversized_delayed_output_has_no_hidden_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _provider = _provider_bound_service(
        monkeypatch,
        runtime_placement_mode="local",
    )
    identity = _identity(
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
    )

    update = await service.execute(
        identity=identity,
        request=ShellExecRequest(
            command="oversized-provider",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.session_id is not None
    assert update.truncated is True
    assert len(update.stdout) <= 1024
    assert "x" * 1100 not in update.stdout
    record = service._records[update.session_id]
    assert not hasattr(record, "transcript")
    assert not hasattr(record, "raw_output")
    assert "x" * 1100 not in repr(record)

    projected = preserve_shell_session_result_fields(
        {"tool": "shell.exec", "status": "success", "success": True},
        raw_result=update.model_dump() | {"tool": "shell.exec"},
        tool_name="shell.exec",
    )
    assert projected["truncated"] is True
    assert "omitted middle content is not preserved" in projected["summary"]


@pytest.mark.asyncio
async def test_split_utf8_boundary_is_preserved_across_yield_and_poll() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="utf8-split", yield_time_ms=0),
    )

    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.session_id is not None
    assert first.stdout == "caf"
    assert "\ufffd" not in first.stdout

    second = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert second.process_status is ShellProcessStatus.COMPLETED
    assert first.stdout + second.stdout == "caf\u00e9"
    assert "\ufffd" not in second.stdout
    assert second.exit_code == 0


@pytest.mark.asyncio
async def test_interactive_command_sends_exact_input_once_including_newline() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="interactive", yield_time_ms=0),
    )

    assert first.session_id is not None
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=first.session_id,
            chars="hello\n",
            yield_time_ms=0,
        ),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.stdout == "answer:hello"
    writes = [
        payload
        for _session_id, payload in manager.sent_inputs
        if payload == b"hello\n"
    ]
    assert writes == [b"hello\n"]


@pytest.mark.asyncio
async def test_foreign_owner_and_task_get_session_unavailable_without_output() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert first.session_id is not None
    foreign = await service.write_stdin(
        identity=_identity(execution_owner_id="subagent:other"),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )
    other_task = await service.write_stdin(
        identity=_identity(task_id=12),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert foreign.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert foreign.stdout == ""
    assert other_task.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert other_task.stdout == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("tenant_id", 8),
        ("task_id", 12),
        ("workspace_id", "other-workspace"),
        ("workspace_path", "/tmp/foreign"),
        ("runtime_placement_mode", "local"),
        ("runner_id", "runner-2"),
        ("execution_site_id", "site-2"),
    ],
)
async def test_runtime_context_mismatch_fails_before_terminal_prepare(
    field: str,
    bad_value: object,
) -> None:
    manager = FakeTerminalManager()
    service = _service(manager, context=_context(**{field: bad_value}))

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.COMMAND_START_FAILED
    assert manager.prepare_calls == []


@pytest.mark.asyncio
async def test_owner_limit_rejects_new_session_without_corrupting_existing() -> None:
    manager = FakeTerminalManager()
    service = _service(manager, config=_config(max_active_per_owner=1))

    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )
    second = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert first.session_id is not None
    assert second.error_code is ShellSessionErrorCode.SESSION_LIMIT_REACHED
    poll = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )
    assert poll.process_status is ShellProcessStatus.COMPLETED
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_concurrent_execute_reserves_capacity_before_terminal_prepare() -> None:
    manager = GatedPrepareTerminalManager()
    service = _service(
        manager,
        config=_config(max_active_per_owner=1, max_active_per_task=1),
    )
    first_task = asyncio.create_task(
        service.execute(
            identity=_identity(),
            request=ShellExecRequest(command="delayed", yield_time_ms=0),
        )
    )
    await asyncio.wait_for(manager.prepare_started.wait(), timeout=1)

    second = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert second.error_code is ShellSessionErrorCode.SESSION_LIMIT_REACHED
    assert len(manager.prepare_calls) == 1
    assert manager.session_counter == 0
    assert service._records == {}

    manager.allow_prepare.set()
    first = await first_task

    assert first.session_id is not None
    assert len(manager.prepare_calls) == 1
    assert manager.session_counter == 1
    assert list(service._records) == [first.session_id]

    poll = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )
    assert poll.process_status is ShellProcessStatus.COMPLETED
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_task_limit_rejects_new_owner_without_corrupting_existing() -> None:
    manager = FakeTerminalManager()
    service = _service(manager, config=_config(max_active_per_task=1))

    first = await service.execute(
        identity=_identity(execution_owner_id="main:first"),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )
    second = await service.execute(
        identity=_identity(execution_owner_id="main:second"),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert first.session_id is not None
    assert second.error_code is ShellSessionErrorCode.SESSION_LIMIT_REACHED
    poll = await service.write_stdin(
        identity=_identity(execution_owner_id="main:first"),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )
    assert poll.process_status is ShellProcessStatus.COMPLETED
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_busy_session_rejects_simultaneous_operation() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )
    assert first.session_id is not None
    service._records[first.session_id].operation_in_progress = True

    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.SESSION_BUSY


@pytest.mark.asyncio
async def test_provider_read_failure_returns_structured_error_and_closes() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    manager.fail_read = True

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED
    assert update.stdout == ""
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_provider_eof_returns_transport_error_and_closes() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    manager.eof_read = True

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=100),
    )

    assert update.error_code is ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED
    assert update.process_status is None
    assert update.session_id is None
    assert update.stdin_available is False
    assert manager.closed_sessions == ["terminal-1"]
    assert manager.read_result_calls == 1


@pytest.mark.asyncio
async def test_ctrl_c_interrupt_returns_terminated_and_closes_record() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="interactive", yield_time_ms=0),
    )

    assert first.session_id is not None
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, chars="\u0003"),
    )

    assert update.success is True
    assert update.process_status is ShellProcessStatus.TERMINATED
    assert update.session_id is None
    assert update.stdout == "interrupted during cleanup"
    assert first.session_id not in service._records
    assert manager.ctrl_c_writes == 1
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_ctrl_c_uses_termination_grace_as_drain_deadline() -> None:
    manager = FakeTerminalManager()
    service = _service(
        manager,
        config=_config(termination_grace_sec=0.01, terminal_io_grace_sec=0.001),
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="quiet-interrupt", yield_time_ms=0),
    )
    assert first.session_id is not None

    started = time.monotonic()
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=first.session_id,
            chars="\u0003",
            yield_time_ms=30_000,
        ),
    )
    elapsed = time.monotonic() - started

    assert update.process_status is ShellProcessStatus.TERMINATED
    assert update.stdout == ""
    assert manager.ctrl_c_writes == 1
    assert manager.closed_sessions == ["terminal-1"]
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_idle_expiry_closes_through_common_cleanup_path() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        config=_config(idle_timeout_sec=1),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert first.session_id is not None
    clock.advance(2)
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_deadline_expiry_poll_returns_timed_out_and_closes() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        config=_config(),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="delayed",
            yield_time_ms=0,
            max_runtime_sec=1,
        ),
    )

    assert first.session_id is not None
    clock.advance(2)
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, yield_time_ms=0),
    )

    assert update.success is False
    assert update.process_status is ShellProcessStatus.TIMED_OUT
    assert update.error_code is ShellSessionErrorCode.COMMAND_TIMED_OUT
    assert update.session_id is None
    assert update.summary == "Command exceeded its configured maximum runtime."
    assert first.session_id not in service._records
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_stale_cleanup_uses_monotonic_idle_expiry() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        config=_config(idle_timeout_sec=1, termination_grace_sec=0),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="delayed",
            yield_time_ms=0,
            max_runtime_sec=300,
        ),
    )

    assert first.session_id is not None
    clock.advance(2)
    await service.cleanup_stale_sessions()

    assert first.session_id not in service._records
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_stale_cleanup_uses_monotonic_hard_runtime_expiry() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        config=_config(idle_timeout_sec=300, termination_grace_sec=0),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="delayed",
            yield_time_ms=0,
            max_runtime_sec=1,
        ),
    )

    assert first.session_id is not None
    clock.advance(2)
    await service.cleanup_stale_sessions()

    assert first.session_id not in service._records
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


def test_backend_config_defaults_feed_service_config() -> None:
    service_config = ShellSessionServiceConfig.from_backend_config()

    assert service_config.max_active_per_owner == (
        backend_config.SHELL_SESSION_MAX_ACTIVE_PER_OWNER
    )
    assert service_config.max_active_per_task == (
        backend_config.SHELL_SESSION_MAX_ACTIVE_PER_TASK
    )
    assert service_config.idle_timeout_sec == (
        backend_config.SHELL_SESSION_IDLE_TIMEOUT_SEC
    )
    assert service_config.cleanup_interval_sec == (
        backend_config.SHELL_SESSION_CLEANUP_INTERVAL_SEC
    )
    assert service_config.termination_grace_sec == (
        backend_config.SHELL_SESSION_TERMINATION_GRACE_SEC
    )
    assert service_config.terminal_io_grace_sec == (
        backend_config.SHELL_SESSION_TERMINAL_IO_GRACE_SEC
    )


@pytest.mark.asyncio
async def test_lifecycle_observability_uses_redacted_events_and_baseline_metrics(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    metrics.counters.clear()
    metrics.gauges.clear()
    monkeypatch.setattr(metrics, "enabled", True)

    with caplog.at_level(
        logging.INFO,
        logger="backend.services.terminal.shell_session_service",
    ):
        update = await service.execute(
            identity=_identity(),
            request=ShellExecRequest(
                command="echo quick",
                cwd="/workspace/host-path-should-not-log",
                env={"TOKEN": "env-secret-should-not-log"},
                yield_time_ms=0,
            ),
        )

    assert update.process_status is ShellProcessStatus.COMPLETED
    log_text = caplog.text
    assert "event=session_opened" in log_text
    assert "event=process_completed" in log_text
    assert "event=session_closed" in log_text
    assert "tenant_id=7" in log_text
    assert "task_id=11" in log_text
    assert "placement=runner" in log_text
    assert "process_status=completed" in log_text
    assert "close_reason=process_completed" in log_text
    assert "main:turn-123" not in log_text
    assert "shs_" not in log_text
    assert "echo quick" not in log_text
    assert "quick" not in log_text
    assert "env-secret-should-not-log" not in log_text
    assert "host-path-should-not-log" not in log_text

    assert metrics.counters["shell_session_starts"] == 1
    assert metrics.counters["shell_session_terminal_outcomes.completed"] == 1
    assert metrics.gauges["shell_session_active_sessions.runner"] == 0.0


@pytest.mark.asyncio
async def test_operation_failure_observability_uses_stable_error_code_only(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeTerminalManager()
    service = _service(manager, context=_context(workspace_path="/workspace/other"))
    metrics.counters.clear()
    metrics.gauges.clear()
    monkeypatch.setattr(metrics, "enabled", True)

    with caplog.at_level(
        logging.INFO,
        logger="backend.services.terminal.shell_session_service",
    ):
        update = await service.execute(
            identity=_identity(),
            request=ShellExecRequest(
                command="leaky-command-should-not-log",
                env={"SECRET": "secret-value-should-not-log"},
                yield_time_ms=0,
            ),
        )

    assert update.error_code is ShellSessionErrorCode.COMMAND_START_FAILED
    log_text = caplog.text
    assert "event=operation_failed" in log_text
    assert "error_code=command_start_failed" in log_text
    assert "placement=runner" in log_text
    assert "leaky-command-should-not-log" not in log_text
    assert "secret-value-should-not-log" not in log_text
    assert "workspace/other" not in log_text
    assert metrics.counters["shell_session_operation_failures.command_start_failed"] == 1


@pytest.mark.asyncio
async def test_write_input_observability_does_not_log_input_contents(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    metrics.counters.clear()
    metrics.gauges.clear()
    monkeypatch.setattr(metrics, "enabled", True)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="interactive", yield_time_ms=0),
    )
    assert first.session_id is not None
    caplog.clear()

    with caplog.at_level(
        logging.INFO,
        logger="backend.services.terminal.shell_session_service",
    ):
        update = await service.write_stdin(
            identity=_identity(),
            request=ShellWriteRequest(
                session_id=first.session_id,
                chars="input-secret-should-not-log\n",
                yield_time_ms=0,
            ),
        )

    assert update.process_status is ShellProcessStatus.COMPLETED
    log_text = caplog.text
    assert "event=process_completed" in log_text
    assert "event=session_closed" in log_text
    assert "input-secret-should-not-log" not in log_text
    assert first.session_id not in log_text
