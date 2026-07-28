"""Process-local subagent-run registry.

The registry owns only in-memory lifecycle state for the current backend
process. It is intentionally not durable, distributed, or database-backed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypeAlias

from .contracts import AgentAssignment, AgentKind, AgentResult, AgentRunStatus


AgentRunKey: TypeAlias = tuple[int, int, str]

ACTIVE_AGENT_RUN_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {"queued", "running", "waiting_for_approval"}
)
TERMINAL_AGENT_RUN_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {"completed", "failed", "cancelled"}
)
DEFAULT_FINISHED_RETENTION = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class LocalAgentRun:
    """Immutable snapshot of one process-local subagent run."""

    agent_run_id: str
    agent_id: str
    tenant_id: int
    task_id: int
    conversation_id: str
    parent_turn_id: str
    graph_thread_id: str
    agent_kind: AgentKind
    assignment: AgentAssignment
    status: AgentRunStatus
    lifecycle_version: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: AgentResult | None
    safe_error: str | None
    task_handle: asyncio.Task[Any] | None
    cancel_requested: bool
    result_consumed: bool
    accounted_usage_record_count: int


class ActiveAgentRunExistsError(RuntimeError):
    """Legacy error retained for callers that handle old singleton conflicts."""

    def __init__(self, *, tenant_id: int, task_id: int, active_agent_run_id: str) -> None:
        super().__init__(
            "An active process-local subagent run already exists for "
            f"tenant_id={tenant_id}, task_id={task_id}: {active_agent_run_id}"
        )
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.active_agent_run_id = active_agent_run_id


class AgentRunNotFoundError(KeyError):
    """Raised when a process-local subagent run key is not present."""


class ProcessLocalAgentRunRegistry:
    """Lock-protected registry for subagent runs owned by this backend process."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        finished_retention: timedelta = DEFAULT_FINISHED_RETENTION,
    ) -> None:
        self._clock = clock or _utc_now
        self._finished_retention = finished_retention
        self._lock = asyncio.Lock()
        self._runs: dict[AgentRunKey, LocalAgentRun] = {}

    async def register(
        self,
        assignment: AgentAssignment,
        *,
        graph_thread_id: str,
        max_active_runs_per_task: int | None = None,
    ) -> LocalAgentRun:
        """Create a queued local entry for one immutable subagent run id."""
        graph_thread_id = _require_non_empty(graph_thread_id, "graph_thread_id")
        if max_active_runs_per_task is not None and max_active_runs_per_task < 1:
            raise ValueError("max_active_runs_per_task must be positive")
        async with self._lock:
            key = _key(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            existing = self._runs.get(key)
            if existing is not None:
                return existing
            if max_active_runs_per_task is not None:
                active_same_agent = [
                    entry
                    for entry in self._runs.values()
                    if entry.tenant_id == assignment.tenant_id
                    and entry.task_id == assignment.task_id
                    and entry.agent_id == assignment.agent_id
                    and entry.status in ACTIVE_AGENT_RUN_STATUSES
                ]
                if len(active_same_agent) >= max_active_runs_per_task:
                    raise ActiveAgentRunExistsError(
                        tenant_id=assignment.tenant_id,
                        task_id=assignment.task_id,
                        active_agent_run_id=active_same_agent[0].agent_run_id,
                    )

            now = self._clock()
            entry = LocalAgentRun(
                agent_run_id=assignment.agent_run_id,
                agent_id=assignment.agent_id,
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                conversation_id=assignment.conversation_id,
                parent_turn_id=assignment.parent_turn_id,
                graph_thread_id=graph_thread_id,
                agent_kind=assignment.agent_kind,
                assignment=assignment,
                status="queued",
                lifecycle_version=1,
                created_at=now,
                started_at=None,
                completed_at=None,
                result=None,
                safe_error=None,
                task_handle=None,
                cancel_requested=False,
                result_consumed=False,
                accounted_usage_record_count=0,
            )
            self._runs[key] = entry
            return entry

    async def get(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun | None:
        """Return a local entry only when tenant, task, and run id all match."""
        async with self._lock:
            return self._runs.get(
                _key(tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id)
            )

    async def attach_task_handle(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        task_handle: asyncio.Task[Any],
    ) -> LocalAgentRun:
        """Attach the non-serializable local asyncio task handle."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                return entry
            return self._store(
                replace(entry, task_handle=task_handle),
            )

    async def mark_running(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        """Transition a queued or waiting local run to running."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry) or entry.status == "running":
                return entry
            return self._store(
                replace(
                    entry,
                    status="running",
                    lifecycle_version=entry.lifecycle_version + 1,
                    started_at=entry.started_at or self._clock(),
                )
            )

    async def mark_waiting_for_approval(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        accounted_usage_record_count: int | None = None,
    ) -> LocalAgentRun:
        """Transition an active local run to the shared approval wait state."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry) or entry.status == "waiting_for_approval":
                return entry
            accounted_count = (
                entry.accounted_usage_record_count
                if accounted_usage_record_count is None
                else max(0, int(accounted_usage_record_count))
            )
            return self._store(
                replace(
                    entry,
                    status="waiting_for_approval",
                    lifecycle_version=entry.lifecycle_version + 1,
                    accounted_usage_record_count=accounted_count,
                )
            )

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        """Set the local cancellation flag and cancel the owned task handle."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                return entry
            if entry.task_handle is not None and not entry.task_handle.done():
                entry.task_handle.cancel()
            if entry.status == "waiting_for_approval":
                return self._terminal_entry(
                    entry,
                    status="cancelled",
                    cancel_requested=True,
                )
            if entry.cancel_requested:
                return entry
            return self._store(replace(entry, cancel_requested=True))

    async def mark_completed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        result: AgentResult,
    ) -> LocalAgentRun:
        """Store the terminal result without allowing later callback regressions."""
        if result.agent_run_id != agent_run_id:
            raise ValueError("result.agent_run_id must match agent_run_id")
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                return entry
            return self._terminal_entry(entry, status="completed", result=result)

    async def mark_failed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        safe_error: str,
    ) -> LocalAgentRun:
        """Store a safe terminal error for a failed local run."""
        safe_error = _require_non_empty(safe_error, "safe_error")
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                return entry
            return self._terminal_entry(entry, status="failed", safe_error=safe_error)

    async def mark_cancelled(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        """Mark a local run cancelled after the worker observes cancellation."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                return entry
            return self._terminal_entry(
                entry,
                status="cancelled",
                cancel_requested=True,
            )

    async def consume_result(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> AgentResult | None:
        """Return a terminal result once for same-process main-agent handoff."""
        async with self._lock:
            entry = self._runs.get(
                _key(tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id)
            )
            if (
                entry is None
                or entry.result is None
                or entry.result_consumed
                or entry.status not in TERMINAL_AGENT_RUN_STATUSES
            ):
                return None
            self._store(replace(entry, result_consumed=True))
            return entry.result

    async def cleanup_finished(self) -> int:
        """Drop finished entries older than the process-local retention window."""
        cutoff = self._clock() - self._finished_retention
        async with self._lock:
            stale_keys = [
                key
                for key, entry in self._runs.items()
                if entry.completed_at is not None and entry.completed_at <= cutoff
            ]
            for key in stale_keys:
                del self._runs[key]
            return len(stale_keys)

    async def list_task_runs(self, *, tenant_id: int, task_id: int) -> list[LocalAgentRun]:
        """List process-local entries for one authorized task scope."""
        async with self._lock:
            return [
                entry
                for entry in self._runs.values()
                if entry.tenant_id == tenant_id and entry.task_id == task_id
            ]

    async def find_active_by_graph_thread(
        self,
        *,
        task_id: int,
        graph_thread_id: str,
        tenant_id: int | None = None,
    ) -> LocalAgentRun | None:
        """Return the active local run for one task child graph thread."""
        graph_thread_id = _require_non_empty(graph_thread_id, "graph_thread_id")
        async with self._lock:
            candidates = [
                entry
                for entry in self._runs.values()
                if entry.task_id == task_id
                and entry.graph_thread_id == graph_thread_id
                and entry.status in ACTIVE_AGENT_RUN_STATUSES
                and (tenant_id is None or entry.tenant_id == tenant_id)
            ]
            if len(candidates) != 1:
                return None
            return candidates[0]

    def _require_entry(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        key = _key(tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id)
        entry = self._runs.get(key)
        if entry is None:
            raise AgentRunNotFoundError(
                f"Process-local subagent run not found for key={key!r}"
            )
        return entry

    def _store(self, entry: LocalAgentRun) -> LocalAgentRun:
        self._runs[
            _key(
                tenant_id=entry.tenant_id,
                task_id=entry.task_id,
                agent_run_id=entry.agent_run_id,
            )
        ] = entry
        return entry

    def _terminal_entry(
        self,
        entry: LocalAgentRun,
        *,
        status: AgentRunStatus,
        result: AgentResult | None = None,
        safe_error: str | None = None,
        cancel_requested: bool | None = None,
    ) -> LocalAgentRun:
        return self._store(
            replace(
                entry,
                status=status,
                lifecycle_version=entry.lifecycle_version + 1,
                completed_at=self._clock(),
                result=result,
                safe_error=safe_error,
                task_handle=None,
                cancel_requested=entry.cancel_requested
                if cancel_requested is None
                else cancel_requested,
            )
        )


def _key(*, tenant_id: int, task_id: int, agent_run_id: str) -> AgentRunKey:
    return (tenant_id, task_id, agent_run_id)


def _is_terminal(entry: LocalAgentRun) -> bool:
    return entry.status in TERMINAL_AGENT_RUN_STATUSES


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ACTIVE_AGENT_RUN_STATUSES",
    "DEFAULT_FINISHED_RETENTION",
    "TERMINAL_AGENT_RUN_STATUSES",
    "ActiveAgentRunExistsError",
    "AgentRunKey",
    "AgentRunNotFoundError",
    "LocalAgentRun",
    "ProcessLocalAgentRunRegistry",
]
