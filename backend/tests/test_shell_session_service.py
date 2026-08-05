"""Tests for the terminal-backed shell session service.

These tests use a fake terminal manager to prove session lifecycle, ownership,
limits, and PTY result mapping without opening a real provider terminal.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from backend import config as backend_config
from backend.services.metrics import metrics
from backend.services.runtime_provider import RuntimeCallScope
from backend.services.terminal.manager import TerminalSession, TerminalSessionManager
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
from runtime_shared.terminal_contracts import TerminalReadResult


class FakeTerminalManager:
    """Small fake for TerminalSessionManager shell-session I/O methods."""

    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self.sent_inputs: list[tuple[str, bytes]] = []
        self.closed_sessions: list[str] = []
        self.queues: dict[str, list[bytes]] = {}
        self.session_counter = 0
        self.session_markers: dict[str, tuple[str, str]] = {}
        self.fail_read = False

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
            elif "delayed" in text:
                self.queues[session_id].append(f"{start}\nstarted\n".encode())
                self.queues[session_id].append(
                    f"done\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "utf8-split" in text:
                utf8_bytes = "caf\u00e9".encode()
                self.queues[session_id].append(f"{start}\n".encode() + utf8_bytes[:-1])
                self.queues[session_id].append(
                    utf8_bytes[-1:] + f"\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
            elif "interactive" in text:
                self.queues[session_id].append(f"{start}\nwaiting\n".encode())
            else:
                self.queues[session_id].append(
                    f"{start}\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                )
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
        if self.fail_read:
            return TerminalReadResult(ok=False, error_code="provider_lost")
        queue = self.queues.get(session_id, [])
        if queue:
            return TerminalReadResult(ok=True, data=queue.pop(0))
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
        self.cursor = 0

    async def run(self, *, session, operation: str, payload=None, **_kwargs):
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
        if "__DROWAI_CMD_START_" not in text:
            return
        start = text.split("printf '", 1)[1].split("\\n';", 1)[0]
        end = text.split("\\n__DROWAI_CMD_END_", 1)[1].split("=", 1)[0]
        end = f"__DROWAI_CMD_END_{end}"
        if "printf quick" in text:
            self.queues[provider_session_id].append(
                f"{start}\nquick\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
            )
        elif "delayed-provider" in text:
            self.queues[provider_session_id].append(f"{start}\nstarted\n".encode())
            self.queues[provider_session_id].append(
                f"done\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
            )
        elif "oversized-provider" in text:
            self.queues[provider_session_id].append(
                (f"{start}\n" + ("x" * 5000)).encode()
            )


def _provider_bound_service(monkeypatch: pytest.MonkeyPatch):
    manager = TerminalSessionManager()
    provider = ProviderBoundaryTerminal()
    monkeypatch.setattr(
        manager,
        "_resolve_internal_runtime_context",
        lambda *, task_id, session_name: SimpleNamespace(
            task_id=task_id,
            tenant_id=7,
            user_id=3,
            runtime_placement_mode="local",
            workspace_id=f"task-{task_id}",
            workspace_path="/workspace",
            runner_id=None,
            execution_site_id=None,
            runtime_call_scope=RuntimeCallScope.PRODUCT_TASK,
        ),
    )
    async def _create_agent_session(task_id: int, cols: int, rows: int, session_name=None):
        del cols, rows
        provider_session_id = f"provider-{task_id}-1"
        provider.queues[provider_session_id] = [b"__DROWAI_PROMPT__> "]
        session = TerminalSession(
            session_id=manager._build_agent_session_id(
                task_id,
                session_name=session_name,
            ),
            task_id=task_id,
            user_id=3,
            container_name=f"drowai-task-{task_id}",
            connection_type="docker_exec",
            exec_id=provider_session_id,
            runtime_job_id="task-runtime-job",
            runtime_call_scope=RuntimeCallScope.PRODUCT_TASK.value,
            session_type="agent",
        )
        manager.sessions[session.session_id] = session
        return session

    monkeypatch.setattr(manager, "_create_agent_session", _create_agent_session)
    monkeypatch.setattr(manager, "_run_session_provider_operation", provider.run)
    service = ShellSessionService(
        terminal_manager=manager,
        config=_config(terminal_io_grace_sec=0),
        runtime_context_resolver=lambda _identity: _context(
            runtime_placement_mode="local",
            runner_id=None,
            execution_site_id=None,
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
    service, provider = _provider_bound_service(monkeypatch)

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
    service, provider = _provider_bound_service(monkeypatch)
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
async def test_local_provider_bound_oversized_delayed_output_has_no_hidden_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _provider = _provider_bound_service(monkeypatch)
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
async def test_ctrl_c_interrupt_returns_terminated_and_closes_record() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )

    assert first.session_id is not None
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=first.session_id, chars="\u0003"),
    )

    assert update.success is True
    assert update.process_status is ShellProcessStatus.TERMINATED
    assert first.session_id not in service._records
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


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
