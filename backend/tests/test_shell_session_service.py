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
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config as backend_config
from backend.database import Base
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Task, User
from backend.services.metrics import metrics
from backend.services.chat.shell_session_lifecycle_projector import (
    ShellSessionLifecycleProjector,
)
from backend.services.chat.transcript_query_service import ChatTranscriptQueryService
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import TurnWorkflowService
from backend.services.runtime_provider import RuntimeCallScope
from backend.services.terminal import manager as terminal_manager_module
from backend.services.terminal.manager import TerminalSessionManager
from backend.services.terminal.shell_session_service import (
    ShellSessionService,
    ShellSessionServiceConfig,
)
from backend.services.terminal.contracts import ShellSessionTerminalEvent
from agent.graph.subgraphs.tool_execution_runtime.result_state_projection import (
    preserve_shell_session_result_fields,
)
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellInteractionBoundary,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionLifecycleStatus,
    ShellSessionOrigin,
    ShellWaitRequest,
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
            elif "quiet-then-done" in text:
                self.deferred_queues[session_id] = [
                    f"{start}\ndone\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
                ]
            elif "bursty-output" in text:
                self.queues[session_id].append(f"{start}\nchunk\n".encode())
            elif "no-output" in text:
                pass
            elif "exit-two" in text:
                self.queues[session_id].append(
                    f"{start}\nfailed\n{end}={PTY_EXIT_CODE_MARKER}2\n".encode()
                )
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


class NoopShellSessionLifecycleProjector:
    """Test projector for shell mechanics tests that must not touch DB or streams."""

    async def project_terminal_event(
        self,
        event: ShellSessionTerminalEvent,
    ) -> None:
        del event


def _noop_lifecycle_projector() -> NoopShellSessionLifecycleProjector:
    return NoopShellSessionLifecycleProjector()


def _real_lifecycle_projector(
    *,
    session_factory,
    hub: RecordingStreamHub,
) -> ShellSessionLifecycleProjector:
    return ShellSessionLifecycleProjector(
        session_factory=session_factory,
        stream_hub_provider=lambda: hub,
        wall_clock=time.time,
    )


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


class BlockingReadTerminalManager(FakeTerminalManager):
    """Block selected reads so public calls can exercise claim contention."""

    def __init__(self) -> None:
        super().__init__()
        self.block_reads = False
        self.blocked_read_started = asyncio.Event()

    async def read_output_result(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ) -> TerminalReadResult:
        if self.block_reads:
            self.blocked_read_started.set()
            await asyncio.Event().wait()
        return await super().read_output_result(
            session_id,
            size,
            timeout=timeout,
        )


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
        self.clock: MutableClock | None = None
        self.advance_empty_reads = False

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
                elif self.advance_empty_reads and self.clock is not None:
                    self.clock.advance(1.25)
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
        elif "exit-two-provider" in text:
            self.queues[provider_session_id].append(
                f"{start}\nfailed\n{end}={PTY_EXIT_CODE_MARKER}2\n".encode()
            )
        elif "no-output-provider" in text:
            pass
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
    clock: MutableClock | None = None,
):
    manager = TerminalSessionManager()
    provider = ProviderBoundaryTerminal()
    provider.clock = clock
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
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(terminal_io_grace_sec=0),
        runtime_context_resolver=lambda _identity: _context(
            runtime_placement_mode=runtime_placement_mode,
            runner_id=runner_id,
            execution_site_id=execution_site_id,
        ),
        clock=clock,
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
        lifecycle_projector=_noop_lifecycle_projector(),
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


def test_prepare_command_terminates_env_option_parsing() -> None:
    request = ShellExecRequest(
        command="printf intended",
        env={"APP_MODE": "test"},
    )

    prepared = ShellSessionService._prepare_command(None, request)

    assert prepared == "env -- APP_MODE=test bash -lc 'printf intended'"


@pytest.mark.asyncio
async def test_service_clamps_process_lifetime_to_configured_tool_maximum() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(tool_timeout_max_sec=60),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )

    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="no-output",
            yield_time_ms=0,
            max_runtime_sec=900,
        ),
    )

    assert started.session_id is not None
    record, _, _, _ = await service._registry.claim(
        identity=_identity(),
        public_session_id=started.session_id,
        now=clock(),
    )
    assert record is not None
    assert record.deadline_at - clock() == 52


class RecordingStreamHub:
    """Test stream hub that records lifecycle packets without subscribers."""

    def __init__(self) -> None:
        self.published: list[tuple[int, dict[str, object]]] = []

    async def publish(self, task_id: int, event: dict[str, object]) -> None:
        self.published.append((task_id, event))


