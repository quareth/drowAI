"""Tests for runner-owned dedicated shell command orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.terminal.contracts import ShellSessionTerminalEvent
from backend.services.terminal.shell_session_service import (
    ShellSessionService,
    ShellSessionServiceConfig,
)
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellInteractionBoundary,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionLifecycleStatus,
    ShellWaitRequest,
    ShellWriteRequest,
)
from runtime_shared.terminal_contracts import TerminalReadResult


class _NoopProjector:
    async def project_terminal_event(self, event: ShellSessionTerminalEvent) -> None:
        del event


class _CommandTerminalManager:
    """Dedicated-command fake with explicit output and exit events."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.sent_inputs: list[tuple[str, bytes]] = []
        self.closed_sessions: list[str] = []
        self.reads: dict[str, list[TerminalReadResult]] = {}
        self.counter = 0
        self.fail_read = False

    async def create_agent_command_session(self, **kwargs):
        self.create_calls.append(dict(kwargs))
        self.counter += 1
        session_id = f"terminal-{self.counter}"
        command = str(kwargs.get("command") or "")
        if "delayed" in command:
            results = [
                TerminalReadResult(ok=True, data=b"Kali banner\nstarted\n"),
                TerminalReadResult(
                    ok=True,
                    data=b"done\n",
                    process_status="running",
                ),
                TerminalReadResult(
                    ok=True,
                    eof=True,
                    process_status="completed",
                    exit_code=0,
                ),
            ]
        elif "interactive-command" in command:
            results = [
                TerminalReadResult(ok=True, data=b"waiting\n"),
            ]
        elif "exit 2" in command:
            results = [
                TerminalReadResult(ok=True, data=b"failed\n"),
                TerminalReadResult(
                    ok=True,
                    eof=True,
                    process_status="failed",
                    exit_code=2,
                ),
            ]
        elif "large-output" in command:
            results = [
                TerminalReadResult(ok=True, data=b"x" * 5000),
                TerminalReadResult(
                    ok=True,
                    eof=True,
                    process_status="completed",
                    exit_code=0,
                ),
            ]
        elif "silent-running" in command:
            results = [TerminalReadResult(ok=True)]
        else:
            results = [
                TerminalReadResult(ok=True, data=b"quick\n"),
                TerminalReadResult(
                    ok=True,
                    eof=True,
                    process_status="completed",
                    exit_code=0,
                ),
            ]
        self.reads[session_id] = results
        return SimpleNamespace(session_id=session_id)

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        payload = data.encode() if isinstance(data, str) else data
        self.sent_inputs.append((session_id, payload))
        if payload == b"\x03":
            self.reads.setdefault(session_id, []).extend(
                [
                    TerminalReadResult(ok=True, data=b"interrupted\n"),
                    TerminalReadResult(
                        ok=True,
                        eof=True,
                        process_status="failed",
                        exit_code=130,
                    ),
                ]
            )
        else:
            self.reads.setdefault(session_id, []).extend(
                [
                    TerminalReadResult(ok=True, data=b"answer:" + payload),
                    TerminalReadResult(
                        ok=True,
                        eof=True,
                        process_status="completed",
                        exit_code=0,
                    ),
                ]
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
        results = self.reads.setdefault(session_id, [])
        return results.pop(0) if results else TerminalReadResult(ok=True)

    async def close_session(self, session_id: str) -> bool:
        self.closed_sessions.append(session_id)
        return True


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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
        "initial_quiet_window_sec": 0.0,
        "output_quiescence_sec": 0.0,
    }
    values.update(overrides)
    return ShellSessionServiceConfig(**values)


def _service(
    manager: _CommandTerminalManager,
    *,
    clock=None,
    artifact_exists=None,
    context=None,
) -> ShellSessionService:
    return ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_NoopProjector(),
        config=_config(),
        runtime_context_resolver=lambda _identity: context or _context(),
        artifact_exists_resolver=artifact_exists,
        clock=clock,
    )


async def _drain_terminal(
    service: ShellSessionService,
    update,
):
    current = update
    while current.process_status is ShellProcessStatus.RUNNING:
        current = await service.wait_for_output(
            identity=_identity(),
            request=ShellWaitRequest(session_id=str(current.session_id)),
        )
    return current


@pytest.mark.asyncio
async def test_utility_starts_one_dedicated_command_without_input_or_artifact() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="printf quick",
            cwd="results",
            env={"APP_MODE": "test"},
            yield_time_ms=0,
        ),
        capability=ShellCapability.UTILITY,
    )

    assert update.stdout == "quick\n"
    update = await _drain_terminal(service, update)
    assert update.process_status is ShellProcessStatus.COMPLETED
    assert update.exit_code == 0
    assert update.stdout == ""
    assert update.artifacts == []
    assert manager.sent_inputs == []
    assert len(manager.create_calls) == 1
    assert manager.create_calls[0]["command"] == "printf quick"
    assert manager.create_calls[0]["cwd"] == "/workspace/results"
    assert manager.create_calls[0]["env"] == {"APP_MODE": "test"}
    assert manager.create_calls[0]["interactive"] is False


