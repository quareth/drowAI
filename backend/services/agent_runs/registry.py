"""Process-local subagent-run registry.

The registry owns only in-memory lifecycle state for the current backend
process. It is intentionally not durable, distributed, or database-backed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeAlias

from backend.services.metrics.utils import safe_gauge, safe_inc

from .contracts import AgentAssignment, AgentKind, AgentResult, AgentRunStatus


logger = logging.getLogger(__name__)


AgentRunKey: TypeAlias = tuple[int, int, str]
HandoffWaitStatus: TypeAlias = Literal["ready", "inactive"]

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

    graph_thread_id: str
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
    result_claim_id: str | None
    accounted_usage_record_count: int

    @property
    def agent_run_id(self) -> str:
        return self.assignment.agent_run_id

    @property
    def agent_id(self) -> str:
        return self.assignment.agent_id

    @property
    def tenant_id(self) -> int:
        return self.assignment.tenant_id

    @property
    def task_id(self) -> int:
        return self.assignment.task_id

    @property
    def conversation_id(self) -> str:
        return self.assignment.conversation_id

    @property
    def parent_turn_id(self) -> str:
        return self.assignment.parent_turn_id

    @property
    def agent_kind(self) -> AgentKind:
        return self.assignment.agent_kind


@dataclass(frozen=True, slots=True)
class ClaimedHandoffBatch:
    """Process-local claim over ready terminal results for one parent task."""

    claim_id: str
    tenant_id: int
    task_id: int
    agent_run_ids: tuple[str, ...]
    results: tuple[AgentResult, ...]
    active_runs: tuple[LocalAgentRun, ...]


@dataclass(frozen=True, slots=True)
class AgentRunTransition:
    """Result of one lifecycle transition attempt against a local run."""

    entry: LocalAgentRun
    changed: bool


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


class AgentRunIdentityCollisionError(RuntimeError):
    """Raised when a scoped run id is reused for different immutable identity."""

    def __init__(self, *, tenant_id: int, task_id: int, agent_run_id: str) -> None:
        super().__init__(
            "Agent run identity collision for "
            f"tenant_id={tenant_id}, task_id={task_id}, agent_run_id={agent_run_id}"
        )
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.agent_run_id = agent_run_id


class HandoffClaimNotFoundError(KeyError):
    """Raised when a process-local handoff claim is not present."""


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
        self._state_changed = asyncio.Condition()
        self._runs: dict[AgentRunKey, LocalAgentRun] = {}
        self._claims: dict[str, tuple[AgentRunKey, ...]] = {}
        self._claim_sequence = 0
        self._state_version = 0

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
                if (
                    existing.assignment == assignment
                    and existing.graph_thread_id == graph_thread_id
                ):
                    return existing
                raise AgentRunIdentityCollisionError(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
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
                graph_thread_id=graph_thread_id,
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
                result_claim_id=None,
                accounted_usage_record_count=0,
            )
            self._runs[key] = entry
            self._mark_state_changed_locked()
        await self._notify_state_changed()
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
            updated = self._store(
                replace(entry, task_handle=task_handle),
            )
        await self._notify_state_changed()
        return updated

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
            updated = self._store(
                replace(
                    entry,
                    status="running",
                    lifecycle_version=entry.lifecycle_version + 1,
                    started_at=entry.started_at or self._clock(),
                )
            )
        await self._notify_state_changed()
        return updated

    async def mark_waiting_for_approval(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        accounted_usage_record_count: int | None = None,
    ) -> LocalAgentRun:
        """Transition an active local run to the shared approval wait state."""
        return (
            await self.record_waiting_for_approval(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
                accounted_usage_record_count=accounted_usage_record_count,
            )
        ).entry

    async def record_waiting_for_approval(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        accounted_usage_record_count: int | None = None,
    ) -> AgentRunTransition:
        """Transition to approval wait and report whether state changed."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry) or entry.status == "waiting_for_approval":
                return AgentRunTransition(entry=entry, changed=False)
            accounted_count = (
                entry.accounted_usage_record_count
                if accounted_usage_record_count is None
                else max(0, int(accounted_usage_record_count))
            )
            updated = self._store(
                replace(
                    entry,
                    status="waiting_for_approval",
                    lifecycle_version=entry.lifecycle_version + 1,
                    accounted_usage_record_count=accounted_count,
                )
            )
        await self._notify_state_changed()
        return AgentRunTransition(entry=updated, changed=True)

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
                updated = self._terminal_entry(
                    entry,
                    status="cancelled",
                    cancel_requested=True,
                )
                should_notify = True
                return_entry = updated
            elif entry.cancel_requested:
                return entry
            else:
                return_entry = self._store(replace(entry, cancel_requested=True))
                should_notify = True
        if should_notify:
            await self._notify_state_changed()
        return return_entry

    async def mark_completed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        result: AgentResult,
    ) -> LocalAgentRun:
        """Store the terminal result without allowing later callback regressions."""
        return (
            await self.record_completed(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
                result=result,
            )
        ).entry

    async def record_completed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        result: AgentResult,
    ) -> AgentRunTransition:
        """Store the terminal result and report whether this call changed state."""
        if result.agent_run_id != agent_run_id:
            raise ValueError("result.agent_run_id must match agent_run_id")
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                _record_duplicate_terminal_suppressed(entry, requested_status="completed")
                return AgentRunTransition(entry=entry, changed=False)
            updated = self._terminal_entry(entry, status="completed", result=result)
        await self._notify_state_changed()
        return AgentRunTransition(entry=updated, changed=True)

    async def mark_failed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        safe_error: str,
    ) -> LocalAgentRun:
        """Store a safe terminal error for a failed local run."""
        return (
            await self.record_failed(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
                safe_error=safe_error,
            )
        ).entry

    async def record_failed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        safe_error: str,
    ) -> AgentRunTransition:
        """Store a safe terminal error and report whether state changed."""
        safe_error = _require_non_empty(safe_error, "safe_error")
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                _record_duplicate_terminal_suppressed(entry, requested_status="failed")
                return AgentRunTransition(entry=entry, changed=False)
            updated = self._terminal_entry(
                entry, status="failed", safe_error=safe_error
            )
        await self._notify_state_changed()
        return AgentRunTransition(entry=updated, changed=True)

    async def mark_cancelled(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        """Mark a local run cancelled after the worker observes cancellation."""
        return (
            await self.record_cancelled(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
        ).entry

    async def record_cancelled(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> AgentRunTransition:
        """Mark a run cancelled and report whether this call changed state."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _is_terminal(entry):
                _record_duplicate_terminal_suppressed(entry, requested_status="cancelled")
                return AgentRunTransition(entry=entry, changed=False)
            updated = self._terminal_entry(
                entry,
                status="cancelled",
                cancel_requested=True,
            )
        await self._notify_state_changed()
        return AgentRunTransition(entry=updated, changed=True)

    async def claim_ready_handoffs(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str | None = None,
        max_results: int | None = None,
    ) -> ClaimedHandoffBatch | None:
        """Atomically claim ready results and snapshot active runs for one task."""
        if max_results is not None and max_results < 1:
            raise ValueError("max_results must be positive")
        async with self._lock:
            candidates: list[LocalAgentRun] = []
            claimed_ready_count = 0
            active_entries: list[LocalAgentRun] = []
            for entry in self._runs.values():
                if (
                    entry.tenant_id != tenant_id
                    or entry.task_id != task_id
                    or (
                        conversation_id is not None
                        and entry.conversation_id != conversation_id
                    )
                ):
                    continue
                if entry.status in ACTIVE_AGENT_RUN_STATUSES:
                    active_entries.append(entry)
                if (
                    entry.result is None
                    or entry.result_consumed
                    or entry.status not in TERMINAL_AGENT_RUN_STATUSES
                ):
                    continue
                if entry.result_claim_id is None:
                    candidates.append(entry)
                else:
                    claimed_ready_count += 1

            candidates.sort(key=_run_sort_key)
            if max_results is not None:
                candidates = candidates[:max_results]
            active_runs = tuple(sorted(active_entries, key=_run_sort_key))
            if not candidates:
                if claimed_ready_count:
                    safe_inc("agent_run_handoff_duplicate_claim_suppressed")
                    safe_gauge(
                        "agent_run_handoff_duplicate_claim_suppressed_count",
                        claimed_ready_count,
                    )
                    logger.info(
                        "Suppressed duplicate handoff claim tenant_id=%s task_id=%s "
                        "conversation_id=%s claimed_ready_count=%s",
                        tenant_id,
                        task_id,
                        conversation_id,
                        claimed_ready_count,
                    )
                return None

            claim_id = self._next_claim_id_locked(
                tenant_id=tenant_id,
                task_id=task_id,
            )
            keys: list[AgentRunKey] = []
            results: list[AgentResult] = []
            for entry in candidates:
                key = _key(
                    tenant_id=entry.tenant_id,
                    task_id=entry.task_id,
                    agent_run_id=entry.agent_run_id,
                )
                keys.append(key)
                assert entry.result is not None
                results.append(entry.result)
                self._store(replace(entry, result_claim_id=claim_id))
            self._claims[claim_id] = tuple(keys)
            batch = ClaimedHandoffBatch(
                claim_id=claim_id,
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_ids=tuple(entry.agent_run_id for entry in candidates),
                results=tuple(results),
                active_runs=active_runs,
            )
            safe_inc("agent_run_handoff_claim_created")
            safe_gauge("agent_run_handoff_claim_batch_size", len(batch.agent_run_ids))
            safe_gauge("agent_run_handoff_claim_active_run_count", len(active_runs))
            logger.info(
                "Created handoff claim tenant_id=%s task_id=%s "
                "conversation_id=%s claim_id=%s batch_size=%s active_run_count=%s",
                tenant_id,
                task_id,
                conversation_id,
                claim_id,
                len(batch.agent_run_ids),
                len(active_runs),
            )
        await self._notify_state_changed()
        return batch

    async def acknowledge_handoffs(self, claim_id: str) -> None:
        """Mark claimed results applied after parent state updates succeed."""
        claim_id = _require_non_empty(claim_id, "claim_id")
        async with self._lock:
            keys = self._claims.pop(claim_id, None)
            if keys is None:
                safe_inc("agent_run_handoff_acknowledge_missing_claim")
                raise HandoffClaimNotFoundError(claim_id)
            acknowledged = 0
            for key in keys:
                entry = self._runs.get(key)
                if entry is None or entry.result_claim_id != claim_id:
                    continue
                self._store(replace(entry, result_consumed=True, result_claim_id=None))
                acknowledged += 1
            safe_inc("agent_run_handoff_claim_acknowledged")
            safe_gauge("agent_run_handoff_claim_acknowledged_count", acknowledged)
            logger.info(
                "Acknowledged handoff claim claim_id=%s acknowledged_count=%s",
                claim_id,
                acknowledged,
            )
        await self._notify_state_changed()

    async def release_handoffs(self, claim_id: str) -> None:
        """Release a claimed batch so cancellation or failure can retry it."""
        claim_id = _require_non_empty(claim_id, "claim_id")
        async with self._lock:
            keys = self._claims.pop(claim_id, None)
            if keys is None:
                safe_inc("agent_run_handoff_release_missing_claim")
                raise HandoffClaimNotFoundError(claim_id)
            released = 0
            for key in keys:
                entry = self._runs.get(key)
                if (
                    entry is None
                    or entry.result_claim_id != claim_id
                    or entry.result_consumed
                ):
                    continue
                self._store(replace(entry, result_claim_id=None))
                released += 1
            safe_inc("agent_run_handoff_claim_released")
            safe_gauge("agent_run_handoff_claim_released_count", released)
            logger.info(
                "Released handoff claim claim_id=%s released_count=%s",
                claim_id,
                released,
            )
        await self._notify_state_changed()

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
                or entry.result_claim_id is not None
                or entry.status not in TERMINAL_AGENT_RUN_STATUSES
            ):
                safe_inc("agent_run_handoff_duplicate_delivery_suppressed")
                return None
            self._store(replace(entry, result_consumed=True))
            result = entry.result
        await self._notify_state_changed()
        return result

    async def cleanup_finished(self) -> int:
        """Drop finished entries older than the process-local retention window."""
        cutoff = self._clock() - self._finished_retention
        async with self._lock:
            stale_keys = [
                key
                for key, entry in self._runs.items()
                if entry.completed_at is not None and entry.completed_at <= cutoff
                and entry.result_claim_id is None
            ]
            for key in stale_keys:
                del self._runs[key]
            if stale_keys:
                stale_set = set(stale_keys)
                self._claims = {
                    claim_id: tuple(key for key in keys if key not in stale_set)
                    for claim_id, keys in self._claims.items()
                }
                self._claims = {
                    claim_id: keys
                    for claim_id, keys in self._claims.items()
                    if keys
                }
                self._mark_state_changed_locked()
            count = len(stale_keys)
        if count:
            await self._notify_state_changed()
        return count

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

    async def state_version(self) -> int:
        """Return the current process-local registry mutation version."""
        async with self._lock:
            return self._state_version

    async def wait_for_state_change(self, *, after_version: int) -> int:
        """Wait until the registry mutates after ``after_version``.

        The registry lock is never held while blocked on the notification
        condition, so callers can wait for child lifecycle changes without
        preventing those changes from being recorded.
        """
        while True:
            async with self._lock:
                current = self._state_version
            if current != after_version:
                return current
            async with self._state_changed:
                async with self._lock:
                    current = self._state_version
                if current != after_version:
                    return current
                await self._state_changed.wait()

    async def wait_for_ready_handoffs_or_inactive(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str | None = None,
        after_version: int,
    ) -> HandoffWaitStatus:
        """Wait for scoped ready handoffs or for scoped active runs to end.

        Notifications for other tenants, tasks, or conversations do not satisfy
        the wait; they only advance the observed registry version before the
        caller blocks again.
        """
        while True:
            async with self._lock:
                status = self._handoff_wait_status_locked(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                )
            if status is not None:
                return status

            async with self._state_changed:
                async with self._lock:
                    status = self._handoff_wait_status_locked(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        conversation_id=conversation_id,
                    )
                    current = self._state_version
                if status is not None:
                    return status
                if current != after_version:
                    after_version = current
                    continue
                await self._state_changed.wait()

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
        self._mark_state_changed_locked()
        return entry

    def _next_claim_id_locked(self, *, tenant_id: int, task_id: int) -> str:
        self._claim_sequence += 1
        return f"handoff-claim:{tenant_id}:{task_id}:{self._claim_sequence}"

    def _mark_state_changed_locked(self) -> None:
        self._state_version += 1

    def _handoff_wait_status_locked(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str | None,
    ) -> HandoffWaitStatus | None:
        has_active = False
        for entry in self._runs.values():
            if (
                entry.tenant_id != tenant_id
                or entry.task_id != task_id
                or (
                    conversation_id is not None
                    and entry.conversation_id != conversation_id
                )
            ):
                continue
            if (
                entry.result is not None
                and not entry.result_consumed
                and entry.result_claim_id is None
                and entry.status in TERMINAL_AGENT_RUN_STATUSES
            ):
                return "ready"
            if entry.status in ACTIVE_AGENT_RUN_STATUSES:
                has_active = True
        if has_active:
            return None
        return "inactive"

    async def _notify_state_changed(self) -> None:
        async with self._state_changed:
            self._state_changed.notify_all()

    def _terminal_entry(
        self,
        entry: LocalAgentRun,
        *,
        status: AgentRunStatus,
        result: AgentResult | None = None,
        safe_error: str | None = None,
        cancel_requested: bool | None = None,
    ) -> LocalAgentRun:
        terminal_result = result
        if terminal_result is None and status in {"failed", "cancelled"}:
            terminal_result = _fallback_terminal_result(
                entry,
                status=status,
                safe_error=safe_error,
            )
        return self._store(
            replace(
                entry,
                status=status,
                lifecycle_version=entry.lifecycle_version + 1,
                completed_at=self._clock(),
                result=terminal_result,
                safe_error=safe_error,
                task_handle=None,
                cancel_requested=entry.cancel_requested
                if cancel_requested is None
                else cancel_requested,
            )
        )


def _key(*, tenant_id: int, task_id: int, agent_run_id: str) -> AgentRunKey:
    return (tenant_id, task_id, agent_run_id)


def _run_sort_key(entry: LocalAgentRun) -> tuple[str, str, str]:
    return (
        _datetime_sort_key(entry.completed_at or entry.started_at),
        _datetime_sort_key(entry.created_at),
        entry.agent_run_id,
    )


def _datetime_sort_key(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _is_terminal(entry: LocalAgentRun) -> bool:
    return entry.status in TERMINAL_AGENT_RUN_STATUSES


def _record_duplicate_terminal_suppressed(
    entry: LocalAgentRun,
    *,
    requested_status: AgentRunStatus,
) -> None:
    """Record a duplicate terminal callback without exposing handoff content."""
    safe_inc("agent_run_terminal_duplicate_suppressed")
    safe_inc(f"agent_run_terminal_duplicate_suppressed_{requested_status}")
    logger.info(
        "Suppressed duplicate terminal transition tenant_id=%s task_id=%s "
        "agent_run_id=%s existing_status=%s requested_status=%s",
        entry.tenant_id,
        entry.task_id,
        entry.agent_run_id,
        entry.status,
        requested_status,
    )


def _fallback_terminal_result(
    entry: LocalAgentRun,
    *,
    status: AgentRunStatus,
    safe_error: str | None,
) -> AgentResult:
    if status == "failed":
        summary = f"Subagent run failed: {safe_error or 'Subagent worker failed'}"
        limitations = (safe_error or "Subagent worker failed",)
        recommended_next_steps = (
            "Review the failure and decide whether a new bounded assignment is needed.",
        )
    elif status == "cancelled":
        summary = "Subagent run was cancelled before completing its assignment."
        limitations = ("Subagent run was cancelled.",)
        recommended_next_steps = (
            "Decide whether the cancelled assignment is still required.",
        )
    else:
        raise ValueError(f"fallback result is only supported for terminal status: {status}")
    return AgentResult(
        agent_run_id=entry.agent_run_id,
        agent_id=entry.agent_id,
        agent_kind=entry.agent_kind,
        outcome=status,
        summary=summary,
        limitations=limitations,
        recommended_next_steps=recommended_next_steps,
    )


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
    "AgentRunIdentityCollisionError",
    "AgentRunNotFoundError",
    "AgentRunTransition",
    "ClaimedHandoffBatch",
    "HandoffClaimNotFoundError",
    "LocalAgentRun",
    "ProcessLocalAgentRunRegistry",
]