def _build_shell_turn_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_shell_turn(
    session_factory,
    *,
    tenant_id: int,
    turn_id: str,
    conversation_id: str,
) -> tuple[int, int]:
    with session_factory() as db:
        user = User(username=f"shell-cleanup-owner-{turn_id}", password="secret")
        db.add(user)
        db.flush()
        task = Task(user_id=user.id, tenant_id=tenant_id, name=f"shell-cleanup-{turn_id}")
        db.add(task)
        db.flush()
        message = ChatMessage(
            task_id=task.id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_type="assistant",
            message="",
            token_count=0,
            turn_number=1,
        )
        db.add(message)
        db.flush()
        TurnWorkflowService(db).start_turn(
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=1,
            graph_name="simple_tool",
            reserved_message_id=message.id,
        )
        db.commit()
        return int(task.id), int(message.id)


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

    second = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
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

    second = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
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

    second = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
    )

    assert second.process_status is ShellProcessStatus.COMPLETED
    assert second.session_id is None
    assert second.exit_code == 0
    assert second.stdout == "done"
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_default_shell_start_returns_when_output_becomes_available() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    original_read = manager.read_output_result

    async def read_and_advance_after_empty(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if not result.data:
            clock.advance(11.0)
        return result

    manager.read_output_result = read_and_advance_after_empty
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed"),
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.interaction_boundary is ShellInteractionBoundary.OUTPUT_AVAILABLE
    assert update.session_id is not None
    assert update.exit_code is None
    assert update.stdout == "started"
    assert manager.closed_sessions == []


@pytest.mark.asyncio
async def test_default_shell_start_yields_silent_running_session() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    original_read = manager.read_output_result

    async def read_and_advance_after_empty(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if not result.data:
            clock.advance(11.0)
        return result

    manager.read_output_result = read_and_advance_after_empty
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="no-output"),
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.interaction_boundary is ShellInteractionBoundary.QUIET_BOUNDARY
    assert update.session_id is not None
    assert update.stdout == ""


@pytest.mark.asyncio
async def test_output_arrival_returns_one_output_available_delta_after_quiescence() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    original_read = manager.read_output_result

    async def read_and_advance_after_empty(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if not result.data:
            clock.advance(0.2)
        return result

    manager.read_output_result = read_and_advance_after_empty
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(output_quiescence_sec=0.1),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="bursty-output", yield_time_ms=1_000),
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.interaction_boundary is ShellInteractionBoundary.OUTPUT_AVAILABLE
    assert update.stdout == "chunk"
    assert update.session_id is not None


@pytest.mark.asyncio
async def test_explicit_interactive_start_with_no_output_emits_one_quiet_boundary() -> None:
    manager = FakeTerminalManager()
    service = _service(manager, config=_config(initial_quiet_window_sec=0.0))

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="no-output", yield_time_ms=0),
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.interaction_boundary is ShellInteractionBoundary.QUIET_BOUNDARY
    assert update.stdout == ""
    assert update.session_id is not None


@pytest.mark.asyncio
async def test_wait_for_output_does_not_emit_recurring_quiet_boundary() -> None:
    manager = FakeTerminalManager()
    service = _service(manager, config=_config(initial_quiet_window_sec=0.0))
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="quiet-then-done", yield_time_ms=0),
    )

    assert first.session_id is not None
    assert first.interaction_boundary is ShellInteractionBoundary.QUIET_BOUNDARY

    update = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
    )

    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.interaction_boundary is ShellInteractionBoundary.TERMINAL
    assert update.stdout == "done"


@pytest.mark.asyncio
async def test_wait_for_output_returns_active_after_shared_silent_yield_window() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    original_read = manager.read_output_result

    async def read_and_advance_after_empty(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if not result.data:
            clock.advance(10.25)
        return result

    manager.read_output_result = read_and_advance_after_empty
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(initial_quiet_window_sec=0.0),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="no-output", yield_time_ms=0),
    )

    assert first.session_id is not None
    update = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.session_status is ShellSessionLifecycleStatus.ACTIVE
    assert update.interaction_boundary is None
    assert update.session_id == first.session_id
    assert await service.get_session_capability(
        identity=_identity(),
        public_session_id=first.session_id,
    ) is ShellCapability.ASSESSMENT


