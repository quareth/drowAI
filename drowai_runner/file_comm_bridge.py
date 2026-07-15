"""Runner-side file-comm bridge for command dispatch and result correlation.

This module appends command rows to the task workspace `commands.jsonl`, waits
for matching executor results in `results.jsonl`, and enforces timeout plus
max-parallel-command limits without exposing absolute host paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from pydantic import ValidationError

from runtime_shared.file_comm_contracts import (
    CommandMessage,
    DEFAULT_FILE_COMM_TIMEOUT_SECONDS,
    FileCommWorkspacePaths,
    ResultMessage,
    TOOL_TIMEOUT_EXIT_CODE,
    TOOL_TIMEOUT_FAILURE_CATEGORY,
)
from runtime_shared.workspace_filesystem import WorkspaceFilesystem
from drowai_runner.tool_command_models import (
    RunnerToolCommandResult,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    TRANSPORT_FILE_COMM,
)

_DEFAULT_POLL_INTERVAL_SECONDS = 0.05
ERROR_FILE_COMM_TIMEOUT = "FILE_COMM_TIMEOUT"
ERROR_FILE_COMM_CANCELLED = "FILE_COMM_CANCELLED"
ERROR_RESULT_MALFORMED = "FILE_COMM_RESULT_MALFORMED"
ERROR_COMMAND_NOT_FOUND = "FILE_COMM_COMMAND_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class FileCommBridgeResult:
    """Stable runner-facing result for one file-comm command dispatch."""

    command_id: str
    status: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    artifacts: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _SubmittedCommand:
    """Runner-side ledger entry for commands picked up and removed by the executor."""

    command_id: str
    submitted_at: datetime
    timeout_seconds: float


class RunnerFileCommBridge:
    """Dispatch commands through file-comm with idempotency and timeout handling."""

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        max_parallel_commands: int,
        default_timeout_seconds: float = DEFAULT_FILE_COMM_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if max_parallel_commands < 1:
            raise ValueError("max_parallel_commands must be >= 1.")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be > 0.")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0.")

        self._paths = FileCommWorkspacePaths.from_workspace(workspace_path)
        self._paths.workspace.mkdir(parents=True, exist_ok=True)
        self._filesystem = WorkspaceFilesystem(self._paths.workspace)
        self._default_timeout_seconds = default_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._semaphore = asyncio.Semaphore(max_parallel_commands)
        self._command_lock = asyncio.Lock()
        self._result_cache: dict[str, FileCommBridgeResult] = {}
        self._inflight: dict[str, asyncio.Future[FileCommBridgeResult]] = {}
        self._submitted_commands: dict[str, _SubmittedCommand] = {}
        self._prepare_workspace_files()

    async def dispatch_command(
        self,
        *,
        command: str,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        timeout_policy: Mapping[str, Any] | None = None,
        command_id: str | None = None,
    ) -> FileCommBridgeResult:
        """Send one command and return the correlated result."""
        resolved_command_id = (command_id or str(uuid.uuid4())).strip()
        if not resolved_command_id:
            raise ValueError("command_id must not be empty.")
        if not command.strip():
            raise ValueError("command must not be empty.")

        cached = self._result_cache.get(resolved_command_id)
        if cached is not None:
            return cached

        async with self._command_lock:
            cached = self._result_cache.get(resolved_command_id)
            if cached is not None:
                return cached
            inflight = self._inflight.get(resolved_command_id)
            if inflight is not None:
                return await inflight
            future: asyncio.Future[FileCommBridgeResult] = asyncio.get_running_loop().create_future()
            self._inflight[resolved_command_id] = future

        try:
            async with self._semaphore:
                result = await self._execute_command(
                    command_id=resolved_command_id,
                    command=command,
                    cwd=cwd,
                    env=dict(env or {}),
                    timeout_seconds=timeout_seconds or self._default_timeout_seconds,
                    timeout_policy=timeout_policy,
                )
            self._result_cache[resolved_command_id] = result
            future.set_result(result)
            return result
        except asyncio.CancelledError:
            cancelled_result = FileCommBridgeResult(
                command_id=resolved_command_id,
                status=STATUS_FAILED,
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Command `{resolved_command_id}` was cancelled before completion.",
                error_code=ERROR_FILE_COMM_CANCELLED,
                error_message="Command dispatch cancelled.",
            )
            self._result_cache[resolved_command_id] = cancelled_result
            future.set_result(cancelled_result)
            return cancelled_result
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            async with self._command_lock:
                self._inflight.pop(resolved_command_id, None)

    async def submit_command(
        self,
        *,
        command: str,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        timeout_policy: Mapping[str, Any] | None = None,
        command_id: str | None = None,
    ) -> RunnerToolCommandResult:
        """Idempotently enqueue one file-comm command without waiting for the result."""
        resolved_command_id = (command_id or str(uuid.uuid4())).strip()
        if not resolved_command_id:
            raise ValueError("command_id must not be empty.")
        if not command.strip():
            raise ValueError("command must not be empty.")
        timeout = timeout_seconds or self._default_timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be > 0.")

        cached = self._result_cache.get(resolved_command_id)
        if cached is not None:
            return _command_result_from_bridge_result(cached)

        current_status = await self.get_command_status(resolved_command_id)
        if current_status.status != STATUS_FAILED or current_status.error_code != ERROR_COMMAND_NOT_FOUND:
            return current_status

        payload = self._build_command_payload(
            command_id=resolved_command_id,
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            timeout_seconds=timeout,
            timeout_policy=timeout_policy,
        )
        await asyncio.to_thread(
            self._append_json_line,
            self._paths.commands_file,
            self._paths.commands_lock,
            payload,
        )
        self._submitted_commands[resolved_command_id] = _SubmittedCommand(
            command_id=resolved_command_id,
            submitted_at=datetime.now(tz=UTC),
            timeout_seconds=timeout,
        )
        return RunnerToolCommandResult.running(
            command_id=resolved_command_id,
            transport=TRANSPORT_FILE_COMM,
        )

    async def get_command_status(self, command_id: str) -> RunnerToolCommandResult:
        """Return the current non-blocking status/result for one file-comm command."""
        resolved_command_id = str(command_id or "").strip()
        if not resolved_command_id:
            raise ValueError("command_id must not be empty.")

        cached = self._result_cache.get(resolved_command_id)
        if cached is not None:
            return _command_result_from_bridge_result(cached)

        result_row = await asyncio.to_thread(self._find_result_row, resolved_command_id)
        if result_row is not None:
            result = self._result_from_row(resolved_command_id, result_row)
            if result.status in {STATUS_COMPLETED, STATUS_FAILED}:
                self._result_cache[resolved_command_id] = _bridge_result_from_command_result(result)
                self._submitted_commands.pop(resolved_command_id, None)
            return result

        command_row = await asyncio.to_thread(self._find_command_row, resolved_command_id)
        if command_row is None:
            submitted = self._submitted_commands.get(resolved_command_id)
            if submitted is not None:
                if self._submitted_deadline_passed(submitted):
                    return RunnerToolCommandResult(
                        command_id=resolved_command_id,
                        status=STATUS_TIMED_OUT,
                        success=False,
                        exit_code=-1,
                        stderr=f"Result for command `{resolved_command_id}` not received before timeout.",
                        error_code=ERROR_FILE_COMM_TIMEOUT,
                        error_message="Timed out waiting for file-comm result.",
                        transport=TRANSPORT_FILE_COMM,
                    )
                return RunnerToolCommandResult.running(
                    command_id=resolved_command_id,
                    transport=TRANSPORT_FILE_COMM,
                )
            return RunnerToolCommandResult(
                command_id=resolved_command_id,
                status=STATUS_FAILED,
                success=False,
                exit_code=-1,
                stderr=f"Command `{resolved_command_id}` was not found in file-comm queue.",
                error_code=ERROR_COMMAND_NOT_FOUND,
                error_message="Command was not found.",
                transport=TRANSPORT_FILE_COMM,
            )

        if self._command_deadline_passed(command_row):
            return RunnerToolCommandResult(
                command_id=resolved_command_id,
                status=STATUS_TIMED_OUT,
                success=False,
                exit_code=-1,
                stderr=f"Result for command `{resolved_command_id}` not received before timeout.",
                error_code=ERROR_FILE_COMM_TIMEOUT,
                error_message="Timed out waiting for file-comm result.",
                transport=TRANSPORT_FILE_COMM,
            )

        return RunnerToolCommandResult.running(
            command_id=resolved_command_id,
            transport=TRANSPORT_FILE_COMM,
        )

    async def _execute_command(
        self,
        *,
        command_id: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout_seconds: float,
        timeout_policy: Mapping[str, Any] | None,
    ) -> FileCommBridgeResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0.")
        await self.submit_command(
            command_id=command_id,
            command=command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            timeout_policy=timeout_policy,
        )
        deadline = datetime.now(tz=UTC) + timedelta(seconds=timeout_seconds)
        while datetime.now(tz=UTC) < deadline:
            status = await self.get_command_status(command_id)
            if status.status in {STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMED_OUT}:
                return _bridge_result_from_command_result(status)
            await asyncio.sleep(self._poll_interval_seconds)
        timeout_result = RunnerToolCommandResult(
            command_id=command_id,
            status=STATUS_TIMED_OUT,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"Result for command `{command_id}` not received before timeout.",
            error_code=ERROR_FILE_COMM_TIMEOUT,
            error_message="Timed out waiting for file-comm result.",
            transport=TRANSPORT_FILE_COMM,
        )
        return _bridge_result_from_command_result(timeout_result)

    def _build_command_payload(
        self,
        *,
        command_id: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout_seconds: float,
        timeout_policy: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        resolved_timeout_policy = dict(timeout_policy or {})
        resolved_timeout_policy.setdefault("deadline_seconds", timeout_seconds)
        payload = {
            "id": command_id,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "command": command,
            "cwd": cwd,
            "env": dict(env),
            "timeout": timeout_seconds,
            "timeout_policy": resolved_timeout_policy,
        }
        try:
            CommandMessage.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"Invalid command payload: {exc}") from exc
        return payload

    def _result_from_row(self, command_id: str, row: dict[str, Any]) -> RunnerToolCommandResult:
        try:
            validated = ResultMessage.model_validate(row)
        except ValidationError as exc:
            return RunnerToolCommandResult(
                command_id=command_id,
                status=STATUS_FAILED,
                success=False,
                exit_code=-1,
                stdout="",
                stderr="Malformed result row received from executor.",
                error_code=ERROR_RESULT_MALFORMED,
                error_message=str(exc),
                transport=TRANSPORT_FILE_COMM,
            )

        metadata = dict(validated.metadata)
        status = STATUS_COMPLETED
        failure_category = str(metadata.get("failure_category") or "").strip().lower()
        if (
            failure_category == TOOL_TIMEOUT_FAILURE_CATEGORY
            or validated.exit_code == TOOL_TIMEOUT_EXIT_CODE
        ):
            status = STATUS_TIMED_OUT

        return RunnerToolCommandResult(
            command_id=validated.id,
            status=status,
            success=validated.success,
            exit_code=validated.exit_code,
            stdout=validated.stdout,
            stderr=validated.stderr,
            artifacts=self._normalize_artifacts(validated.artifacts),
            metadata=metadata,
            transport=TRANSPORT_FILE_COMM,
        )

    async def _wait_for_result_row(
        self,
        *,
        command_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        deadline = datetime.now(tz=UTC) + timedelta(seconds=timeout_seconds)
        while datetime.now(tz=UTC) < deadline:
            rows = await asyncio.to_thread(
                self._read_json_rows,
                self._paths.results_file,
                self._paths.results_lock,
            )
            for row in rows:
                if row.get("id") == command_id:
                    return row
            await asyncio.sleep(self._poll_interval_seconds)
        return None

    def _prepare_workspace_files(self) -> None:
        for path in (
            self._paths.commands_file,
            self._paths.results_file,
            self._paths.cancellations_file,
            self._paths.commands_lock,
            self._paths.results_lock,
            self._paths.cancellations_lock,
        ):
            self._filesystem.append_bytes(
                self._relative_workspace_path(path), b"", mode=0o644
            )

    def _command_exists(self, command_id: str) -> bool:
        rows = self._read_json_rows(self._paths.commands_file, self._paths.commands_lock)
        return any(row.get("id") == command_id for row in rows)

    def _find_command_row(self, command_id: str) -> dict[str, Any] | None:
        rows = self._read_json_rows(self._paths.commands_file, self._paths.commands_lock)
        for row in rows:
            if row.get("id") == command_id:
                return row
        return None

    def _find_result_row(self, command_id: str) -> dict[str, Any] | None:
        rows = self._read_json_rows(self._paths.results_file, self._paths.results_lock)
        for row in rows:
            if row.get("id") == command_id:
                return row
        return None

    def _command_deadline_passed(self, command_row: Mapping[str, Any]) -> bool:
        raw_timestamp = str(command_row.get("timestamp") or "").strip()
        try:
            timeout_seconds = float(command_row.get("timeout") or self._default_timeout_seconds)
        except (TypeError, ValueError):
            timeout_seconds = self._default_timeout_seconds
        if timeout_seconds <= 0:
            return True
        if not raw_timestamp:
            return False
        try:
            started_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        return datetime.now(tz=UTC) >= started_at + timedelta(seconds=timeout_seconds)

    def _submitted_deadline_passed(self, command: _SubmittedCommand) -> bool:
        return datetime.now(tz=UTC) >= command.submitted_at + timedelta(
            seconds=command.timeout_seconds
        )

    def _append_json_line(self, file_path: Path, lock_path: Path, payload: Mapping[str, Any]) -> None:
        line = (json.dumps(dict(payload), separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self._filesystem.append_bytes_locked(
            self._relative_workspace_path(file_path),
            self._relative_workspace_path(lock_path),
            line,
            mode=0o644,
        )

    def _read_json_rows(self, file_path: Path, lock_path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            content = self._filesystem.read_bytes_locked(
                self._relative_workspace_path(file_path),
                self._relative_workspace_path(lock_path),
                mode=0o644,
            ).decode("utf-8")
        except FileNotFoundError:
            return []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _relative_workspace_path(self, path: Path) -> str:
        return path.relative_to(self._paths.workspace).as_posix()

    def _normalize_artifacts(self, artifacts: list[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        workspace = self._paths.workspace.resolve()
        for candidate in artifacts:
            raw = str(candidate).strip()
            if not raw:
                continue
            artifact_path = Path(raw)
            if artifact_path.is_absolute():
                try:
                    relative = artifact_path.resolve().relative_to(workspace)
                except ValueError:
                    continue
                normalized.append(relative.as_posix())
                continue
            if ".." in artifact_path.parts:
                continue
            normalized.append(artifact_path.as_posix())
        return tuple(normalized)


def _command_result_from_bridge_result(result: FileCommBridgeResult) -> RunnerToolCommandResult:
    """Convert the legacy bridge result to the transport-neutral command result."""
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


def _bridge_result_from_command_result(result: RunnerToolCommandResult) -> FileCommBridgeResult:
    """Convert a transport-neutral command result to the legacy bridge result."""
    success = bool(result.success)
    exit_code = int(result.exit_code if result.exit_code is not None else (0 if success else -1))
    status = result.status
    if status == STATUS_TIMED_OUT:
        status = STATUS_FAILED
    return FileCommBridgeResult(
        command_id=result.command_id,
        status=status,
        success=success,
        exit_code=exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        artifacts=tuple(result.artifacts),
        error_code=result.error_code,
        error_message=result.error_message,
        metadata=dict(result.metadata),
    )
