"""Tests for runner operation-service handling of transport-neutral tool commands."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

from drowai_runner.config import RunnerConfig
from drowai_runner.file_comm_bridge import ERROR_FILE_COMM_TIMEOUT
from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.tool_command_models import RunnerToolCommandResult
from drowai_runner.workspace import RunnerWorkspaceManager


def _build_service(tmp_path: Path) -> RunnerOperationService:
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_ROOT": str(tmp_path / "runner-root"),
            "DROWAI_RUNNER_CLOUD_BASE_URL": "http://cloud.example.test",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
        }
    )
    workspace = RunnerWorkspaceManager(config.runner_root)
    workspace.initialize_runner_root()
    workspace.initialize_task_workspace("task-91")
    store = initialize_runner_job_store(config.runner_root / "jobs.sqlite")
    store.start_job(
        runtime_job_id="task-runtime-91",
        tenant_id="7",
        task_id="91",
        workspace_id="task-91",
        image="runtime:test",
    )
    return RunnerOperationService(
        config=config,
        workspace=workspace,
        job_store=store,
        docker_runtime=Mock(),
        logs_metrics=Mock(),
        terminal_proxy=Mock(),
        cleanup=Mock(),
    )


def test_dispatch_tool_command_passes_timeout_to_file_comm_bridge(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured: dict[str, object] = {}

    class _FakeBridge:
        async def dispatch_command(
            self,
            *,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
        ) -> RunnerToolCommandResult:
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            captured["timeout_policy"] = timeout_policy
            captured["timeout_seconds"] = timeout_seconds
            captured["command_id"] = command_id
            return RunnerToolCommandResult(
                command_id=command_id,
                status="completed",
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                transport="file-comm",
            )

    bridge = _FakeBridge()

    def _get_bridge(_workspace_id: str, *, workspace_path: Path) -> _FakeBridge:
        del workspace_path
        return bridge

    service._tool_commands._get_bridge = _get_bridge  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "id",
            "cwd": "/workspace",
            "env": {},
            "timeout_policy": {"deadline_seconds": 9.5, "grace_seconds": 1.0},
            "command_id": "cmd-91",
            "timeout_seconds": 9.5,
        },
    )

    assert captured["timeout_seconds"] == 9.5
    assert captured["timeout_policy"] == {"deadline_seconds": 9.5, "grace_seconds": 1.0}
    assert captured["command_id"] == "cmd-91"
    assert payload["accepted"] is True
    assert payload["status"] == "completed"
    assert payload["metadata"]["transport"] == "file-comm"


def test_dispatch_tool_command_materializes_workspace_files_before_file_comm(
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)

    class _FakeBridge:
        async def dispatch_command(
            self,
            *,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
        ) -> RunnerToolCommandResult:
            del command, cwd, env, timeout_policy, timeout_seconds
            workspace = tmp_path / "runner-root" / "tasks" / "task-91"
            assert (workspace / "wordlists" / "ffuf.txt").read_text(
                encoding="utf-8"
            ) == "admin\nlogin\n"
            assert (workspace / "reports" / "wapiti").is_dir()
            return RunnerToolCommandResult(
                command_id=command_id,
                status="completed",
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                transport="file-comm",
            )

    service._tool_commands._get_bridge = lambda _workspace_id, *, workspace_path: _FakeBridge()  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "web_applications.web_crawlers.ffuf",
            "command": "ffuf -w /workspace/wordlists/ffuf.txt -u http://example/FUZZ",
            "cwd": "/workspace",
            "env": {},
            "timeout_policy": {"deadline_seconds": 9.5, "grace_seconds": 1.0},
            "command_id": "cmd-workspace-files",
            "timeout_seconds": 9.5,
            "workspace_files": [
                {
                    "relative_path": "wordlists/ffuf.txt",
                    "content_base64": base64.b64encode(b"admin\nlogin\n").decode("ascii"),
                    "mode": "write",
                }
            ],
            "workspace_directories": [
                {
                    "relative_path": "reports/wapiti",
                    "description": "report output parent",
                }
            ],
        },
    )

    assert payload["accepted"] is True
    assert payload["status"] == "completed"


def test_dispatch_tool_command_rejects_non_positive_timeout(tmp_path: Path) -> None:
    service = _build_service(tmp_path)

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "id",
            "timeout_seconds": 0,
        },
    )

    assert payload["status"] == "failed"
    assert payload["error_code"] == "INVALID_TIMEOUT_SECONDS"


def test_dispatch_tool_command_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    service = _build_service(tmp_path)

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "id",
            "cwd": "/tmp",
            "timeout_seconds": 5,
        },
    )

    assert payload["status"] == "rejected"
    assert payload["error_code"] == "RUNNER_TOOL_COMMAND_CWD_OUTSIDE_WORKSPACE"


def test_dispatch_tool_command_normalizes_relative_cwd_to_workspace(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured: dict[str, object] = {}

    class _FakeBridge:
        async def dispatch_command(
            self,
            *,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
        ) -> RunnerToolCommandResult:
            del command, env, timeout_policy, timeout_seconds
            captured["cwd"] = cwd
            return RunnerToolCommandResult(
                command_id=command_id,
                status="completed",
                success=True,
                exit_code=0,
                stdout="ok",
                stderr="",
                transport="file-comm",
            )

    bridge = _FakeBridge()

    def _get_bridge(_workspace_id: str, *, workspace_path: Path) -> _FakeBridge:
        del workspace_path
        return bridge

    service._tool_commands._get_bridge = _get_bridge  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "pwd",
            "cwd": "reports/../artifacts",
            "command_id": "cmd-cwd",
            "timeout_seconds": 5,
        },
    )

    assert payload["accepted"] is True
    assert captured["cwd"] == "/workspace/artifacts"


def test_dispatch_tool_command_returns_timeout_failure_payload(tmp_path: Path) -> None:
    service = _build_service(tmp_path)

    class _TimeoutBridge:
        async def dispatch_command(
            self,
            *,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
        ) -> RunnerToolCommandResult:
            del command, cwd, env, timeout_policy, timeout_seconds
            return RunnerToolCommandResult(
                command_id=command_id,
                status="failed",
                success=False,
                exit_code=-1,
                stdout="",
                stderr="timeout",
                error_code=ERROR_FILE_COMM_TIMEOUT,
                error_message="Timed out waiting for file-comm result.",
            )

    bridge = _TimeoutBridge()

    def _get_bridge(_workspace_id: str, *, workspace_path: Path) -> _TimeoutBridge:
        del workspace_path
        return bridge

    service._tool_commands._get_bridge = _get_bridge  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "id",
            "command_id": "cmd-timeout",
            "timeout_seconds": 1.0,
        },
    )

    assert payload["accepted"] is False
    assert payload["status"] == "failed"
    assert payload["error_code"] == ERROR_FILE_COMM_TIMEOUT
    assert payload["metadata"]["command_id"] == "cmd-timeout"


def test_dispatch_tool_command_uses_pty_transport_when_requested(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured: dict[str, object] = {}

    class _FakePtyTransport:
        async def submit_command(
            self,
            *,
            runtime_job_id: str,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
            session_name: str | None,
            cleanup_session: bool,
        ) -> RunnerToolCommandResult:
            captured.update(
                {
                    "runtime_job_id": runtime_job_id,
                    "command": command,
                    "cwd": cwd,
                    "env": env,
                    "timeout_policy": timeout_policy,
                    "timeout_seconds": timeout_seconds,
                    "command_id": command_id,
                    "session_name": session_name,
                    "cleanup_session": cleanup_session,
                }
            )
            return RunnerToolCommandResult.running(command_id=command_id, transport="pty")

        async def get_command_status(self, command_id: str) -> RunnerToolCommandResult:
            return RunnerToolCommandResult(
                command_id=command_id,
                status="completed",
                success=True,
                exit_code=0,
                stdout="pty-ok",
                stderr="",
                transport="pty",
            )

    pty_transport = _FakePtyTransport()

    def _get_pty_command_transport(
        _workspace_id: str,
        *,
        workspace_path: Path,
    ) -> _FakePtyTransport:
        del workspace_path
        return pty_transport

    service._tool_commands._get_pty_command_transport = _get_pty_command_transport  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="dispatch_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "echo ok",
            "command_id": "cmd-pty-dispatch",
            "timeout_seconds": 5,
            "transport": "pty",
            "session_name": "cloud_call",
            "cleanup_session": True,
        },
    )

    assert payload["accepted"] is True
    assert payload["status"] == "completed"
    assert payload["metadata"]["transport"] == "pty"
    assert payload["metadata"]["stdout"] == "pty-ok"
    assert captured["session_name"] == "cloud_call"
    assert captured["command"] == "echo ok"


def test_submit_tool_command_returns_running_payload_without_waiting(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured: dict[str, object] = {}

    class _FakeBridge:
        async def submit_command(
            self,
            *,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
        ) -> RunnerToolCommandResult:
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            captured["timeout_policy"] = timeout_policy
            captured["timeout_seconds"] = timeout_seconds
            captured["command_id"] = command_id
            return RunnerToolCommandResult.running(command_id=command_id, transport="file-comm")

    bridge = _FakeBridge()

    def _get_bridge(_workspace_id: str, *, workspace_path: Path) -> _FakeBridge:
        del workspace_path
        return bridge

    service._tool_commands._get_bridge = _get_bridge  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="submit_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "network.nmap",
            "command": "nmap -T4 -n -oX - 127.0.0.1",
            "command_id": "cmd-submit",
            "timeout_seconds": 60,
        },
    )

    assert payload["accepted"] is True
    assert payload["status"] == "running"
    assert payload["metadata"]["command_id"] == "cmd-submit"
    assert captured["timeout_seconds"] == 60.0


def test_get_tool_command_result_polls_same_file_comm_command(tmp_path: Path) -> None:
    service = _build_service(tmp_path)

    class _FakeBridge:
        async def get_command_status(self, command_id: str) -> RunnerToolCommandResult:
            return RunnerToolCommandResult(
                command_id=command_id,
                status="completed",
                success=True,
                exit_code=0,
                stdout="done",
                stderr="",
                transport="file-comm",
            )

    bridge = _FakeBridge()

    def _get_bridge(_workspace_id: str, *, workspace_path: Path) -> _FakeBridge:
        del workspace_path
        return bridge

    service._tool_commands._get_bridge = _get_bridge  # type: ignore[assignment]
    service._tool_commands._command_transports["cmd-submit"] = "file-comm"

    payload = service.dispatch_operation(
        operation="get_tool_command_result",
        params={
            "runtime_job_id": "task-runtime-91",
            "command_id": "cmd-submit",
        },
    )

    assert payload["accepted"] is True
    assert payload["status"] == "completed"
    assert payload["metadata"]["stdout"] == "done"
    assert payload["metadata"]["terminal"] is True


def test_submit_tool_command_uses_pty_transport_when_requested(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    captured: dict[str, object] = {}

    class _FakePtyTransport:
        async def submit_command(
            self,
            *,
            runtime_job_id: str,
            command: str,
            cwd: str,
            env: dict[str, str],
            timeout_policy: dict[str, object],
            timeout_seconds: float,
            command_id: str,
            session_name: str | None,
            cleanup_session: bool,
        ) -> RunnerToolCommandResult:
            captured.update(
                {
                    "runtime_job_id": runtime_job_id,
                    "command": command,
                    "cwd": cwd,
                    "env": env,
                    "timeout_policy": timeout_policy,
                    "timeout_seconds": timeout_seconds,
                    "command_id": command_id,
                    "session_name": session_name,
                    "cleanup_session": cleanup_session,
                }
            )
            return RunnerToolCommandResult.running(command_id=command_id, transport="pty")

    pty_transport = _FakePtyTransport()

    def _get_pty_command_transport(
        _workspace_id: str,
        *,
        workspace_path: Path,
    ) -> _FakePtyTransport:
        del workspace_path
        return pty_transport

    service._tool_commands._get_pty_command_transport = _get_pty_command_transport  # type: ignore[assignment]

    payload = service.dispatch_operation(
        operation="submit_tool_command",
        params={
            "runtime_job_id": "task-runtime-91",
            "tool": "shell.exec",
            "command": "echo ok",
            "command_id": "cmd-pty",
            "timeout_seconds": 5,
            "transport": "pty",
            "session_name": "batch_shell",
            "cleanup_session": True,
        },
    )

    assert payload["accepted"] is True
    assert payload["metadata"]["transport"] == "pty"
    assert captured["session_name"] == "batch_shell"
    assert captured["cleanup_session"] is True
    assert captured["command"] == "echo ok"


def test_retire_runtime_uses_short_graceful_stop_before_cleanup(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service._lifecycle._job_store.mark_running("task-runtime-91", container_id="cid-91")
    docker_runtime = service._lifecycle._docker_runtime
    service._lifecycle._cleanup.cleanup_task.return_value = SimpleNamespace(  # type: ignore[attr-defined]
        status="ok",
        container_removed=True,
        workspace_removed=True,
        retained_paths=(),
    )

    payload = service.dispatch_operation(
        operation="retire_runtime",
        params={"runtime_job_id": "task-runtime-91"},
    )

    assert payload["accepted"] is True
    docker_runtime.stop_container.assert_called_once_with(  # type: ignore[attr-defined]
        "cid-91",
        timeout_seconds=1,
    )


def test_retire_runtime_recovers_missing_job_from_task_identity(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    workspace = service._workspace.initialize_task_workspace("task-92")
    marker = workspace / "reports" / "report.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("keep", encoding="utf-8")
    docker_runtime = service._lifecycle._docker_runtime
    docker_runtime.find_container_id_by_name.return_value = "cid-92"  # type: ignore[attr-defined]
    service._lifecycle._cleanup.cleanup_task.return_value = SimpleNamespace(  # type: ignore[attr-defined]
        status="ok",
        container_removed=True,
        workspace_removed=True,
        retained_paths=(),
    )

    payload = service.dispatch_operation(
        operation="retire_runtime",
        params={
            "runtime_job_id": "task-runtime-92",
            "tenant_id": "7",
            "task_id": "92",
            "workspace_id": "task-92",
        },
    )

    assert payload["accepted"] is True
    assert payload["metadata"]["container_removed"] is True
    assert service._lifecycle._job_store.get_job("task-runtime-92").status == "stopped"
    docker_runtime.find_container_id_by_name.assert_called_once_with("drowai-7-task-92")  # type: ignore[attr-defined]
    docker_runtime.stop_container.assert_called_once_with(  # type: ignore[attr-defined]
        "cid-92",
        timeout_seconds=1,
    )