@pytest.mark.asyncio
async def test_write_stdin_returns_active_session_after_silent_fallback_window() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    original_send = manager.send_input
    original_read = manager.read_output_result
    advance_empty_reads = False

    async def send_without_output_after_start(
        session_id: str,
        data: bytes | str,
    ) -> bool:
        payload = data.encode() if isinstance(data, str) else data
        if b"__DROWAI_CMD_START_" in payload:
            return await original_send(session_id, payload)
        manager.sent_inputs.append((session_id, payload))
        return True

    async def read_and_advance_after_post_input_empty(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if advance_empty_reads and not result.data:
            clock.advance(10.25)
        return result

    manager.send_input = send_without_output_after_start
    manager.read_output_result = read_and_advance_after_post_input_empty
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="no-output",
            yield_time_ms=0,
            max_runtime_sec=60,
        ),
    )
    assert first.session_id is not None
    assert first.interaction_boundary is ShellInteractionBoundary.QUIET_BOUNDARY

    advance_empty_reads = True
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=first.session_id,
            chars="accepted-but-quiet\n",
        ),
    )

    assert update.error_code is None
    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.interaction_boundary is None
    assert update.session_id == first.session_id
    assert ("terminal-1", b"accepted-but-quiet\n") in manager.sent_inputs
    assert manager.closed_sessions == []


