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
import hashlib
import inspect
import logging
import secrets
import shlex
import time
from typing import Any

from backend import config as backend_config
from backend.services.metrics.utils import safe_gauge, safe_inc
from runtime_shared.docker_contracts import CONTAINER_WORKSPACE_PATH
from runtime_shared.shell_session_contracts import (
    SHELL_SESSION_CLEANUP_TIMEOUT_SEC,
    SHELL_SESSION_CONTROL_TIMEOUT_SEC,
    SHELL_SESSION_PREPARATION_TIMEOUT_SEC,
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_framing import (
    PtyCommandFrame,
    StreamingPtyFramingParser,
    create_pty_command_frame,
)
from runtime_shared.terminal_contracts import TerminalReadResult
from .shell_session_output import ShellSessionOutputAccumulator

logger = logging.getLogger(__name__)

_SHELL_SESSION_ERROR_MESSAGES = {
    ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE: (
        "Shell runtime is unavailable for this task."
    ),
    ShellSessionErrorCode.SESSION_LIMIT_REACHED: (
        "Shell session limit reached; close an active session before retrying."
    ),
    ShellSessionErrorCode.SESSION_UNAVAILABLE: (
        "Shell session is unavailable or does not belong to this execution."
    ),
    ShellSessionErrorCode.SESSION_BUSY: (
        "Shell session already has an operation in progress."
    ),
    ShellSessionErrorCode.COMMAND_START_FAILED: "Shell command could not be started.",
    ShellSessionErrorCode.COMMAND_OUTPUT_INVALID: (
        "Shell command output did not match the session framing protocol."
    ),
    ShellSessionErrorCode.COMMAND_TIMED_OUT: (
        "Command exceeded its configured maximum runtime."
    ),
    ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED: (
        "Shell runtime transport failed while processing the session."
    ),
}

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
    originating_capability: ShellCapability
    framing_parser: StreamingPtyFramingParser
    last_activity_at: float
    deadline_at: float
    operation_in_progress: bool = False
    pending_utf8_bytes: bytes = b""


@dataclass(slots=True)
class _StartCapacityReservation:
    """Idempotent pending-capacity lease for one shell-session start."""

    owner_key: tuple[int, int, str]
    task_key: tuple[int, int]
    released: bool = False


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
        capability: ShellCapability = ShellCapability.ASSESSMENT,
    ) -> ShellSessionUpdate:
        """Start one PTY shell command and return its first bounded update."""
        started_at = self._clock()
        context_error, terminal_workspace_path = await self._validate_runtime_context(
            identity
        )
        if context_error is not None:
            self._emit_operation_failed(identity, context_error)
            return self._error_update(
                error_code=context_error,
                duration_ms=self._duration_ms(started_at),
            )

        public_session_id = self._generate_public_session_id()
        frame = create_pty_command_frame(self._prepare_command(request))
        reservation: _StartCapacityReservation | None = None
        terminal_session_id: str | None = None
        registered = False
        start_error_code = ShellSessionErrorCode.COMMAND_START_FAILED

        async with self._lock:
            reservation = self._reserve_start_capacity_locked(identity)
            if reservation is None:
                self._emit_operation_failed(
                    identity,
                    ShellSessionErrorCode.SESSION_LIMIT_REACHED,
                )
                return self._error_update(
                    error_code=ShellSessionErrorCode.SESSION_LIMIT_REACHED,
                    duration_ms=self._duration_ms(started_at),
                )

        try:
            async with asyncio.timeout(SHELL_SESSION_PREPARATION_TIMEOUT_SEC):
                terminal_session = await self._prepare_reserved_terminal(
                    identity=identity,
                    request=request,
                    terminal_workspace_path=terminal_workspace_path,
                    public_session_id=public_session_id,
                    frame=frame,
                    capability=capability,
                    reservation=reservation,
                )
                terminal_session_id, record = terminal_session
                registered = True
                start_error_code = ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED
                if not await self._send_input_with_deadline(
                    terminal_session_id,
                    frame.wrapped_command.encode(),
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
                await self._remove_and_close_record(
                    public_session_id,
                    interrupt=True,
                    close_reason="cancelled",
                )
            elif terminal_session_id:
                await self._close_terminal(terminal_session_id, interrupt=True)
            raise
        except Exception:
            self._emit_operation_failed(
                identity,
                start_error_code,
                public_session_id=public_session_id,
            )
            if registered:
                await self._remove_and_close_record(
                    public_session_id,
                    interrupt=False,
                    close_reason="start_failed",
                )
            elif terminal_session_id:
                await self._close_terminal(terminal_session_id, interrupt=False)
            return self._error_update(
                error_code=start_error_code,
                duration_ms=self._duration_ms(started_at),
            )
        finally:
            await self._release_start_capacity(reservation)

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
            self._emit_operation_failed(
                identity,
                error_code,
                public_session_id=request.session_id,
            )
            if error_code is ShellSessionErrorCode.COMMAND_TIMED_OUT:
                return self._timeout_update(
                    stdout="",
                    truncated=False,
                    duration_ms=self._duration_ms(started_at),
                )
            return self._error_update(error_code=error_code, duration_ms=0)

        try:
            if request.chars == "\u0003":
                if not await self._send_input_with_deadline(
                    record.terminal_session_id,
                    request.chars.encode(),
                ):
                    return await self._fail_claimed_record(
                        record,
                        ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED,
                        started_at,
                    )
                return await self._read_update(
                    record=record,
                    yield_time_ms=int(self._config.termination_grace_sec * 1000),
                    max_output_chars=request.max_output_chars,
                    started_at=started_at,
                    terminate_after_window=True,
                )

            if request.chars:
                if not await self._send_input_with_deadline(
                    record.terminal_session_id,
                    request.chars.encode(),
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

    async def get_session_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None:
        """Return the capability of an owned live session without claiming it."""

        normalized_id = str(public_session_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized_id)
            if record is None or not self._same_owner(record.identity, identity):
                return None
            now = self._clock()
            if self._is_deadline_expired(record, now) or self._is_idle_expired(
                record, now
            ):
                return None
            return record.originating_capability

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
        await self._close_records(
            records,
            interrupt=True,
            close_reason="owner_cleanup",
        )

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
        await self._close_records(
            records,
            interrupt=True,
            close_reason="task_cleanup",
        )

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
        records = await self._pop_stale_records(now)
        for record, close_reason in records:
            if close_reason == "deadline_expired":
                self._emit_process_completed(record, ShellProcessStatus.TIMED_OUT)
            await self._close_records(
                [record],
                interrupt=True,
                close_reason=close_reason,
            )

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
        terminate_after_window: bool = False,
    ) -> ShellSessionUpdate:
        deadline = self._clock() + (float(yield_time_ms) / 1000.0)
        output = ShellSessionOutputAccumulator(
            parser=record.framing_parser,
            max_output_chars=max_output_chars,
        )
        while True:
            now = self._clock()
            if self._is_deadline_expired(record, now):
                self._emit_process_completed(record, ShellProcessStatus.TIMED_OUT)
                self._emit_operation_failed(
                    record.identity,
                    ShellSessionErrorCode.COMMAND_TIMED_OUT,
                    public_session_id=record.public_session_id,
                )
                await self._remove_and_close_record(
                    record.public_session_id,
                    interrupt=True,
                    expected_record=record,
                    close_reason="deadline_expired",
                )
                stdout, truncated = output.stdout()
                return self._timeout_update(
                    stdout=stdout,
                    truncated=truncated,
                    duration_ms=self._duration_ms(started_at),
                )

            remaining = max(0.0, deadline - now)
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
            if result.truncated:
                output.mark_provider_output_truncated()
            if not result.data:
                if yield_time_ms == 0 or self._clock() >= deadline:
                    break
                continue
            record.last_activity_at = self._clock()
            try:
                completion = output.ingest(self._decode_bytes(record, result.data))
            except ValueError:
                return await self._fail_claimed_record(
                    record,
                    ShellSessionErrorCode.COMMAND_OUTPUT_INVALID,
                    started_at,
                )
            if completion is not None:
                if terminate_after_window:
                    break
                stdout, truncated = output.stdout()
                exit_code = completion.exit_code
                self._emit_process_completed(record, ShellProcessStatus.COMPLETED)
                await self._remove_and_close_record(
                    record.public_session_id,
                    interrupt=False,
                    expected_record=record,
                    close_reason="process_completed",
                )
                return ShellSessionUpdate(
                    success=exit_code == 0,
                    status="success" if exit_code == 0 else "error",
                    process_status=ShellProcessStatus.COMPLETED,
                    session_id=None,
                    stdout=stdout,
                    stderr=(
                        ""
                        if exit_code == 0 or stdout
                        else f"Command exited with code {exit_code}."
                    ),
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

        stdout, truncated = output.stdout()
        if terminate_after_window:
            self._emit_process_completed(record, ShellProcessStatus.TERMINATED)
            await self._remove_and_close_record(
                record.public_session_id,
                interrupt=False,
                expected_record=record,
                close_reason="interrupted",
            )
            return ShellSessionUpdate(
                success=True,
                status="success",
                process_status=ShellProcessStatus.TERMINATED,
                session_id=None,
                stdout=stdout,
                stderr="",
                exit_code=None,
                stdin_available=False,
                truncated=truncated,
                duration_ms=self._duration_ms(started_at),
                summary="Command was interrupted.",
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
                self._emit_active_session_gauges(record.identity.runtime_placement_mode)
                self._emit_process_completed(record, ShellProcessStatus.TIMED_OUT)
                close_record = record
                error_code = ShellSessionErrorCode.COMMAND_TIMED_OUT
                close_reason = "deadline_expired"
            elif self._is_idle_expired(record, now):
                self._records.pop(normalized_id, None)
                self._emit_active_session_gauges(record.identity.runtime_placement_mode)
                close_record = record
                error_code = ShellSessionErrorCode.SESSION_UNAVAILABLE
                close_reason = "idle_expired"
            elif record.operation_in_progress:
                return None, ShellSessionErrorCode.SESSION_BUSY
            else:
                record.operation_in_progress = True
                return record, ShellSessionErrorCode.SESSION_UNAVAILABLE
        await self._close_records(
            [close_record],
            interrupt=True,
            close_reason=close_reason,
        )
        return None, error_code

    async def _prepare_reserved_terminal(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        terminal_workspace_path: str | None,
        public_session_id: str,
        frame: PtyCommandFrame,
        capability: ShellCapability,
        reservation: _StartCapacityReservation,
    ) -> tuple[str, ShellSessionRecord]:
        terminal_session_id: str | None = None
        try:
            terminal_session = await self._terminal_manager.prepare_agent_session(
                task_id=identity.task_id,
                workspace_path=terminal_workspace_path,
                session_name=f"shell_{public_session_id}",
                reset=True,
            )
            terminal_session_id = str(terminal_session.session_id)
            now = self._clock()
            record = ShellSessionRecord(
                public_session_id=public_session_id,
                terminal_session_id=terminal_session_id,
                identity=identity,
                originating_capability=capability,
                framing_parser=StreamingPtyFramingParser(frame),
                last_activity_at=now,
                deadline_at=now + float(request.max_runtime_sec),
                operation_in_progress=True,
            )
            async with self._lock:
                self._records[public_session_id] = record
                self._release_start_capacity_locked(reservation)
                self._emit_session_opened(record)
            return terminal_session_id, record
        except asyncio.CancelledError:
            if terminal_session_id is not None:
                await self._close_terminal(terminal_session_id, interrupt=True)
            raise
        except Exception:
            if terminal_session_id is not None:
                await self._close_terminal(terminal_session_id, interrupt=False)
            raise

    async def _release_record(self, record: ShellSessionRecord) -> None:
        async with self._lock:
            current = self._records.get(record.public_session_id)
            if current is record:
                record.operation_in_progress = False

    async def _send_input_with_deadline(
        self,
        terminal_session_id: str,
        data: bytes,
    ) -> bool:
        """Send one shell control write within the shared invocation budget."""
        async with asyncio.timeout(SHELL_SESSION_CONTROL_TIMEOUT_SEC):
            return bool(
                await self._terminal_manager.send_input(terminal_session_id, data)
            )

    async def _release_start_capacity(
        self,
        reservation: _StartCapacityReservation | None,
    ) -> None:
        if reservation is None or reservation.released:
            return
        async with self._lock:
            self._release_start_capacity_locked(reservation)

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
            close_reason="operation_failed",
        )
        self._emit_operation_failed(
            record.identity,
            error_code,
            public_session_id=record.public_session_id,
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
        close_reason: str,
        expected_record: ShellSessionRecord | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(public_session_id)
            if record is None:
                return
            if expected_record is not None and record is not expected_record:
                return
            self._records.pop(public_session_id, None)
            record.operation_in_progress = False
            self._emit_active_session_gauges(record.identity.runtime_placement_mode)
        await self._close_records(
            [record],
            interrupt=interrupt,
            close_reason=close_reason,
        )

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
                record.operation_in_progress = False
            self._emit_active_session_gauges(
                *(record.identity.runtime_placement_mode for record in records)
            )
            return records

    async def _pop_stale_records(
        self,
        now: float,
    ) -> list[tuple[ShellSessionRecord, str]]:
        async with self._lock:
            records: list[tuple[ShellSessionRecord, str]] = []
            for record in self._records.values():
                if self._is_deadline_expired(record, now):
                    records.append((record, "deadline_expired"))
                elif self._is_idle_expired(record, now):
                    records.append((record, "idle_expired"))
            for record, _close_reason in records:
                self._records.pop(record.public_session_id, None)
                record.operation_in_progress = False
            self._emit_active_session_gauges(
                *(
                    record.identity.runtime_placement_mode
                    for record, _close_reason in records
                )
            )
            return records

    async def _close_records(
        self,
        records: list[ShellSessionRecord],
        *,
        interrupt: bool,
        close_reason: str,
    ) -> None:
        for record in records:
            await self._close_terminal(record.terminal_session_id, interrupt=interrupt)
            self._emit_session_closed(record, close_reason)

    async def _close_terminal(self, terminal_session_id: str, *, interrupt: bool) -> None:
        try:
            async with asyncio.timeout(SHELL_SESSION_CLEANUP_TIMEOUT_SEC):
                if interrupt:
                    try:
                        await self._send_input_with_deadline(
                            terminal_session_id,
                            b"\x03",
                        )
                        await asyncio.sleep(self._config.termination_grace_sec)
                    except Exception:
                        pass
                try:
                    await self._terminal_manager.close_session(terminal_session_id)
                except Exception:
                    pass
        except TimeoutError:
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
            return ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE, None

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
        terminal_workspace_path = (
            CONTAINER_WORKSPACE_PATH if error is None else None
        )
        return error, terminal_workspace_path

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
    ) -> _StartCapacityReservation | None:
        owner_key = self._owner_start_key(identity)
        task_key = self._task_start_key(identity)
        owner_pending = self._pending_owner_starts.get(owner_key, 0)
        task_pending = self._pending_task_starts.get(task_key, 0)
        if (
            self._active_owner_count(identity) + owner_pending
            >= self._config.max_active_per_owner
        ):
            return None
        if (
            self._active_task_count(identity) + task_pending
            >= self._config.max_active_per_task
        ):
            return None

        self._pending_owner_starts[owner_key] = owner_pending + 1
        self._pending_task_starts[task_key] = task_pending + 1
        return _StartCapacityReservation(owner_key=owner_key, task_key=task_key)

    def _release_start_capacity_locked(
        self,
        reservation: _StartCapacityReservation,
    ) -> None:
        if reservation.released:
            return
        reservation.released = True
        owner_key = reservation.owner_key
        task_key = reservation.task_key
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

    def _emit_session_opened(self, record: ShellSessionRecord) -> None:
        placement = self._placement(record.identity.runtime_placement_mode)
        logger.info(
            (
                "shell_session event=session_opened tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s"
            ),
            record.identity.tenant_id,
            record.identity.task_id,
            self._fingerprint(record.identity.execution_owner_id),
            self._fingerprint(record.public_session_id),
            placement,
        )
        safe_inc("shell_session_starts")
        self._emit_active_session_gauges(placement)

    def _emit_process_completed(
        self,
        record: ShellSessionRecord,
        process_status: ShellProcessStatus,
    ) -> None:
        placement = self._placement(record.identity.runtime_placement_mode)
        process_status_value = process_status.value
        logger.info(
            (
                "shell_session event=process_completed tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s process_status=%s"
            ),
            record.identity.tenant_id,
            record.identity.task_id,
            self._fingerprint(record.identity.execution_owner_id),
            self._fingerprint(record.public_session_id),
            placement,
            process_status_value,
        )
        safe_inc(f"shell_session_terminal_outcomes.{process_status_value}")

    def _emit_session_closed(
        self,
        record: ShellSessionRecord,
        close_reason: str,
    ) -> None:
        placement = self._placement(record.identity.runtime_placement_mode)
        logger.info(
            (
                "shell_session event=session_closed tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s close_reason=%s"
            ),
            record.identity.tenant_id,
            record.identity.task_id,
            self._fingerprint(record.identity.execution_owner_id),
            self._fingerprint(record.public_session_id),
            placement,
            self._stable_segment(close_reason),
        )

    def _emit_operation_failed(
        self,
        identity: ShellSessionIdentity,
        error_code: ShellSessionErrorCode,
        *,
        public_session_id: str | None = None,
    ) -> None:
        placement = self._placement(identity.runtime_placement_mode)
        error_code_value = error_code.value
        logger.info(
            (
                "shell_session event=operation_failed tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s error_code=%s"
            ),
            identity.tenant_id,
            identity.task_id,
            self._fingerprint(identity.execution_owner_id),
            self._fingerprint(public_session_id or ""),
            placement,
            error_code_value,
        )
        safe_inc(f"shell_session_operation_failures.{error_code_value}")

    def _emit_active_session_gauges(self, *placements: str) -> None:
        normalized = {self._placement(placement) for placement in placements}
        normalized.update(
            self._placement(record.identity.runtime_placement_mode)
            for record in self._records.values()
        )
        for placement in normalized:
            active_count = sum(
                1
                for record in self._records.values()
                if self._placement(record.identity.runtime_placement_mode)
                == placement
            )
            safe_gauge(f"shell_session_active_sessions.{placement}", active_count)

    @staticmethod
    def _fingerprint(value: str) -> str:
        if not value:
            return "none"
        return hashlib.blake2s(value.encode("utf-8"), digest_size=8).hexdigest()

    @classmethod
    def _placement(cls, value: str | None) -> str:
        normalized = cls._stable_segment(value or "unknown")
        if normalized in {"local", "runner", "managed_runner"}:
            return normalized
        return "unknown"

    @staticmethod
    def _stable_segment(value: str) -> str:
        normalized = "".join(
            char if char.isalnum() or char == "_" else "_"
            for char in str(value).strip().lower()
        ).strip("_")
        return normalized or "unknown"

    @staticmethod
    def _error_update(
        *,
        error_code: ShellSessionErrorCode,
        duration_ms: int,
    ) -> ShellSessionUpdate:
        message = _SHELL_SESSION_ERROR_MESSAGES[error_code]
        return ShellSessionUpdate(
            success=False,
            status="error",
            process_status=None,
            session_id=None,
            stdout="",
            stderr=message,
            exit_code=None,
            stdin_available=False,
            truncated=False,
            duration_ms=duration_ms,
            summary=message,
            error_code=error_code,
        )

    @staticmethod
    def _timeout_update(
        *,
        stdout: str,
        truncated: bool,
        duration_ms: int,
    ) -> ShellSessionUpdate:
        message = _SHELL_SESSION_ERROR_MESSAGES[
            ShellSessionErrorCode.COMMAND_TIMED_OUT
        ]
        return ShellSessionUpdate(
            success=False,
            status="error",
            process_status=ShellProcessStatus.TIMED_OUT,
            session_id=None,
            stdout=stdout,
            stderr=message,
            exit_code=None,
            stdin_available=False,
            truncated=truncated,
            duration_ms=duration_ms,
            error_code=ShellSessionErrorCode.COMMAND_TIMED_OUT,
            summary=message,
        )
