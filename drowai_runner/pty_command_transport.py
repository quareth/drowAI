"""Runner PTY tool-command transport built on terminal proxy primitives.

This module executes PTY-capable tool commands through scoped runner terminal
sessions and returns the same command-result envelope as the file-comm path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import posixpath
import re
import shlex
from typing import Any, Mapping

from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.tool_command_models import (
    RunnerToolCommandResult,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    TRANSPORT_PTY,
)

logger = logging.getLogger(__name__)

ERROR_PTY_COMMAND_NOT_FOUND = "PTY_COMMAND_NOT_FOUND"
ERROR_PTY_COMMAND_TIMEOUT = "PTY_COMMAND_TIMEOUT"
ERROR_PTY_SESSION_FAILED = "PTY_SESSION_FAILED"

_EXIT_MARKER_PREFIX = "__DROWAI_EXIT_CODE_"


class RunnerPtyCommandTransport:
    """Submit and observe runner PTY tool commands."""

    def __init__(
        self,
        *,
        terminal_proxy: RunnerTerminalProxy,
        workspace_path: str | Path,
        max_parallel_commands: int,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if max_parallel_commands < 1:
            raise ValueError("max_parallel_commands must be >= 1.")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0.")
        del workspace_path
        self._terminal_proxy = terminal_proxy
        self._poll_interval_seconds = poll_interval_seconds
        self._semaphore = asyncio.Semaphore(max_parallel_commands)
        self._results: dict[str, RunnerToolCommandResult] = {}
        self._tasks: dict[str, asyncio.Task[RunnerToolCommandResult]] = {}
        self._lock = asyncio.Lock()

    async def submit_command(
        self,
        *,
        runtime_job_id: str,
        command: str,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
        timeout_seconds: float,
        timeout_policy: Mapping[str, Any] | None = None,
        command_id: str,
        session_name: str | None = None,
        cleanup_session: bool = True,
    ) -> RunnerToolCommandResult:
        """Start a PTY command in the background and return promptly."""
        normalized_command_id = str(command_id or "").strip()
        if not normalized_command_id:
            raise ValueError("command_id must not be empty.")
        if not command.strip():
            raise ValueError("command must not be empty.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0.")

        async with self._lock:
            cached = self._results.get(normalized_command_id)
            if cached is not None:
                return cached
            existing = self._tasks.get(normalized_command_id)
            if existing is not None and not existing.done():
                return RunnerToolCommandResult.running(
                    command_id=normalized_command_id,
                    transport=TRANSPORT_PTY,
                )
            task = asyncio.create_task(
                self._execute_command(
                    runtime_job_id=runtime_job_id,
                    command_id=normalized_command_id,
                    command=command,
                    cwd=cwd,
                    env=dict(env or {}),
                    timeout_seconds=timeout_seconds,
                    timeout_policy=dict(timeout_policy or {}),
                    session_name=session_name,
                    cleanup_session=cleanup_session,
                )
            )
            self._tasks[normalized_command_id] = task
            task.add_done_callback(lambda done, cid=normalized_command_id: self._record_task_result(cid, done))

        return RunnerToolCommandResult.running(
            command_id=normalized_command_id,
            transport=TRANSPORT_PTY,
        )

    async def get_command_status(self, command_id: str) -> RunnerToolCommandResult:
        """Return current PTY command status/result."""
        normalized_command_id = str(command_id or "").strip()
        if not normalized_command_id:
            raise ValueError("command_id must not be empty.")
        cached = self._results.get(normalized_command_id)
        if cached is not None:
            return cached
        task = self._tasks.get(normalized_command_id)
        if task is None:
            return RunnerToolCommandResult(
                command_id=normalized_command_id,
                status=STATUS_FAILED,
                success=False,
                exit_code=-1,
                stderr=f"PTY command `{normalized_command_id}` was not found.",
                error_code=ERROR_PTY_COMMAND_NOT_FOUND,
                error_message="PTY command was not found.",
                transport=TRANSPORT_PTY,
            )
        if task.done():
            try:
                result = task.result()
            except TimeoutErrorResult as exc:
                result = exc.result
            except Exception as exc:
                result = RunnerToolCommandResult(
                    command_id=normalized_command_id,
                    status=STATUS_FAILED,
                    success=False,
                    exit_code=-1,
                    stderr=str(exc),
                    error_code=ERROR_PTY_SESSION_FAILED,
                    error_message=str(exc),
                    transport=TRANSPORT_PTY,
                )
            self._results[normalized_command_id] = result
            return result
        return RunnerToolCommandResult.running(
            command_id=normalized_command_id,
            transport=TRANSPORT_PTY,
        )

    async def _execute_command(
        self,
        *,
        runtime_job_id: str,
        command_id: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout_seconds: float,
        timeout_policy: dict[str, Any],
        session_name: str | None,
        cleanup_session: bool,
    ) -> RunnerToolCommandResult:
        async with self._semaphore:
            resolved_session_name = session_name or f"tool_{_safe_identifier(command_id)}"
            session = self._terminal_proxy.open_terminal_session(
                runtime_job_id=runtime_job_id,
                session_name=resolved_session_name,
            )
            if not session.accepted:
                return RunnerToolCommandResult(
                    command_id=command_id,
                    status=STATUS_FAILED,
                    success=False,
                    exit_code=-1,
                    stderr=session.error_message or "PTY session open failed.",
                    error_code=session.error_code or ERROR_PTY_SESSION_FAILED,
                    error_message=session.error_message,
                    transport=TRANSPORT_PTY,
                )

            metadata = dict(session.metadata or {})
            session_id = str(metadata.get("session_id") or "")
            try:
                raw_output, stdout, exit_code = await self._run_shell_command(
                    session_id=session_id,
                    command_id=command_id,
                    command=_wrap_command(command=command, cwd=cwd, env=env),
                    timeout_seconds=timeout_seconds,
                    timeout_policy=timeout_policy,
                )
            finally:
                if cleanup_session and session_id:
                    self._terminal_proxy.close_terminal_session(session_id=session_id)

            success = exit_code == 0
            return RunnerToolCommandResult(
                command_id=command_id,
                status="completed",
                success=success,
                exit_code=exit_code,
                stdout=stdout,
                stderr="",
                artifacts=(),
                metadata={
                    "transport": TRANSPORT_PTY,
                    "raw_output_chars": len(raw_output),
                    "command_text": command,
                    "cwd": cwd,
                    "timeout_policy": timeout_policy,
                },
                transport=TRANSPORT_PTY,
            )

    async def _run_shell_command(
        self,
        *,
        session_id: str,
        command_id: str,
        command: str,
        timeout_seconds: float,
        timeout_policy: Mapping[str, Any],
    ) -> tuple[str, str, int]:
        safe_id = _safe_identifier(command_id)
        start_marker = f"__DROWAI_START_{safe_id}__"
        exit_marker = f"{_EXIT_MARKER_PREFIX}{safe_id}__="
        wrapped = (
            f"printf '\\n{start_marker}\\n'; "
            f"{command}; __drowai_code=$?; "
            f"printf '\\n{exit_marker}%s\\n' \"$__drowai_code\"\n"
        )
        self._terminal_proxy.send_terminal_input(session_id=session_id, data=wrapped)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        chunks: list[str] = []
        pattern = re.compile(re.escape(exit_marker) + r"(\d+)")
        while asyncio.get_running_loop().time() < deadline:
            response = self._terminal_proxy.read_terminal_output(
                session_id=session_id,
                max_bytes=65536,
            )
            if response.accepted and response.metadata:
                output = str(response.metadata.get("output") or "")
                if output:
                    chunks.append(output)
                    raw = "".join(chunks)
                    matches = list(pattern.finditer(raw))
                    if matches:
                        match = matches[-1]
                        return raw, _extract_stdout(raw, start_marker, match.start()), int(match.group(1))
            await asyncio.sleep(self._poll_interval_seconds)

        raw = "".join(chunks)
        raise TimeoutErrorResult(
            RunnerToolCommandResult(
                command_id=command_id,
                status=STATUS_TIMED_OUT,
                success=False,
                exit_code=-1,
                stdout=_extract_stdout(raw, start_marker, len(raw)),
                stderr=f"PTY command `{command_id}` timed out after {timeout_seconds} seconds.",
                error_code=ERROR_PTY_COMMAND_TIMEOUT,
                error_message="Timed out waiting for PTY command result.",
                metadata={
                    "transport": TRANSPORT_PTY,
                    "partial_output": True,
                    "timeout_policy": dict(timeout_policy),
                },
                transport=TRANSPORT_PTY,
            )
        )

    def _record_task_result(self, command_id: str, task: asyncio.Task[RunnerToolCommandResult]) -> None:
        try:
            self._results[command_id] = task.result()
        except TimeoutErrorResult as exc:
            self._results[command_id] = exc.result
        except Exception as exc:
            self._results[command_id] = RunnerToolCommandResult(
                command_id=command_id,
                status=STATUS_FAILED,
                success=False,
                exit_code=-1,
                stderr=str(exc),
                error_code=ERROR_PTY_SESSION_FAILED,
                error_message=str(exc),
                transport=TRANSPORT_PTY,
            )


class TimeoutErrorResult(Exception):
    """Internal wrapper carrying a terminal timeout result."""

    def __init__(self, result: RunnerToolCommandResult) -> None:
        super().__init__(result.error_message or "PTY command timed out.")
        self.result = result


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)[:80] or "command"


def _extract_stdout(raw: str, start_marker: str, end_index: int) -> str:
    bounded_end = max(0, min(int(end_index), len(raw)))

    # PTYs echo the submitted wrapper command, including marker strings. The
    # printed marker nearest the exit marker is the real stdout boundary.
    start = raw.rfind(start_marker, 0, bounded_end)
    if start >= 0:
        line_end = raw.find("\n", start, bounded_end)
        start = (line_end + 1) if line_end >= 0 else start + len(start_marker)
    else:
        start = 0
    return _strip_ansi(raw[start:bounded_end]).replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _wrap_command(*, command: str, cwd: str, env: Mapping[str, str]) -> str:
    normalized_cwd = posixpath.normpath(cwd or "/workspace")
    prefix = f"cd {shlex.quote(normalized_cwd)}"
    if env:
        env_parts = [
            f"{shlex.quote(str(key))}={shlex.quote(str(value))}"
            for key, value in sorted(env.items())
        ]
        return f"{prefix} && env {' '.join(env_parts)} bash -c {shlex.quote(command)}"
    return f"{prefix} && bash -c {shlex.quote(command)}"


__all__ = ["RunnerPtyCommandTransport"]