@pytest.mark.asyncio
async def test_non_zero_exit_is_failed_terminal_with_exit_code() -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="exit-two", yield_time_ms=0),
    )

    assert update.success is False
    assert update.process_status is ShellProcessStatus.FAILED
    assert update.interaction_boundary is ShellInteractionBoundary.TERMINAL
    assert update.exit_code == 2
    assert update.stderr == "Command exited with code 2."


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

    second = await service.wait_for_output(
        identity=identity,
        request=ShellWaitRequest(
            session_id=first.session_id,
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
async def test_provider_buffer_loss_before_start_returns_framing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeTerminalManager()
    service = _service(manager)

    async def _read_without_start(
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ) -> TerminalReadResult:
        del size, timeout
        _start, end = manager.session_markers[session_id]
        return TerminalReadResult(
            ok=True,
            data=(
                "retained tail\n"
                f"{end}={PTY_EXIT_CODE_MARKER}0\n"
            ).encode(),
            truncated=True,
        )

    monkeypatch.setattr(manager, "read_output_result", _read_without_start)

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="echo quick", yield_time_ms=0),
    )

    assert update.error_code is ShellSessionErrorCode.COMMAND_OUTPUT_INVALID
    assert update.process_status is ShellProcessStatus.FAILED
    assert update.interaction_boundary is ShellInteractionBoundary.TERMINAL
    assert update.session_id is None
    assert manager.closed_sessions == ["terminal-1"]


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
    completed = await service.wait_for_output(
        identity=identity,
        request=ShellWaitRequest(
            session_id=delayed.session_id,
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


def _provider_bound_identity(runtime_placement_mode: str, **overrides: object):
    values: dict[str, object] = {"runtime_placement_mode": runtime_placement_mode}
    if runtime_placement_mode == "local":
        values.update({"runner_id": None, "execution_site_id": None})
    values.update(overrides)
    return _identity(**values)


def _provider_bound_context(runtime_placement_mode: str, **overrides: object):
    values: dict[str, object] = {"runtime_placement_mode": runtime_placement_mode}
    if runtime_placement_mode == "local":
        values.update({"runner_id": None, "execution_site_id": None})
    values.update(overrides)
    return _context(**values)


def _stable_update_contract(update):
    return {
        "success": update.success,
        "status": update.status,
        "process_status": update.process_status,
        "session_status": update.session_status,
        "interaction_boundary": update.interaction_boundary,
        "session_id_present": update.session_id is not None,
        "exit_code": update.exit_code,
        "stdout": update.stdout,
        "stderr": update.stderr,
        "stdin_available": update.stdin_available,
        "truncated": update.truncated,
        "summary": update.summary,
        "error_code": update.error_code,
    }


@pytest.mark.asyncio
async def test_provider_bound_local_and_managed_outcome_matrix_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect(runtime_placement_mode: str):
        clock = MutableClock()
        service, provider = _provider_bound_service(
            monkeypatch,
            runtime_placement_mode=runtime_placement_mode,
            clock=clock,
        )
        identity = _provider_bound_identity(runtime_placement_mode)

        failed = await service.execute(
            identity=identity,
            request=ShellExecRequest(
                command="exit-two-provider",
                yield_time_ms=0,
                max_output_chars=1024,
            ),
        )

        quiet = await service.execute(
            identity=identity,
            request=ShellExecRequest(
                command="no-output-provider",
                yield_time_ms=0,
                max_runtime_sec=1,
                max_output_chars=1024,
            ),
        )
        assert quiet.session_id is not None
        provider.advance_empty_reads = True
        timed_out = await service.wait_for_output(
            identity=identity,
            request=ShellWaitRequest(
                session_id=quiet.session_id,
                max_output_chars=1024,
            ),
        )
        provider.advance_empty_reads = False

        stale_source = await service.execute(
            identity=identity,
            request=ShellExecRequest(
                command="delayed-provider",
                yield_time_ms=0,
                max_output_chars=1024,
            ),
        )
        assert stale_source.session_id is not None
        stale_identity = _provider_bound_identity(
            runtime_placement_mode,
            workspace_id="workspace-11-reassigned",
        )
        service._runtime_context_resolver = lambda _identity: _provider_bound_context(
            runtime_placement_mode,
            workspace_id="workspace-11-reassigned",
        )
        stale_operations_before = len(provider.operations)
        stale = await service.wait_for_output(
            identity=stale_identity,
            request=ShellWaitRequest(session_id=stale_source.session_id),
        )
        stale_operation_delta = len(provider.operations) - stale_operations_before
        service._runtime_context_resolver = lambda _identity: _provider_bound_context(
            runtime_placement_mode
        )
        await service.close_task_sessions(tenant_id=identity.tenant_id, task_id=identity.task_id)

        lost_source = await service.execute(
            identity=identity,
            request=ShellExecRequest(
                command="delayed-provider",
                yield_time_ms=0,
                max_output_chars=1024,
            ),
        )
        assert lost_source.session_id is not None

        def _runtime_lost(_identity):
            raise RuntimeError("runtime unavailable")

        service._runtime_context_resolver = _runtime_lost
        runtime_lost_operations_before = len(provider.operations)
        runtime_lost = await service.wait_for_output(
            identity=identity,
            request=ShellWaitRequest(session_id=lost_source.session_id),
        )
        runtime_lost_operation_delta = (
            len(provider.operations) - runtime_lost_operations_before
        )
        service._runtime_context_resolver = lambda _identity: _provider_bound_context(
            runtime_placement_mode
        )
        await service.close_task_sessions(tenant_id=identity.tenant_id, task_id=identity.task_id)

        unavailable_operations_before = len(provider.operations)
        unavailable = await service.wait_for_output(
            identity=identity,
            request=ShellWaitRequest(session_id="shs_missing_session"),
        )
        unavailable_operation_delta = len(provider.operations) - unavailable_operations_before

        return {
            "updates": {
                "failed": _stable_update_contract(failed),
                "quiet": _stable_update_contract(quiet),
                "timed_out": _stable_update_contract(timed_out),
                "stale": _stable_update_contract(stale),
                "runtime_lost": _stable_update_contract(runtime_lost),
                "unavailable": _stable_update_contract(unavailable),
            },
            "operations": provider.operations,
            "stale_operation_delta": stale_operation_delta,
            "runtime_lost_operation_delta": runtime_lost_operation_delta,
            "unavailable_operation_delta": unavailable_operation_delta,
        }

    managed = await collect("runner")
    local = await collect("local")

    assert managed["updates"] == local["updates"]
    assert managed["updates"]["failed"]["process_status"] is ShellProcessStatus.FAILED
    assert managed["updates"]["failed"]["exit_code"] == 2
    assert managed["updates"]["quiet"]["interaction_boundary"] is (
        ShellInteractionBoundary.QUIET_BOUNDARY
    )
    assert managed["updates"]["timed_out"]["error_code"] is (
        ShellSessionErrorCode.COMMAND_TIMED_OUT
    )
    assert managed["updates"]["stale"]["error_code"] is (
        ShellSessionErrorCode.SESSION_UNAVAILABLE
    )
    assert managed["updates"]["runtime_lost"]["error_code"] is (
        ShellSessionErrorCode.SESSION_UNAVAILABLE
    )
    assert managed["updates"]["unavailable"]["error_code"] is (
        ShellSessionErrorCode.SESSION_UNAVAILABLE
    )
    assert managed["stale_operation_delta"] == local["stale_operation_delta"] == 0
    assert managed["runtime_lost_operation_delta"] == (
        local["runtime_lost_operation_delta"]
    ) == 0
    assert managed["unavailable_operation_delta"] == (
        local["unavailable_operation_delta"]
    ) == 0
    for operations in (managed["operations"], local["operations"]):
        assert "open_terminal_session" in operations
        assert "send_terminal_input" in operations
        assert "read_terminal_output" in operations
        assert "close_terminal_session" in operations


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

    second = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
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
    foreign = await service.wait_for_output(
        identity=_identity(execution_owner_id="subagent:other"),
        request=ShellWaitRequest(session_id=first.session_id),
    )
    other_task = await service.wait_for_output(
        identity=_identity(task_id=12),
        request=ShellWaitRequest(session_id=first.session_id),
    )

    assert foreign.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert foreign.stdout == ""
    assert other_task.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert other_task.stdout == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("continuation_identity", "chars"),
    [
        (_identity(), ""),
        (
            _identity(runner_id="runner-2", execution_site_id="site-2"),
            "whoami\n",
        ),
    ],
)
async def test_runtime_reassignment_rejects_stale_session_without_terminal_io(
    continuation_identity: ShellSessionIdentity,
    chars: str,
) -> None:
    manager = FakeTerminalManager()
    runtime_context = _context()
    service = _service(manager, context=runtime_context)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
    )
    assert first.session_id is not None
    reads_before = manager.read_result_calls
    writes_before = list(manager.sent_inputs)

    runtime_context.runner_id = "runner-2"
    runtime_context.execution_site_id = "site-2"
    if chars:
        update = await service.write_stdin(
            identity=continuation_identity,
            request=ShellWriteRequest(
                session_id=first.session_id,
                chars=chars,
                yield_time_ms=0,
            ),
        )
    else:
        update = await service.wait_for_output(
            identity=continuation_identity,
            request=ShellWaitRequest(session_id=first.session_id),
        )

    assert update.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert update.session_id is None
    assert manager.read_result_calls == reads_before
    assert manager.sent_inputs == writes_before


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
    poll = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
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

    manager.allow_prepare.set()
    first = await first_task

    assert first.session_id is not None
    assert len(manager.prepare_calls) == 1
    assert manager.session_counter == 1
    assert await service.get_session_capability(
        identity=_identity(),
        public_session_id=first.session_id,
    ) is ShellCapability.ASSESSMENT

    poll = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
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
    poll = await service.wait_for_output(
        identity=_identity(execution_owner_id="main:first"),
        request=ShellWaitRequest(session_id=first.session_id),
    )
    assert poll.process_status is ShellProcessStatus.COMPLETED
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_busy_session_rejects_simultaneous_operation() -> None:
    manager = BlockingReadTerminalManager()
    service = _service(manager)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="no-output", yield_time_ms=0),
    )
    assert first.session_id is not None
    manager.block_reads = True
    active_operation = asyncio.create_task(
        service.wait_for_output(
            identity=_identity(),
            request=ShellWaitRequest(session_id=first.session_id),
        )
    )
    await asyncio.wait_for(manager.blocked_read_started.wait(), timeout=1)

    update = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
    )

    assert update.error_code is ShellSessionErrorCode.SESSION_BUSY
    active_operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active_operation


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
    assert update.process_status is ShellProcessStatus.FAILED
    assert update.interaction_boundary is ShellInteractionBoundary.TERMINAL
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
async def test_initial_quiet_boundary_keeps_session_active_before_idle_expiry() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(idle_timeout_sec=1),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="no-output",
            yield_time_ms=0,
            max_runtime_sec=10,
        ),
    )

    assert first.session_id is not None
    assert first.interaction_boundary is ShellInteractionBoundary.QUIET_BOUNDARY
    clock.advance(0.75)
    await service.cleanup_stale_sessions()
    assert await service.get_session_capability(
        identity=_identity(),
        public_session_id=first.session_id,
    ) is ShellCapability.ASSESSMENT
    assert manager.closed_sessions == []


