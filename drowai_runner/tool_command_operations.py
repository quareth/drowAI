"""Runner-local tool-command operation orchestration.

This module owns command parameter parsing, workspace preparation, transport
selection, command-result polling, and async bridge loop coordination for
runner-local tool execution. It does not perform protocol mapping or websocket
I/O.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import posixpath
from pathlib import Path
import threading
from typing import Any, Mapping
import uuid

from drowai_runner.config import RunnerConfig
from drowai_runner.file_comm_bridge import RunnerFileCommBridge
from drowai_runner.job_store import RunnerJobStore
from drowai_runner.pty_command_transport import RunnerPtyCommandTransport
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.tool_command_models import (
    RunnerToolCommandResult,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_TIMED_OUT,
    TERMINAL_STATUSES,
    TRANSPORT_FILE_COMM,
    TRANSPORT_PTY,
)
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.tool_command_transport import normalize_tool_command_transport
from runtime_shared.workspace_files import (
    RuntimeWorkspaceDirectory,
    RuntimeWorkspaceFile,
    RuntimeWorkspaceFileError,
    materialize_runtime_workspace_preparation,
    normalize_runtime_workspace_directories,
    normalize_runtime_workspace_files,
)


class RunnerToolCommandOperations:
    """Execute runner-local tool-command operations."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        workspace: RunnerWorkspaceManager,
        job_store: RunnerJobStore,
        terminal_proxy: RunnerTerminalProxy,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._job_store = job_store
        self._terminal_proxy = terminal_proxy
        self._file_comm_bridges: dict[str, RunnerFileCommBridge] = {}
        self._file_comm_bridges_guard = threading.Lock()
        self._pty_command_transports: dict[str, RunnerPtyCommandTransport] = {}
        self._pty_command_transports_guard = threading.Lock()
        self._command_transports: dict[str, str] = {}
        self._command_transports_guard = threading.Lock()
        self._bridge_event_loop: asyncio.AbstractEventLoop | None = None
        self._bridge_event_loop_thread: threading.Thread | None = None
        self._bridge_event_loop_guard = threading.Lock()

    def dispatch_tool_command(self, params: dict[str, object]) -> dict[str, object]:
        parsed = self._parse_tool_command_params(params)
        if "error" in parsed:
            return parsed["error"]
        runtime_job_id = parsed["runtime_job_id"]
        command_id = parsed["command_id"]
        transport = parsed["transport"]
        result = self._run_bridge_coroutine(
            self._dispatch_tool_command_async(
                runtime_job_id=runtime_job_id,
                workspace_id=parsed["workspace_id"],
                workspace_path=parsed["workspace_path"],
                command_id=command_id,
                command=parsed["command"],
                cwd=parsed["cwd"],
                env=parsed["env"],
                timeout_policy=parsed["timeout_policy"],
                timeout_seconds=parsed["timeout_seconds"],
                transport=transport,
                workspace_files=parsed["workspace_files"],
                workspace_directories=parsed["workspace_directories"],
                params=params,
            )
        )
        with self._command_transports_guard:
            self._command_transports[command_id] = transport
        self._job_store.set_last_command_id(runtime_job_id, command_id=command_id)
        return self._command_result_response(runtime_job_id=runtime_job_id, result=result)

    def submit_tool_command(self, params: dict[str, object]) -> dict[str, object]:
        parsed = self._parse_tool_command_params(params)
        if "error" in parsed:
            return parsed["error"]
        runtime_job_id = parsed["runtime_job_id"]
        command_id = parsed["command_id"]
        transport = parsed["transport"]
        result = self._run_bridge_coroutine(
            self._submit_tool_command_async(
                runtime_job_id=runtime_job_id,
                workspace_id=parsed["workspace_id"],
                workspace_path=parsed["workspace_path"],
                command_id=command_id,
                command=parsed["command"],
                cwd=parsed["cwd"],
                env=parsed["env"],
                timeout_policy=parsed["timeout_policy"],
                timeout_seconds=parsed["timeout_seconds"],
                transport=transport,
                workspace_files=parsed["workspace_files"],
                workspace_directories=parsed["workspace_directories"],
                params=params,
            )
        )
        with self._command_transports_guard:
            self._command_transports[command_id] = transport
        self._job_store.set_last_command_id(runtime_job_id, command_id=command_id)
        return self._command_result_response(runtime_job_id=runtime_job_id, result=result)

    def get_tool_command_result(self, params: dict[str, object]) -> dict[str, object]:
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        command_id = str(params.get("command_id") or "").strip()
        if not runtime_job_id:
            return {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}
        if not command_id:
            return {"status": "failed", "error_code": "MISSING_COMMAND_ID"}
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return {"status": "failed", "error_code": "RUNNER_JOB_NOT_FOUND"}
        workspace_path = self._workspace.resolve_task_workspace(job.workspace_id)
        requested_transport = _normalize_transport(params.get("transport"))
        with self._command_transports_guard:
            transport = requested_transport or self._command_transports.get(command_id) or TRANSPORT_FILE_COMM

        result = self._run_bridge_coroutine(
            self._get_tool_command_result_async(
                runtime_job_id=runtime_job_id,
                workspace_id=job.workspace_id,
                workspace_path=workspace_path,
                command_id=command_id,
                transport=transport,
            )
        )
        return self._command_result_response(runtime_job_id=runtime_job_id, result=result)

    async def _submit_tool_command_async(
        self,
        *,
        runtime_job_id: str,
        workspace_id: str,
        workspace_path: Path,
        command_id: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout_policy: dict[str, object],
        timeout_seconds: float,
        transport: str,
        workspace_files: tuple[RuntimeWorkspaceFile, ...],
        workspace_directories: tuple[RuntimeWorkspaceDirectory, ...],
        params: Mapping[str, object],
    ) -> RunnerToolCommandResult:
        materialization_error = _materialize_tool_command_workspace_files(
            command_id=command_id,
            workspace_path=workspace_path,
            workspace_files=workspace_files,
            workspace_directories=workspace_directories,
        )
        if materialization_error is not None:
            return materialization_error
        if transport == TRANSPORT_PTY:
            pty = self._get_pty_command_transport(workspace_id, workspace_path=workspace_path)
            return await pty.submit_command(
                runtime_job_id=runtime_job_id,
                command=command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
                timeout_policy=timeout_policy,
                command_id=command_id,
                session_name=_optional_text(params.get("session_name")),
                cleanup_session=bool(params.get("cleanup_session", True)),
            )
        bridge = self._get_bridge(workspace_id, workspace_path=workspace_path)
        return await bridge.submit_command(
            command=command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            timeout_policy=timeout_policy,
            command_id=command_id,
        )

    async def _dispatch_tool_command_async(
        self,
        *,
        runtime_job_id: str,
        workspace_id: str,
        workspace_path: Path,
        command_id: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout_policy: dict[str, object],
        timeout_seconds: float,
        transport: str,
        workspace_files: tuple[RuntimeWorkspaceFile, ...],
        workspace_directories: tuple[RuntimeWorkspaceDirectory, ...],
        params: Mapping[str, object],
    ) -> RunnerToolCommandResult:
        materialization_error = _materialize_tool_command_workspace_files(
            command_id=command_id,
            workspace_path=workspace_path,
            workspace_files=workspace_files,
            workspace_directories=workspace_directories,
        )
        if materialization_error is not None:
            return materialization_error
        if transport != TRANSPORT_PTY:
            bridge = self._get_bridge(workspace_id, workspace_path=workspace_path)
            result = await bridge.dispatch_command(
                command=command,
                cwd=cwd,
                env=env,
                timeout_policy=timeout_policy,
                timeout_seconds=timeout_seconds,
                command_id=command_id,
            )
            return RunnerToolCommandResult(
                command_id=result.command_id,
                status=result.status,
                success=result.success,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                artifacts=tuple(result.artifacts),
                metadata=dict(result.metadata),
                error_code=result.error_code,
                error_message=result.error_message,
                transport=TRANSPORT_FILE_COMM,
            )

        result = await self._submit_tool_command_async(
            runtime_job_id=runtime_job_id,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            command_id=command_id,
            command=command,
            cwd=cwd,
            env=env,
            timeout_policy=timeout_policy,
            timeout_seconds=timeout_seconds,
            transport=transport,
            workspace_files=workspace_files,
            workspace_directories=workspace_directories,
            params=params,
        )
        if result.terminal:
            return result

        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0) + 1.0
        last_result = result
        while asyncio.get_running_loop().time() <= deadline:
            await asyncio.sleep(0.05)
            last_result = await self._get_tool_command_result_async(
                runtime_job_id=runtime_job_id,
                workspace_id=workspace_id,
                workspace_path=workspace_path,
                command_id=command_id,
                transport=transport,
            )
            if last_result.terminal:
                return last_result
        return last_result

    async def _get_tool_command_result_async(
        self,
        *,
        runtime_job_id: str,
        workspace_id: str,
        workspace_path: Path,
        command_id: str,
        transport: str,
    ) -> RunnerToolCommandResult:
        del runtime_job_id
        if transport == TRANSPORT_PTY:
            pty = self._get_pty_command_transport(workspace_id, workspace_path=workspace_path)
            return await pty.get_command_status(command_id)
        bridge = self._get_bridge(workspace_id, workspace_path=workspace_path)
        return await bridge.get_command_status(command_id)

    def _parse_tool_command_params(self, params: Mapping[str, object]) -> dict[str, Any]:
        runtime_job_id = str(params.get("runtime_job_id") or "").strip()
        tool = str(params.get("tool") or "").strip()
        if not runtime_job_id:
            return {"error": {"status": "failed", "error_code": "MISSING_RUNTIME_JOB_ID"}}
        if not tool:
            return {"error": {"status": "failed", "error_code": "MISSING_TOOL"}}
        raw_timeout_seconds = params.get("timeout_seconds")
        if raw_timeout_seconds is None:
            timeout_seconds = 30.0
        else:
            try:
                timeout_seconds = float(raw_timeout_seconds)
            except (TypeError, ValueError):
                return {"error": {"status": "failed", "error_code": "INVALID_TIMEOUT_SECONDS"}}
        if timeout_seconds <= 0:
            return {"error": {"status": "failed", "error_code": "INVALID_TIMEOUT_SECONDS"}}
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return {"error": {"status": "failed", "error_code": "RUNNER_JOB_NOT_FOUND"}}
        try:
            workspace_path = self._workspace.resolve_task_workspace(job.workspace_id)
        except ValueError as exc:
            return {
                "error": {
                    "accepted": False,
                    "status": "rejected",
                    "error_code": "RUNNER_WORKSPACE_PATH_OUTSIDE_SCOPE",
                    "error_message": str(exc),
                }
            }
        command = str(params.get("command") or "").strip()
        if not command:
            return {"error": {"status": "failed", "error_code": "MISSING_COMMAND"}}
        try:
            cwd = _normalize_container_cwd(params.get("cwd"))
        except ValueError as exc:
            return {
                "error": {
                    "accepted": False,
                    "status": "rejected",
                    "error_code": "RUNNER_TOOL_COMMAND_CWD_OUTSIDE_WORKSPACE",
                    "error_message": str(exc),
                }
            }
        raw_env = params.get("env")
        env = {str(key): str(value) for key, value in raw_env.items()} if isinstance(raw_env, Mapping) else {}
        raw_timeout_policy = params.get("timeout_policy")
        timeout_policy = dict(raw_timeout_policy) if isinstance(raw_timeout_policy, Mapping) else {}
        timeout_policy.setdefault("deadline_seconds", timeout_seconds)
        command_id = str(params.get("command_id") or str(uuid.uuid4())).strip()
        transport = _normalize_transport(params.get("transport")) or TRANSPORT_FILE_COMM
        try:
            workspace_files = normalize_runtime_workspace_files(params.get("workspace_files", ()))
            workspace_directories = normalize_runtime_workspace_directories(
                params.get("workspace_directories", ())
            )
        except RuntimeWorkspaceFileError as exc:
            return {
                "error": {
                    "accepted": False,
                    "status": "rejected",
                    "error_code": "RUNNER_TOOL_COMMAND_WORKSPACE_PREPARATION_INVALID",
                    "error_message": str(exc),
                }
            }
        return {
            "runtime_job_id": runtime_job_id,
            "workspace_id": job.workspace_id,
            "workspace_path": workspace_path,
            "tool": tool,
            "command": command,
            "cwd": cwd,
            "env": env,
            "timeout_policy": timeout_policy,
            "timeout_seconds": timeout_seconds,
            "command_id": command_id,
            "transport": transport,
            "workspace_files": workspace_files,
            "workspace_directories": workspace_directories,
        }

    def _get_bridge(self, workspace_id: str, *, workspace_path: Path) -> RunnerFileCommBridge:
        existing = self._file_comm_bridges.get(workspace_id)
        if existing is not None:
            return existing
        with self._file_comm_bridges_guard:
            existing = self._file_comm_bridges.get(workspace_id)
            if existing is not None:
                return existing
            bridge = RunnerFileCommBridge(
                workspace_path=workspace_path,
                max_parallel_commands=self._config.max_parallel_commands_per_task,
            )
            self._file_comm_bridges[workspace_id] = bridge
            return bridge

    def _get_pty_command_transport(
        self,
        workspace_id: str,
        *,
        workspace_path: Path,
    ) -> RunnerPtyCommandTransport:
        existing = self._pty_command_transports.get(workspace_id)
        if existing is not None:
            return existing
        with self._pty_command_transports_guard:
            existing = self._pty_command_transports.get(workspace_id)
            if existing is not None:
                return existing
            transport = RunnerPtyCommandTransport(
                terminal_proxy=self._terminal_proxy,
                workspace_path=workspace_path,
                max_parallel_commands=self._config.max_parallel_commands_per_task,
            )
            self._pty_command_transports[workspace_id] = transport
            return transport

    @staticmethod
    def _command_result_response(
        *,
        runtime_job_id: str,
        result: RunnerToolCommandResult,
    ) -> dict[str, object]:
        terminal = result.status in TERMINAL_STATUSES
        accepted = (
            result.status == STATUS_RUNNING
            or result.status not in {STATUS_FAILED, STATUS_TIMED_OUT}
            or bool(result.success)
        )
        payload = result.to_payload()
        payload["runtime_job_id"] = runtime_job_id
        payload["terminal"] = terminal
        return {
            "accepted": accepted,
            "status": result.status,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "metadata": payload,
        }

    def _run_bridge_coroutine(self, coroutine: Coroutine[Any, Any, object]) -> object:
        loop = self._bridge_loop_instance()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result()

    def _bridge_loop_instance(self) -> asyncio.AbstractEventLoop:
        existing = self._bridge_event_loop
        if existing is not None:
            return existing
        with self._bridge_event_loop_guard:
            existing = self._bridge_event_loop
            if existing is not None:
                return existing
            loop = asyncio.new_event_loop()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(
                target=_run_loop,
                name="runner-file-comm-loop",
                daemon=True,
            )
            thread.start()
            self._bridge_event_loop = loop
            self._bridge_event_loop_thread = thread
            return loop


