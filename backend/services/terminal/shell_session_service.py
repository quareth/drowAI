"""Provider-backed interactive shell session service.

Responsibilities:
- Own public shell-session handles and process-local lifecycle state.
- Validate runtime identity before delegating PTY I/O to TerminalSessionManager.
- Return bounded serializable shell-session updates without storing transcripts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
import logging
import secrets
import shlex
import time
from typing import Any

from backend import config as backend_config
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_session_framing import (
    PTY_EXIT_CODE_MARKER,
    PtyCommandFrame,
    ShellSessionFramingError,
    create_pty_command_frame,
    normalize_pty_output,
    parse_marked_command_output,
    strip_pty_artifacts,
)
from runtime_shared.terminal_contracts import TerminalReadResult

logger = logging.getLogger(__name__)

ContextResolver = Callable[[ShellSessionIdentity], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class ShellSessionServiceConfig:
    """Configuration values injected into ShellSessionService composition."""

    max_active_per_owner: int
    max_active_per_task: int
    idle_timeout_sec: float
    cleanup_interval_sec: float
    termination_grace_sec: float
    terminal_io_grace_sec: float

    @classmethod
    def from_backend_config(cls) -> "ShellSessionServiceConfig":
        """Build service config from the backend configuration authority."""
        return cls(
            max_active_per_owner=backend_config.SHELL_SESSION_MAX_ACTIVE_PER_OWNER,
            max_active_per_task=backend_config.SHELL_SESSION_MAX_ACTIVE_PER_TASK,
            idle_timeout_sec=float(backend_config.SHELL_SESSION_IDLE_TIMEOUT_SEC),
            cleanup_interval_sec=float(
                backend_config.SHELL_SESSION_CLEANUP_INTERVAL_SEC
            ),
            termination_grace_sec=float(
                backend_config.SHELL_SESSION_TERMINATION_GRACE_SEC
            ),
            terminal_io_grace_sec=float(
                backend_config.SHELL_SESSION_TERMINAL_IO_GRACE_SEC
            ),
        )


@dataclass(slots=True)
class ShellSessionRecord:
    """Small process-local control record for one live shell session."""

    public_session_id: str
    terminal_session_id: str
    identity: ShellSessionIdentity
    frame: PtyCommandFrame
    command: str
    process_status: ShellProcessStatus
    created_at: float
    last_activity_at: float
    deadline_at: float
    operation_in_progress: bool = False
    close_requested: bool = False
    pending_utf8_bytes: bytes = b""


class ShellSessionService:
    """Run interactive shell commands through provider-backed terminal sessions."""

    def __init__(
        self,
        *,
        terminal_manager: Any,
        config: ShellSessionServiceConfig | None = None,
        runtime_context_resolver: ContextResolver | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._terminal_manager = terminal_manager
        self._config = config or ShellSessionServiceConfig.from_backend_config()
        self._runtime_context_resolver = (
            runtime_context_resolver or self._default_runtime_context_resolver
        )
        self._clock = clock or time.monotonic
        self._records: dict[str, ShellSessionRecord] = {}
        self._pending_owner_starts: dict[tuple[int, int, str], int] = {}
        self._pending_task_starts: dict[tuple[int, int], int] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    async def execute(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
    ) -> ShellSessionUpdate:
        """Start one PTY shell command and return its first bounded update."""
        started_at = self._clock()
        context_error, workspace_path = await self._validate_runtime_context(identity)
        if context_error is not None:
            return self._error_update(
                error_code=ShellSessionErrorCode.COMMAND_START_FAILED,
                duration_ms=self._duration_ms(started_at),
            )

        async with self._lock:
            if self._active_owner_count(identity) >= self._config.max_active_per_owner:
                return self._error_update(
                    error_code=ShellSessionErrorCode.SESSION_LIMIT_REACHED,
                    duration_ms=self._duration_ms(started_at),
                )
            if self._active_task_count(identity) >= self._config.max_active_per_task:
                return self._error_update(
                    error_code=ShellSessionErrorCode.SESSION_LIMIT_REACHED,
                    duration_ms=self._duration_ms(started_at),
                )

        public_session_id = self._generate_public_session_id()
        frame = create_pty_command_frame(self._prepare_command(request))
        owner_key: tuple[int, int, str] | None = None
        task_key: tuple[int, int] | None = None
        terminal_session_id: str | None = None
        registered = False

        async with self._lock:
            owner_key, task_key = self._reserve_start_capacity_locked(identity)
            if owner_key is None or task_key is None:
                return self._error_update(
                    error_code=ShellSessionErrorCode.SESSION_LIMIT_REACHED,
                    duration_ms=self._duration_ms(started_at),
                )

        try:
            terminal_session = await self._prepare_reserved_terminal(
                identity=identity,
                request=request,
                workspace_path=workspace_path,
                public_session_id=public_session_id,
                frame=frame,
                owner_key=owner_key,
                task_key=task_key,
            )
            terminal_session_id, record = terminal_session
            registered = True
            owner_key = None
            task_key = None
            if not await self._terminal_manager.send_input(
                terminal_session_id, frame.wrapped_command.encode()
            ):
                raise RuntimeError("send_terminal_input failed")

            return await self._read_update(
                record=record,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=request.max_output_chars,
                started_at=started_at,
            )
        except asyncio.CancelledError:
            if registered and public_session_id:
                await self._remove_and_close_record(public_session_id, interrupt=True)
            elif terminal_session_id:
                await self._close_terminal(terminal_session_id, interrupt=True)
            elif owner_key is not None and task_key is not None:
                await self._release_start_capacity(owner_key, task_key)
            raise
        except Exception as exc:
            logger.info(
                "shell_session.execute failed task_id=%s owner=%s session=%s error=%s",
                identity.task_id,
                identity.execution_owner_id,
                self._fingerprint(public_session_id),
                type(exc).__name__,
            )
            if registered:
                await self._remove_and_close_record(public_session_id, interrupt=False)
            elif terminal_session_id:
                await self._close_terminal(terminal_session_id, interrupt=False)
            elif owner_key is not None and task_key is not None:
                await self._release_start_capacity(owner_key, task_key)
            return self._error_update(
                error_code=ShellSessionErrorCode.COMMAND_START_FAILED,
                duration_ms=self._duration_ms(started_at),
            )

    async def write_stdin(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellWriteRequest,
    ) -> ShellSessionUpdate:
        """Poll, write exact input to, or interrupt an existing shell session."""
        started_at = self._clock()
        record, error_code = await self._claim_existing_record(
            identity=identity,
            public_session_id=request.session_id,
        )
        if record is None:
            if error_code is ShellSessionErrorCode.COMMAND_TIMED_OUT:
                return self._timeout_update(
                    stdout="",
                    truncated=False,
                    duration_ms=self._duration_ms(started_at),
                )
            return self._error_update(error_code=error_code, duration_ms=0)

        try:
            if request.chars == "\u0003":
                if not await self._terminal_manager.send_input(
                    record.terminal_session_id, request.chars.encode()
                ):
                    return await self._fail_claimed_record(
                        record,
                        ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED,
                        started_at,
                    )
                await asyncio.sleep(self._config.termination_grace_sec)
                await self._remove_and_close_record(
                    record.public_session_id,
                    interrupt=False,
                    expected_record=record,
                )
                return ShellSessionUpdate(
                    success=True,
                    status="success",
                    process_status=ShellProcessStatus.TERMINATED,
                    session_id=None,
                    stdout="",
                    stderr="",
                    exit_code=None,
                    stdin_available=False,
                    truncated=False,
                    duration_ms=self._duration_ms(started_at),
                    summary="Command was interrupted.",
                )

            if request.chars:
                if not await self._terminal_manager.send_input(
                    record.terminal_session_id, request.chars.encode()
                ):
                    return await self._fail_claimed_record(
                        record,
                        ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED,
                        started_at,
                    )
                record.last_activity_at = self._clock()

            return await self._read_update(
                record=record,
                yield_time_ms=request.yield_time_ms,
                max_output_chars=request.max_output_chars,
                started_at=started_at,
            )
        except asyncio.CancelledError:
            await self._release_record(record)
            raise
        except Exception:
            return await self._fail_claimed_record(
                record,
                ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED,
                started_at,
            )

    async def close_owner_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> None:
        """Idempotently close every session for one execution owner."""
        records = await self._pop_matching_records(
            lambda record: (
                record.identity.tenant_id == int(tenant_id)
                and record.identity.task_id == int(task_id)
                and record.identity.execution_owner_id == execution_owner_id
            )
        )
        await self._close_records(records, interrupt=True)

    async def close_task_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> None:
        """Idempotently close every shell session for one task."""
        records = await self._pop_matching_records(
            lambda record: (
                record.identity.tenant_id == int(tenant_id)
                and record.identity.task_id == int(task_id)
            )
        )
        await self._close_records(records, interrupt=True)

    def start(self) -> None:
        """Start process-local stale-session cleanup."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop process-local stale-session cleanup."""
        task = self._cleanup_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._cleanup_task = None

    async def cleanup_stale_sessions(self) -> None:
        """Close idle or deadline-expired sessions through the common close path."""
        now = self._clock()
        records = await self._pop_matching_records(
            lambda record: self._is_deadline_expired(record, now)
            or self._is_idle_expired(record, now)
        )
        await self._close_records(records, interrupt=True)

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.cleanup_interval_sec)
                await self.cleanup_stale_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("shell session cleanup loop failed: %s", exc)

    async def _read_update(
        self,
        *,
        record: ShellSessionRecord,
        yield_time_ms: int,
        max_output_chars: int,
        started_at: float,
    ) -> ShellSessionUpdate:
        deadline = self._clock() + (float(yield_time_ms) / 1000.0)
        raw_parts: list[str] = []
        while True:
            now = self._clock()
            if self._is_deadline_expired(record, now):
                await self._remove_and_close_record(
                    record.public_session_id,
                    interrupt=True,
                    expected_record=record,
                )
                stdout, truncated = self._bounded_text(
                    "".join(raw_parts),
                    max_output_chars,
                )
                return self._timeout_update(
                    stdout=stdout,
                    truncated=truncated,
                    duration_ms=self._duration_ms(started_at),
                )

            remaining = max(0.0, deadline - now)
            if yield_time_ms == 0 and raw_parts:
                break
            if remaining <= 0.0 and yield_time_ms > 0:
                break

            read_timeout = min(self._config.terminal_io_grace_sec, remaining)
            if yield_time_ms == 0:
                read_timeout = 0.0
            result = await self._terminal_manager.read_output_result(
                record.terminal_session_id,
                4096,
                timeout=read_timeout,
            )
            if not isinstance(result, TerminalReadResult):
                result = TerminalReadResult(ok=True, data=getattr(result, "data", b""))
            if not result.ok:
                return await self._fail_claimed_record(
                    record,
                    ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED,
                    started_at,
                )
            if not result.data:
                if yield_time_ms == 0 or self._clock() >= deadline:
                    break
                continue
            record.last_activity_at = self._clock()
            raw_parts.append(self._decode_bytes(record, result.data))
            combined = "".join(raw_parts)
            try:
                completion = self._extract_completion(record, combined, max_output_chars)
            except ValueError:
                return await self._fail_claimed_record(
                    record,
                    ShellSessionErrorCode.COMMAND_OUTPUT_INVALID,
                    started_at,
                )
            if completion is not None:
                stdout, exit_code, truncated = completion
                await self._remove_and_close_record(
                    record.public_session_id,
                    interrupt=False,
                    expected_record=record,
                )
                return ShellSessionUpdate(
                    success=exit_code == 0,
                    status="success" if exit_code == 0 else "error",
                    process_status=ShellProcessStatus.COMPLETED,
                    session_id=None,
                    stdout=stdout,
                    stderr="",
                    exit_code=exit_code,
                    stdin_available=False,
                    truncated=truncated,
                    duration_ms=self._duration_ms(started_at),
                    summary=(
                        "Command completed successfully."
                        if exit_code == 0
                        else f"Command completed with exit code {exit_code}."
                    ),
                )
            if len(combined) >= max_output_chars:
                break

        stdout, truncated = self._bounded_text(
            self._visible_output(record, "".join(raw_parts)),
            max_output_chars,
        )
        await self._release_record(record)
        return ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.RUNNING,
            session_id=record.public_session_id,
            stdout=stdout,
            stderr="",
            exit_code=None,
            stdin_available=True,
            truncated=truncated,
            duration_ms=self._duration_ms(started_at),
        )

    def _extract_completion(
        self,
        record: ShellSessionRecord,
        raw_output: str,
        max_output_chars: int,
    ) -> tuple[str, int, bool] | None:
        normalized = normalize_pty_output(raw_output)
        if record.frame.end_marker not in normalized:
            return None
        parser_input = raw_output
        if record.frame.start_marker not in normalized:
            parser_input = f"{record.frame.start_marker}\n{raw_output}"
        try:
            visible, exit_code = parse_marked_command_output(
                parser_input,
                record.frame.start_marker,
                record.frame.end_marker,
            )
        except ShellSessionFramingError as exc:
            raise ValueError(str(exc)) from exc
        stdout, truncated = self._bounded_text(visible, max_output_chars)
        return stdout, exit_code, truncated

    def _visible_output(self, record: ShellSessionRecord, raw_output: str) -> str:
        visible = strip_pty_artifacts(raw_output, record.frame.wrapped_command)
        visible = visible.replace(record.frame.start_marker, "")
        visible = visible.replace(record.frame.end_marker, "")
        lines = []
        for line in visible.split("\n"):
            if PTY_EXIT_CODE_MARKER in line:
                continue
            if not line.strip() and not lines:
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    async def _claim_existing_record(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> tuple[ShellSessionRecord | None, ShellSessionErrorCode]:
        normalized_id = str(public_session_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized_id)
            if record is None or not self._same_owner(record.identity, identity):
                return None, ShellSessionErrorCode.SESSION_UNAVAILABLE
            now = self._clock()
            if self._is_deadline_expired(record, now):
                self._records.pop(normalized_id, None)
                record.close_requested = True
                close_record = record
                error_code = ShellSessionErrorCode.COMMAND_TIMED_OUT
            elif self._is_idle_expired(record, now):
                self._records.pop(normalized_id, None)
                record.close_requested = True
                close_record = record
                error_code = ShellSessionErrorCode.SESSION_UNAVAILABLE
            elif record.operation_in_progress:
                return None, ShellSessionErrorCode.SESSION_BUSY
            else:
                record.operation_in_progress = True
                return record, ShellSessionErrorCode.SESSION_UNAVAILABLE
        await self._close_records([close_record], interrupt=True)
        return None, error_code

    async def _prepare_reserved_terminal(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        workspace_path: str | None,
        public_session_id: str,
        frame: PtyCommandFrame,
        owner_key: tuple[int, int, str],
        task_key: tuple[int, int],
    ) -> tuple[str, ShellSessionRecord]:
        terminal_session_id: str | None = None
        try:
            terminal_session = await self._terminal_manager.prepare_agent_session(
                task_id=identity.task_id,
                workspace_path=workspace_path,
                session_name=f"shell_{public_session_id}",
                reset=True,
            )
            terminal_session_id = str(terminal_session.session_id)
            now = self._clock()
            record = ShellSessionRecord(
                public_session_id=public_session_id,
                terminal_session_id=terminal_session_id,
                identity=identity,
                frame=frame,
                command=request.command,
                process_status=ShellProcessStatus.RUNNING,
                created_at=now,
                last_activity_at=now,
                deadline_at=now + float(request.max_runtime_sec),
                operation_in_progress=True,
            )
            async with self._lock:
                self._records[public_session_id] = record
                self._release_start_capacity_locked(owner_key, task_key)
            return terminal_session_id, record
        except asyncio.CancelledError:
            if terminal_session_id is not None:
                await self._close_terminal(terminal_session_id, interrupt=True)
            await self._release_start_capacity(owner_key, task_key)
            raise
        except Exception:
            if terminal_session_id is not None:
                await self._close_terminal(terminal_session_id, interrupt=False)
            await self._release_start_capacity(owner_key, task_key)
            raise

    async def _release_record(self, record: ShellSessionRecord) -> None:
        async with self._lock:
            current = self._records.get(record.public_session_id)
            if current is record:
                record.operation_in_progress = False

    async def _release_start_capacity(
        self,
        owner_key: tuple[int, int, str],
        task_key: tuple[int, int],
    ) -> None:
        async with self._lock:
            self._release_start_capacity_locked(owner_key, task_key)

    async def _fail_claimed_record(
        self,
        record: ShellSessionRecord,
        error_code: ShellSessionErrorCode,
        started_at: float,
    ) -> ShellSessionUpdate:
        await self._remove_and_close_record(
            record.public_session_id,
            interrupt=False,
            expected_record=record,
        )
        return self._error_update(
            error_code=error_code,
            duration_ms=self._duration_ms(started_at),
        )

    async def _remove_and_close_record(
        self,
        public_session_id: str,
        *,
        interrupt: bool,
        expected_record: ShellSessionRecord | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(public_session_id)
            if record is None:
                return
            if expected_record is not None and record is not expected_record:
                return
            self._records.pop(public_session_id, None)
            record.close_requested = True
            record.operation_in_progress = False
        await self._close_records([record], interrupt=interrupt)

    async def _pop_matching_records(
        self,
        predicate: Callable[[ShellSessionRecord], bool],
    ) -> list[ShellSessionRecord]:
        async with self._lock:
            records = [
                record
                for record in self._records.values()
                if predicate(record)
            ]
            for record in records:
                self._records.pop(record.public_session_id, None)
                record.close_requested = True
                record.operation_in_progress = False
            return records

    async def _close_records(
        self,
        records: list[ShellSessionRecord],
        *,
        interrupt: bool,
    ) -> None:
        for record in records:
            await self._close_terminal(record.terminal_session_id, interrupt=interrupt)

    async def _close_terminal(self, terminal_session_id: str, *, interrupt: bool) -> None:
        if interrupt:
            try:
                await self._terminal_manager.send_input(terminal_session_id, b"\x03")
                await asyncio.sleep(self._config.termination_grace_sec)
            except Exception:
                pass
        try:
            await self._terminal_manager.close_session(terminal_session_id)
        except Exception:
            pass

    async def _validate_runtime_context(
        self,
        identity: ShellSessionIdentity,
    ) -> tuple[ShellSessionErrorCode | None, str | None]:
        try:
            context = self._runtime_context_resolver(identity)
            if inspect.isawaitable(context):
                context = await context
        except Exception:
            return ShellSessionErrorCode.COMMAND_START_FAILED, None

        context_workspace_path = getattr(context, "workspace_path", None)
        checks = [
            int(getattr(context, "tenant_id", -1)) == int(identity.tenant_id),
            int(getattr(context, "task_id", -1)) == int(identity.task_id),
            str(getattr(context, "workspace_id", "")) == identity.workspace_id,
            str(getattr(context, "runtime_placement_mode", ""))
            == identity.runtime_placement_mode,
        ]
        if context_workspace_path is not None:
            checks.append(str(context_workspace_path) == str(identity.workspace_path))
        if identity.runner_id is not None:
            checks.append(str(getattr(context, "runner_id", "")) == identity.runner_id)
        if identity.execution_site_id is not None:
            checks.append(
                str(getattr(context, "execution_site_id", ""))
                == identity.execution_site_id
            )
        error = None if all(checks) else ShellSessionErrorCode.COMMAND_START_FAILED
        workspace_path = (
            str(context_workspace_path)
            if context_workspace_path is not None
            else identity.workspace_path
        )
        return error, workspace_path

    @staticmethod
    def _default_runtime_context_resolver(identity: ShellSessionIdentity) -> Any:
        from backend.database import SessionLocal
        from backend.services.runtime_provider import RuntimeActorType
        from backend.services.runtime_provider.context import RuntimeProviderContextResolver

        db = SessionLocal()
        try:
            resolver = RuntimeProviderContextResolver(db)
            return resolver.resolve_internal_task_context(
                task_id=identity.task_id,
                actor_type=RuntimeActorType.AGENT,
                actor_id=f"shell_session:{identity.execution_owner_id}",
            )
        finally:
            db.close()

    def _prepare_command(self, request: ShellExecRequest) -> str:
        parts: list[str] = []
        if request.cwd:
            parts.append(f"cd -- {shlex.quote(request.cwd)}")
        command = f"bash -lc {shlex.quote(request.command)}"
        if request.env:
            env_args = " ".join(
                shlex.quote(f"{key}={value}")
                for key, value in sorted(request.env.items())
            )
            command = f"env {env_args} {command}"
        parts.append(command)
        return " && ".join(parts)

    def _decode_bytes(self, record: ShellSessionRecord, chunk: bytes) -> str:
        data = record.pending_utf8_bytes + bytes(chunk)
        try:
            record.pending_utf8_bytes = b""
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            safe = data[: exc.start]
            record.pending_utf8_bytes = data[exc.start:][-4:]
            return safe.decode("utf-8", errors="replace")

    def _active_owner_count(self, identity: ShellSessionIdentity) -> int:
        return sum(
            1
            for record in self._records.values()
            if self._same_owner(record.identity, identity)
        )

    def _active_task_count(self, identity: ShellSessionIdentity) -> int:
        return sum(
            1
            for record in self._records.values()
            if record.identity.tenant_id == identity.tenant_id
            and record.identity.task_id == identity.task_id
        )

    def _reserve_start_capacity_locked(
        self,
        identity: ShellSessionIdentity,
    ) -> tuple[tuple[int, int, str] | None, tuple[int, int] | None]:
        owner_key = self._owner_start_key(identity)
        task_key = self._task_start_key(identity)
        owner_pending = self._pending_owner_starts.get(owner_key, 0)
        task_pending = self._pending_task_starts.get(task_key, 0)
        if (
            self._active_owner_count(identity) + owner_pending
            >= self._config.max_active_per_owner
        ):
            return None, None
        if (
            self._active_task_count(identity) + task_pending
            >= self._config.max_active_per_task
        ):
            return None, None

        self._pending_owner_starts[owner_key] = owner_pending + 1
        self._pending_task_starts[task_key] = task_pending + 1
        return owner_key, task_key

    def _release_start_capacity_locked(
        self,
        owner_key: tuple[int, int, str],
        task_key: tuple[int, int],
    ) -> None:
        owner_pending = self._pending_owner_starts.get(owner_key, 0)
        if owner_pending <= 1:
            self._pending_owner_starts.pop(owner_key, None)
        else:
            self._pending_owner_starts[owner_key] = owner_pending - 1

        task_pending = self._pending_task_starts.get(task_key, 0)
        if task_pending <= 1:
            self._pending_task_starts.pop(task_key, None)
        else:
            self._pending_task_starts[task_key] = task_pending - 1

    @staticmethod
    def _owner_start_key(identity: ShellSessionIdentity) -> tuple[int, int, str]:
        return (
            int(identity.tenant_id),
            int(identity.task_id),
            identity.execution_owner_id,
        )

    @staticmethod
    def _task_start_key(identity: ShellSessionIdentity) -> tuple[int, int]:
        return (int(identity.tenant_id), int(identity.task_id))

    @staticmethod
    def _same_owner(
        left: ShellSessionIdentity,
        right: ShellSessionIdentity,
    ) -> bool:
        return (
            left.tenant_id == right.tenant_id
            and left.task_id == right.task_id
            and left.execution_owner_id == right.execution_owner_id
        )

    def _is_deadline_expired(self, record: ShellSessionRecord, now: float) -> bool:
        return now >= record.deadline_at

    def _is_idle_expired(self, record: ShellSessionRecord, now: float) -> bool:
        return now - record.last_activity_at >= self._config.idle_timeout_sec

    def _duration_ms(self, started_at: float) -> int:
        return max(0, int((self._clock() - started_at) * 1000))

    @staticmethod
    def _generate_public_session_id() -> str:
        return f"shs_{secrets.token_urlsafe(12)}"

    @staticmethod
    def _fingerprint(public_session_id: str) -> str:
        return public_session_id[:12]

    @staticmethod
    def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
        if len(value) <= limit:
            return value, False
        marker = "\n[... shell output truncated ...]\n"
        if limit <= len(marker):
            return value[:limit], True
        head_len = max(0, (limit - len(marker)) // 2)
        tail_len = max(0, limit - len(marker) - head_len)
        return f"{value[:head_len]}{marker}{value[-tail_len:]}", True

    @staticmethod
    def _error_update(
        *,
        error_code: ShellSessionErrorCode,
        duration_ms: int,
    ) -> ShellSessionUpdate:
        return ShellSessionUpdate(
            success=False,
            status="error",
            process_status=None,
            session_id=None,
            stdout="",
            stderr="",
            exit_code=None,
            stdin_available=False,
            truncated=False,
            duration_ms=duration_ms,
            error_code=error_code,
        )

    @staticmethod
    def _timeout_update(
        *,
        stdout: str,
        truncated: bool,
        duration_ms: int,
    ) -> ShellSessionUpdate:
        return ShellSessionUpdate(
            success=False,
            status="error",
            process_status=ShellProcessStatus.TIMED_OUT,
            session_id=None,
            stdout=stdout,
            stderr="",
            exit_code=None,
            stdin_available=False,
            truncated=truncated,
            duration_ms=duration_ms,
            error_code=ShellSessionErrorCode.COMMAND_TIMED_OUT,
            summary="Command exceeded its configured maximum runtime.",
        )