@pytest.mark.asyncio
async def test_stale_cleanup_does_not_interrupt_claimed_silent_wait() -> None:
    """Idle cleanup skips a session while a coordinator actively waits on it."""

    class _GatedReadTerminalManager(FakeTerminalManager):
        def __init__(self) -> None:
            super().__init__()
            self.gate_reads = False
            self.read_started = asyncio.Event()
            self.allow_read = asyncio.Event()

        async def read_output_result(
            self,
            session_id: str,
            size: int = 4096,
            *,
            timeout: float | None = None,
        ) -> TerminalReadResult:
            if not self.gate_reads:
                return await super().read_output_result(
                    session_id,
                    size,
                    timeout=timeout,
                )
            self.read_started.set()
            await self.allow_read.wait()
            start, end = self.session_markers[session_id]
            return TerminalReadResult(
                ok=True,
                data=f"{start}\ndone\n{end}={PTY_EXIT_CODE_MARKER}0\n".encode(),
            )

    manager = _GatedReadTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(idle_timeout_sec=1),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="no-output",
            yield_time_ms=0,
            max_runtime_sec=10,
        ),
    )
    assert first.session_id is not None

    manager.gate_reads = True
    wait_task = asyncio.create_task(
        service.wait_for_output(
            identity=_identity(),
            request=ShellWaitRequest(session_id=first.session_id),
        )
    )
    await asyncio.wait_for(manager.read_started.wait(), timeout=1)
    clock.advance(2)

    await service.cleanup_stale_sessions()

    assert manager.closed_sessions == []
    assert await service.get_session_capability(
        identity=_identity(),
        public_session_id=first.session_id,
    ) is ShellCapability.ASSESSMENT

    manager.allow_read.set()
    completed = await asyncio.wait_for(wait_task, timeout=1)
    assert completed.process_status is ShellProcessStatus.COMPLETED
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_quiet_boundary_does_not_extend_hard_runtime_deadline() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
        config=_config(idle_timeout_sec=1),
        runtime_context_resolver=lambda _identity: _context(),
        clock=clock,
    )
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="no-output",
            yield_time_ms=0,
            max_runtime_sec=1,
        ),
    )

    assert first.session_id is not None
    assert first.interaction_boundary is ShellInteractionBoundary.QUIET_BOUNDARY

    clock.advance(1.25)
    timed_out = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
    )
    assert timed_out.error_code is ShellSessionErrorCode.COMMAND_TIMED_OUT
    assert timed_out.process_status is ShellProcessStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_idle_expiry_closes_through_common_cleanup_path() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
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
    update = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
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
        lifecycle_projector=_noop_lifecycle_projector(),
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
        capability=ShellCapability.ASSESSMENT,
    )

    assert first.session_id is not None
    clock.advance(2)
    assert (
        await service.get_session_capability(
            identity=_identity(),
            public_session_id=first.session_id,
        )
        is ShellCapability.ASSESSMENT
    )
    update = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=first.session_id),
    )

    assert update.success is False
    assert update.process_status is ShellProcessStatus.TIMED_OUT
    assert update.error_code is ShellSessionErrorCode.COMMAND_TIMED_OUT
    assert update.session_id is None
    assert update.summary == "Command exceeded its configured maximum runtime."
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]
    assert (
        await service.get_session_capability(
            identity=_identity(),
            public_session_id=first.session_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_stale_cleanup_uses_monotonic_idle_expiry() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
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

    assert await service.get_session_capability(
        identity=_identity(),
        public_session_id=first.session_id,
    ) is None
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_stale_cleanup_uses_monotonic_hard_runtime_expiry() -> None:
    manager = FakeTerminalManager()
    clock = MutableClock()
    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_noop_lifecycle_projector(),
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

    assert await service.get_session_capability(
        identity=_identity(),
        public_session_id=first.session_id,
    ) is None
    assert b"\x03" in [payload for _session, payload in manager.sent_inputs]
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_task_cleanup_persists_canonical_terminal_turn_event(monkeypatch) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "task-cleanup-turn-1"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-shell-cleanup",
    )
    hub = RecordingStreamHub()

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )
    try:
        manager = FakeTerminalManager()
        service = ShellSessionService(
            terminal_manager=manager,
            lifecycle_projector=_real_lifecycle_projector(
                session_factory=session_factory,
                hub=hub,
            ),
            config=_config(termination_grace_sec=0),
            runtime_context_resolver=lambda _identity: _context(
                tenant_id=tenant_id,
                task_id=task_id,
            ),
        )
        first = await service.execute(
            identity=_identity(
                tenant_id=tenant_id,
                task_id=task_id,
                execution_owner_id=f"main:{turn_id}",
            ),
            request=ShellExecRequest(command="delayed", yield_time_ms=0),
        )

        assert first.session_id is not None
        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        with session_factory() as db:
            rows = db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

        assert len(rows) == 1
        tool_call_id = f"shell-session-{first.session_id}"
        assert rows[0].tool_call_id == tool_call_id
        assert rows[0].content == "Shell session closed"
        assert rows[0].event_metadata["lifecycle_event"] == "shell_session_terminal"
        assert rows[0].event_metadata["close_reason"] == "task_cleanup"
        assert rows[0].event_metadata["session_id"] == first.session_id
        assert rows[0].event_metadata["session_status"] == "closed"
        assert rows[0].event_metadata["process_status"] == "terminated"
        assert rows[0].event_metadata["interaction_boundary"] == "terminal"
        assert rows[0].event_metadata["compact_tool_result"]["summary"] == (
            "Shell session closed"
        )

        assert len(hub.published) == 1
        published_task_id, event = hub.published[0]
        assert published_task_id == task_id
        metadata = event["metadata"]
        assert isinstance(metadata, dict)
        assert event["type"] == "tool_end"
        assert event["content"] == "Shell session closed"
        assert metadata["tool_call_id"] == tool_call_id
        assert metadata["lifecycle_event"] == "shell_session_terminal"
        assert metadata["close_reason"] == "task_cleanup"
        assert metadata["session_id"] == first.session_id
        assert metadata["status"] == "cancelled"
        assert metadata["session_status"] == "closed"
        assert metadata["process_status"] == "terminated"
        assert metadata["interaction_boundary"] == "terminal"
        assert metadata["output_persistence"] == "transient"
        assert "chars" not in repr(event)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_terminal_event_replaces_originating_shell_card(monkeypatch) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "task-cleanup-originating-turn-1"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-shell-cleanup-originating",
    )
    hub = RecordingStreamHub()

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )
    try:
        manager = FakeTerminalManager()
        service = ShellSessionService(
            terminal_manager=manager,
            lifecycle_projector=_real_lifecycle_projector(
                session_factory=session_factory,
                hub=hub,
            ),
            config=_config(termination_grace_sec=0),
            runtime_context_resolver=lambda _identity: _context(
                tenant_id=tenant_id,
                task_id=task_id,
            ),
        )
        first = await service.execute(
            identity=_identity(
                tenant_id=tenant_id,
                task_id=task_id,
                execution_owner_id=f"main:{turn_id}",
            ),
            request=ShellExecRequest(command="delayed", yield_time_ms=0),
            capability=ShellCapability.UTILITY,
        )

        assert first.session_id is not None
        with session_factory() as db:
            db.add(
                ChatTurnEvent(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    conversation_id="conv-shell-cleanup-originating",
                    chat_message_id=message_id,
                    turn_number=1,
                    phase_sequence=0,
                    kind="tool",
                    tool_call_id="call-shell-origin",
                    content="Session active",
                    event_metadata={
                        "tool_name": "shell.utility",
                        "tool_call_id": "call-shell-origin",
                        "status": "success",
                        "process_status": "running",
                        "session_status": "active",
                        "interaction_boundary": "output_available",
                        "session_id": first.session_id,
                        "output_persistence": "transient",
                    },
                )
            )
            db.commit()

        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        with session_factory() as db:
            rows = db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

            page = ChatTranscriptQueryService(db).list_latest_transcript_page(
                task_id=task_id,
                requested_conversation_id="conv-shell-cleanup-originating",
                limit=10,
            )

        assert len(rows) == 1
        assert rows[0].tool_call_id == "call-shell-origin"
        assert rows[0].content == "Shell session closed"
        metadata = rows[0].event_metadata
        assert metadata["tool_call_id"] == "call-shell-origin"
        assert metadata["tool_name"] == "shell.utility"
        assert metadata["lifecycle_event"] == "shell_session_terminal"
        assert metadata["close_reason"] == "task_cleanup"
        assert metadata["session_id"] == first.session_id
        assert metadata["status"] == "cancelled"
        assert metadata["process_status"] == "terminated"
        assert metadata["session_status"] == "closed"
        assert metadata["interaction_boundary"] == "terminal"
        assert metadata["output_persistence"] == "transient"
        assert metadata["compact_tool_result"]["session_status"] == "closed"
        assert metadata["compact_tool_result"]["process_status"] == "terminated"
        assert not any(
            str(row.tool_call_id or "").startswith("shell-session-") for row in rows
        )

        tool_items = [item for item in page.items if item.kind == "tool"]
        assert len(tool_items) == 1
        assert tool_items[0].metadata["tool_call_id"] == "call-shell-origin"
        assert tool_items[0].metadata["process_status"] == "terminated"
        assert tool_items[0].metadata["session_status"] == "closed"
        assert tool_items[0].metadata["interaction_boundary"] == "terminal"
        assert tool_items[0].metadata["output_persistence"] == "transient"

        assert len(hub.published) == 1
        published_task_id, event = hub.published[0]
        assert published_task_id == task_id
        live_metadata = event["metadata"]
        assert live_metadata["tool_call_id"] == "call-shell-origin"
        assert live_metadata["tool_name"] == "shell.utility"
        assert live_metadata["process_status"] == "terminated"
        assert live_metadata["session_status"] == "closed"
        assert live_metadata["interaction_boundary"] == "terminal"
        assert live_metadata["output_persistence"] == "transient"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_terminal_event_uses_session_origin_without_existing_row(
    monkeypatch,
) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "task-cleanup-origin-before-row-turn-1"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-shell-cleanup-origin-before-row",
    )
    hub = RecordingStreamHub()

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )
    try:
        manager = FakeTerminalManager()
        service = ShellSessionService(
            terminal_manager=manager,
            lifecycle_projector=_real_lifecycle_projector(
                session_factory=session_factory,
                hub=hub,
            ),
            config=_config(termination_grace_sec=0),
            runtime_context_resolver=lambda _identity: _context(
                tenant_id=tenant_id,
                task_id=task_id,
            ),
        )
        first = await service.execute(
            identity=_identity(
                tenant_id=tenant_id,
                task_id=task_id,
                execution_owner_id=f"main:{turn_id}",
            ),
            request=ShellExecRequest(command="delayed", yield_time_ms=0),
            capability=ShellCapability.UTILITY,
            origin=ShellSessionOrigin(
                tool_call_id="call-shell-origin-before-row",
                tool_batch_id="batch-shell-origin-before-row",
                tool_name="shell.utility",
            ),
        )

        assert first.session_id is not None
        await service.close_task_sessions(tenant_id=tenant_id, task_id=task_id)

        with session_factory() as db:
            rows = db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

        assert len(rows) == 1
        metadata = rows[0].event_metadata
        assert rows[0].tool_call_id == "call-shell-origin-before-row"
        assert metadata["tool_call_id"] == "call-shell-origin-before-row"
        assert metadata["tool_batch_id"] == "batch-shell-origin-before-row"
        assert metadata["tool_name"] == "shell.utility"
        assert metadata["session_id"] == first.session_id
        assert metadata["lifecycle_event"] == "shell_session_terminal"
        assert not str(rows[0].tool_call_id or "").startswith("shell-session-")

        assert len(hub.published) == 1
        live_metadata = hub.published[0][1]["metadata"]
        assert live_metadata["tool_call_id"] == "call-shell-origin-before-row"
        assert live_metadata["tool_batch_id"] == "batch-shell-origin-before-row"
        assert live_metadata["tool_name"] == "shell.utility"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_idle_cleanup_persists_and_publishes_terminal_turn_event(monkeypatch) -> None:
    engine, session_factory = _build_shell_turn_session()
    tenant_id = 1
    turn_id = "idle-cleanup-turn-1"
    task_id, message_id = _seed_shell_turn(
        session_factory,
        tenant_id=tenant_id,
        turn_id=turn_id,
        conversation_id="conv-shell-idle-cleanup",
    )
    hub = RecordingStreamHub()

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )
    try:
        manager = FakeTerminalManager()
        clock = MutableClock()
        service = ShellSessionService(
            terminal_manager=manager,
            lifecycle_projector=_real_lifecycle_projector(
                session_factory=session_factory,
                hub=hub,
            ),
            config=_config(idle_timeout_sec=1, termination_grace_sec=0),
            runtime_context_resolver=lambda _identity: _context(
                tenant_id=tenant_id,
                task_id=task_id,
            ),
            clock=clock,
        )
        first = await service.execute(
            identity=_identity(
                tenant_id=tenant_id,
                task_id=task_id,
                execution_owner_id=f"main:{turn_id}",
            ),
            request=ShellExecRequest(
                command="no-output",
                yield_time_ms=0,
                max_runtime_sec=10,
            ),
        )

        assert first.session_id is not None
        clock.advance(2)
        await service.cleanup_stale_sessions()

        with session_factory() as db:
            rows = db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

        assert len(rows) == 1
        assert rows[0].event_metadata["close_reason"] == "idle_expired"
        assert rows[0].event_metadata["session_id"] == first.session_id
        assert rows[0].event_metadata["process_status"] == "terminated"
        assert len(hub.published) == 1
        published_task_id, event = hub.published[0]
        assert published_task_id == task_id
        metadata = event["metadata"]
        assert isinstance(metadata, dict)
        assert metadata["close_reason"] == "idle_expired"
        assert metadata["session_id"] == first.session_id
        assert metadata["process_status"] == "terminated"
        assert metadata["session_status"] == "closed"
        assert metadata["interaction_boundary"] == "terminal"
        assert metadata["tool_call_id"] == f"shell-session-{first.session_id}"
        assert "no-output" not in repr(event)
    finally:
        engine.dispose()


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
    assert service_config.tool_timeout_max_sec == (
        backend_config.TOOL_TIMEOUT_MAX_SECONDS
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