def _materialize_tool_command_workspace_files(
    *,
    command_id: str,
    workspace_path: Path,
    workspace_files: tuple[RuntimeWorkspaceFile, ...],
    workspace_directories: tuple[RuntimeWorkspaceDirectory, ...],
) -> RunnerToolCommandResult | None:
    """Materialize pre-execution workspace preparation into the runner workspace."""

    if not workspace_files and not workspace_directories:
        return None
    try:
        materialize_runtime_workspace_preparation(
            workspace=workspace_path,
            files=workspace_files,
            directories=workspace_directories,
        )
    except (OSError, RuntimeWorkspaceFileError) as exc:
        return RunnerToolCommandResult(
            command_id=command_id,
            status=STATUS_FAILED,
            success=False,
            exit_code=-1,
            stderr=str(exc),
            error_code="RUNNER_TOOL_COMMAND_WORKSPACE_PREPARATION_FAILED",
            error_message=str(exc),
        )
    return None


def _normalize_transport(value: object) -> str | None:
    return normalize_tool_command_transport(value)


def _normalize_container_cwd(value: object) -> str:
    raw = str(value or "/workspace").strip() or "/workspace"
    if raw == "/workspace":
        return raw
    if raw.startswith("/workspace/"):
        normalized = posixpath.normpath(raw)
    elif raw.startswith("/"):
        raise ValueError("tool command cwd must stay inside /workspace")
    else:
        normalized = posixpath.normpath(posixpath.join("/workspace", raw))
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        raise ValueError("tool command cwd must stay inside /workspace")
    return normalized


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["RunnerToolCommandOperations"]