@pytest.mark.asyncio
async def test_assessment_capture_is_kali_tee_and_exposed_only_after_confirmation() -> None:
    manager = _CommandTerminalManager()
    checked: list[str] = []

    async def _exists(_identity: ShellSessionIdentity, path: str) -> bool:
        checked.append(path)
        return True

    service = _service(manager, artifact_exists=_exists)
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
        capability=ShellCapability.ASSESSMENT,
    )

    update = await _drain_terminal(service, update)
    command = str(manager.create_calls[0]["command"])
    assert "tee -- /workspace/artifacts/" in command
    assert ".incomplete" in command
    assert "PIPESTATUS" in command
    assert "bash -c " in command
    assert "bash -lc " not in command
    assert "script " not in command
    assert "__DROWAI_CMD_" not in command
    assert update.artifacts == checked
    assert len(update.artifacts) == 1


@pytest.mark.asyncio
async def test_assessment_confirmation_failure_has_no_fallback_artifact() -> None:
    manager = _CommandTerminalManager()
    service = _service(
        manager,
        artifact_exists=lambda _identity, _path: False,
    )
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
        capability=ShellCapability.ASSESSMENT,
    )

    update = await _drain_terminal(service, update)
    assert update.success is True
    assert update.artifacts == []


@pytest.mark.asyncio
async def test_banner_and_early_output_do_not_repeat_original_command() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    first = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="delayed", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.interaction_boundary is ShellInteractionBoundary.OUTPUT_AVAILABLE
    assert first.stdin_available is False

    terminal = await _drain_terminal(service, first)
    assert terminal.process_status is ShellProcessStatus.COMPLETED
    assert first.stdout == "Kali banner\nstarted\n"
    assert len(manager.create_calls) == 1
    assert manager.sent_inputs == []


@pytest.mark.asyncio
async def test_noninteractive_session_rejects_regular_stdin_without_sending() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="silent-running", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    update = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=str(started.session_id), chars="again\n"),
    )
    assert update.error_code is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert manager.sent_inputs == []


@pytest.mark.asyncio
async def test_explicit_interactive_session_accepts_exact_input_and_completes() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="interactive-command",
            interactive=True,
            yield_time_ms=0,
        ),
        capability=ShellCapability.UTILITY,
    )
    assert started.process_status is ShellProcessStatus.RUNNING
    assert started.stdin_available is True

    progress = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=str(started.session_id), chars="hello\n"),
    )
    assert manager.sent_inputs == [("terminal-1", b"hello\n")]
    assert progress.stdout == "answer:hello\n"
    terminal = await _drain_terminal(service, progress)
    assert terminal.process_status is ShellProcessStatus.COMPLETED


@pytest.mark.asyncio
async def test_nonzero_exec_exit_is_command_failure_not_transport_failure() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="exit 2", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    update = await _drain_terminal(service, update)
    assert update.success is False
    assert update.process_status is ShellProcessStatus.FAILED
    assert update.exit_code == 2
    assert update.error_code is None


@pytest.mark.asyncio
async def test_provider_read_failure_closes_with_transport_error() -> None:
    manager = _CommandTerminalManager()
    manager.fail_read = True
    service = _service(manager)
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="silent-running", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    assert update.error_code is ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED
    assert manager.closed_sessions == ["terminal-1"]


@pytest.mark.asyncio
async def test_interrupt_maps_exec_exit_to_terminated() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="silent-running", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    terminal = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id=str(started.session_id), chars="\u0003"),
    )
    assert terminal.process_status is ShellProcessStatus.TERMINATED
    assert terminal.session_status is ShellSessionLifecycleStatus.CLOSED
    assert manager.sent_inputs == [("terminal-1", b"\x03")]


@pytest.mark.asyncio
async def test_hard_deadline_terminates_a_running_exec() -> None:
    manager = _CommandTerminalManager()
    clock = _Clock()
    service = _service(manager, clock=clock)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="silent-running",
            yield_time_ms=0,
            max_runtime_sec=1,
        ),
        capability=ShellCapability.UTILITY,
    )
    clock.advance(2)
    terminal = await service.wait_for_output(
        identity=_identity(),
        request=ShellWaitRequest(session_id=str(started.session_id)),
    )
    assert terminal.process_status is ShellProcessStatus.TIMED_OUT
    assert manager.sent_inputs == [("terminal-1", b"\x03")]


@pytest.mark.asyncio
async def test_output_projection_remains_bounded_without_lifecycle_parsing() -> None:
    manager = _CommandTerminalManager()
    service = _service(manager)
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="large-output",
            yield_time_ms=0,
            max_output_chars=1024,
        ),
        capability=ShellCapability.UTILITY,
    )
    assert update.truncated is True
    assert len(update.stdout) <= 1024
    assert "shell output truncated" in update.stdout
    terminal = await _drain_terminal(service, update)
    assert terminal.process_status is ShellProcessStatus.COMPLETED


@pytest.mark.asyncio
async def test_runtime_identity_mismatch_fails_before_exec_creation() -> None:
    manager = _CommandTerminalManager()
    update = await _service(
        manager,
        context=_context(runner_id="different-runner"),
    ).execute(
        identity=_identity(),
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )
    assert update.error_code is ShellSessionErrorCode.COMMAND_START_FAILED
    assert manager.create_calls == []
