"""In-memory registries for physical and logical terminal sessions.

Responsibilities:
- Own physical terminal-session storage and stale cleanup policy.
- Own logical shell-session records, capacity, claims, and expiry transitions.
- Keep physical provider resources distinct from public logical shell handles.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionOrigin,
)
from runtime_shared.shell_session_framing import StreamingPtyFramingParser

from ...core.time_utils import utc_now
from .models import TerminalSession
from .shell_session_observability import ShellSessionOperationalObserver

logger = logging.getLogger(__name__)


class TerminalSessionRegistry:
    """Store live terminal sessions and clean up stale entries."""

    def __init__(
        self,
        *,
        session_timeout: int = 3600,
        agent_session_timeout: int = 7200,
        cleanup_interval: int = 300,
    ) -> None:
        self.sessions: dict[str, TerminalSession] = {}
        self.session_timeout = session_timeout
        self.agent_session_timeout = agent_session_timeout
        self.cleanup_interval = cleanup_interval
        self.cleanup_task: asyncio.Task | None = None

    def start_cleanup_loop(
        self,
        close_session_callback: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Start the background stale-session cleanup loop."""
        if self.cleanup_task is None or self.cleanup_task.done():
            self.cleanup_task = asyncio.create_task(
                self._cleanup_sessions_loop(close_session_callback)
            )

    async def stop_cleanup_loop(self) -> None:
        """Cancel the background stale-session cleanup loop."""
        task = self.cleanup_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self.cleanup_task = None

    async def _cleanup_sessions_loop(
        self,
        close_session_callback: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Sleep, scan for stale sessions, and close them via the manager callback."""
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval)
                for session_id in self.iter_stale_session_ids():
                    session = self.sessions.get(session_id)
                    if session is None:
                        continue
                    logger.info(
                        "Cleaning up stale %s session: %s",
                        session.session_type,
                        session_id,
                    )
                    try:
                        await close_session_callback(session_id)
                    except Exception as exc:
                        logger.error("Error cleaning stale terminal session %s: %s", session_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error in terminal session cleanup loop: %s", exc)

    def get(self, session_id: str) -> TerminalSession | None:
        """Return a session by id."""
        return self.sessions.get(session_id)

    def set(self, session: TerminalSession) -> None:
        """Store or replace a session."""
        self.sessions[session.session_id] = session

    def remove(self, session_id: str) -> TerminalSession | None:
        """Remove and return a session if it exists."""
        return self.sessions.pop(session_id, None)

    def get_user_sessions(self, user_id: int) -> list[TerminalSession]:
        """Return active sessions owned by the user."""
        return [
            session
            for session in self.sessions.values()
            if session.user_id == user_id and session.is_active
        ]

    def get_task_sessions(self, task_id: int) -> list[TerminalSession]:
        """Return active sessions for the task."""
        return [
            session
            for session in self.sessions.values()
            if session.task_id == task_id and session.is_active
        ]

    def iter_stale_session_ids(self) -> Iterable[str]:
        """Yield ids of sessions whose activity exceeds their timeout."""
        current_time = utc_now()
        for session_id, session in list(self.sessions.items()):
            timeout = (
                self.agent_session_timeout
                if session.session_type == "agent"
                else self.session_timeout
            )
            if (
                session.last_activity is not None
                and current_time - session.last_activity > timedelta(seconds=timeout)
            ):
                yield session_id


@dataclass(slots=True)
class ShellSessionRecord:
    """Small process-local control record for one live logical shell session."""

    public_session_id: str
    terminal_session_id: str
    identity: ShellSessionIdentity
    originating_capability: ShellCapability
    origin: ShellSessionOrigin | None
    framing_parser: StreamingPtyFramingParser
    last_activity_at: float
    deadline_at: float
    operation_in_progress: bool = False
    pending_utf8_bytes: bytes = b""
    initial_quiet_boundary_emitted: bool = False


@dataclass(slots=True)
class _StartCapacityReservation:
    """Idempotent pending-capacity lease for one logical shell-session start."""

    owner_key: tuple[int, int, str]
    task_key: tuple[int, int]
    released: bool = False


class ShellSessionStateRegistry:
    """Own lock-protected logical shell-session state and capacity policy."""

    def __init__(
        self,
        *,
        max_active_per_owner: int,
        max_active_per_task: int,
        idle_timeout_sec: float,
        observer: ShellSessionOperationalObserver,
    ) -> None:
        self._max_active_per_owner = max_active_per_owner
        self._max_active_per_task = max_active_per_task
        self._idle_timeout_sec = idle_timeout_sec
        self._observer = observer
        self._records: dict[str, ShellSessionRecord] = {}
        self._pending_owner_starts: dict[tuple[int, int, str], int] = {}
        self._pending_task_starts: dict[tuple[int, int], int] = {}
        self._lock = asyncio.Lock()

    async def reserve_start(
        self,
        identity: ShellSessionIdentity,
    ) -> _StartCapacityReservation | None:
        """Reserve owner and task capacity atomically for a pending start."""

        async with self._lock:
            owner_key = self._owner_start_key(identity)
            task_key = self._task_start_key(identity)
            owner_pending = self._pending_owner_starts.get(owner_key, 0)
            task_pending = self._pending_task_starts.get(task_key, 0)
            if (
                self._active_owner_count(identity) + owner_pending
                >= self._max_active_per_owner
            ):
                return None
            if (
                self._active_task_count(identity) + task_pending
                >= self._max_active_per_task
            ):
                return None

            self._pending_owner_starts[owner_key] = owner_pending + 1
            self._pending_task_starts[task_key] = task_pending + 1
            return _StartCapacityReservation(owner_key=owner_key, task_key=task_key)

    async def release_start(
        self,
        reservation: _StartCapacityReservation | None,
    ) -> None:
        """Idempotently release a pending start reservation."""

        if reservation is None or reservation.released:
            return
        async with self._lock:
            self._release_start_locked(reservation)

    async def register(
        self,
        record: ShellSessionRecord,
        *,
        reservation: _StartCapacityReservation,
    ) -> None:
        """Commit a logical record and observe its opened state under the lock."""

        async with self._lock:
            self._records[record.public_session_id] = record
            self._release_start_locked(reservation)
            self._observer.session_opened(
                identity=record.identity,
                public_session_id=record.public_session_id,
                active_placements=self._active_placements(),
            )

    async def get_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None:
        """Return immutable capability provenance for a full-identity match."""

        normalized_id = str(public_session_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized_id)
            if record is None or record.identity != identity:
                return None
            return record.originating_capability

    async def claim(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
        now: float,
    ) -> tuple[
        ShellSessionRecord | None,
        ShellSessionRecord | None,
        ShellSessionErrorCode,
        str | None,
    ]:
        """Claim a record or atomically retire it when its lifetime expired."""

        normalized_id = str(public_session_id or "").strip()
        async with self._lock:
            record = self._records.get(normalized_id)
            if record is None or record.identity != identity:
                return (
                    None,
                    None,
                    ShellSessionErrorCode.SESSION_UNAVAILABLE,
                    None,
                )
            if self.is_deadline_expired(record, now):
                self._records.pop(normalized_id, None)
                self._observer.active_session_gauges(
                    changed_placements=(record.identity.runtime_placement_mode,),
                    active_placements=self._active_placements(),
                )
                self._observer.process_completed(
                    identity=record.identity,
                    public_session_id=record.public_session_id,
                    process_status=ShellProcessStatus.TIMED_OUT,
                )
                return (
                    None,
                    record,
                    ShellSessionErrorCode.COMMAND_TIMED_OUT,
                    "deadline_expired",
                )
            if self._is_idle_expired(record, now):
                self._records.pop(normalized_id, None)
                self._observer.active_session_gauges(
                    changed_placements=(record.identity.runtime_placement_mode,),
                    active_placements=self._active_placements(),
                )
                return (
                    None,
                    record,
                    ShellSessionErrorCode.SESSION_UNAVAILABLE,
                    "idle_expired",
                )
            if record.operation_in_progress:
                return None, None, ShellSessionErrorCode.SESSION_BUSY, None

            record.last_activity_at = now
            record.operation_in_progress = True
            return record, None, ShellSessionErrorCode.SESSION_UNAVAILABLE, None

    async def release(self, record: ShellSessionRecord) -> None:
        """Release an operation claim if the same record remains registered."""

        async with self._lock:
            current = self._records.get(record.public_session_id)
            if current is record:
                record.operation_in_progress = False

    async def remove(
        self,
        public_session_id: str,
        *,
        expected_record: ShellSessionRecord | None = None,
    ) -> ShellSessionRecord | None:
        """Remove and return a record when its optional identity guard matches."""

        async with self._lock:
            record = self._records.get(public_session_id)
            if record is None:
                return None
            if expected_record is not None and record is not expected_record:
                return None
            self._records.pop(public_session_id, None)
            record.operation_in_progress = False
            self._observer.active_session_gauges(
                changed_placements=(record.identity.runtime_placement_mode,),
                active_placements=self._active_placements(),
            )
            return record

    async def pop_owner(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> list[ShellSessionRecord]:
        """Remove all records for one tenant/task execution owner."""

        return await self._pop_matching(
            lambda record: (
                record.identity.tenant_id == int(tenant_id)
                and record.identity.task_id == int(task_id)
                and record.identity.execution_owner_id == execution_owner_id
            )
        )

    async def pop_task(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> list[ShellSessionRecord]:
        """Remove all logical records for one tenant/task pair."""

        return await self._pop_matching(
            lambda record: (
                record.identity.tenant_id == int(tenant_id)
                and record.identity.task_id == int(task_id)
            )
        )

    async def pop_stale(
        self,
        now: float,
    ) -> list[tuple[ShellSessionRecord, str]]:
        """Remove deadline- or idle-expired records, with deadline precedence."""

        async with self._lock:
            records: list[tuple[ShellSessionRecord, str]] = []
            for record in self._records.values():
                if self.is_deadline_expired(record, now):
                    records.append((record, "deadline_expired"))
                elif self._is_idle_expired(record, now):
                    records.append((record, "idle_expired"))
            for record, _close_reason in records:
                self._records.pop(record.public_session_id, None)
                record.operation_in_progress = False
            self._observer.active_session_gauges(
                changed_placements=(
                    record.identity.runtime_placement_mode
                    for record, _close_reason in records
                ),
                active_placements=self._active_placements(),
            )
            return records

    async def _pop_matching(
        self,
        predicate: Callable[[ShellSessionRecord], bool],
    ) -> list[ShellSessionRecord]:
        async with self._lock:
            records = [
                record for record in self._records.values() if predicate(record)
            ]
            for record in records:
                self._records.pop(record.public_session_id, None)
                record.operation_in_progress = False
            self._observer.active_session_gauges(
                changed_placements=(
                    record.identity.runtime_placement_mode for record in records
                ),
                active_placements=self._active_placements(),
            )
            return records

    def _active_owner_count(self, identity: ShellSessionIdentity) -> int:
        return sum(
            1
            for record in self._records.values()
            if (
                record.identity.tenant_id == identity.tenant_id
                and record.identity.task_id == identity.task_id
                and record.identity.execution_owner_id
                == identity.execution_owner_id
            )
        )

    def _active_task_count(self, identity: ShellSessionIdentity) -> int:
        return sum(
            1
            for record in self._records.values()
            if record.identity.tenant_id == identity.tenant_id
            and record.identity.task_id == identity.task_id
        )

    def _release_start_locked(
        self,
        reservation: _StartCapacityReservation,
    ) -> None:
        if reservation.released:
            return
        reservation.released = True
        owner_pending = self._pending_owner_starts.get(reservation.owner_key, 0)
        if owner_pending <= 1:
            self._pending_owner_starts.pop(reservation.owner_key, None)
        else:
            self._pending_owner_starts[reservation.owner_key] = owner_pending - 1

        task_pending = self._pending_task_starts.get(reservation.task_key, 0)
        if task_pending <= 1:
            self._pending_task_starts.pop(reservation.task_key, None)
        else:
            self._pending_task_starts[reservation.task_key] = task_pending - 1

    def _active_placements(self) -> tuple[str, ...]:
        return tuple(
            record.identity.runtime_placement_mode
            for record in self._records.values()
        )

    @staticmethod
    def _owner_start_key(identity: ShellSessionIdentity) -> tuple[int, int, str]:
        return (
            int(identity.tenant_id),
            int(identity.task_id),
            identity.execution_owner_id,
        )

    @staticmethod
    def _task_start_key(identity: ShellSessionIdentity) -> tuple[int, int]:
        return int(identity.tenant_id), int(identity.task_id)

    @staticmethod
    def is_deadline_expired(record: ShellSessionRecord, now: float) -> bool:
        """Return whether a record reached its monotonic hard deadline."""

        return now >= record.deadline_at

    def _is_idle_expired(self, record: ShellSessionRecord, now: float) -> bool:
        return now - record.last_activity_at >= self._idle_timeout_sec
