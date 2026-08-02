"""Process-local subagent-run registry.

The registry owns only in-memory lifecycle state for the current backend
process. It is intentionally not durable, distributed, or database-backed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.services.metrics.utils import safe_gauge, safe_inc

from . import registry_contracts as _registry_contracts
from . import registry_handoffs as _registry_handoffs
from . import registry_lifecycle as _registry_lifecycle
from . import registry_queries as _registry_queries
from . import registry_signaling as _registry_signaling
from .contracts import AgentAssignment, AgentResult, AgentRunStatus


logger = logging.getLogger(__name__)


class ProcessLocalAgentRunRegistry:
    """Lock-protected registry for subagent runs owned by this backend process."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        finished_retention: timedelta = _registry_contracts.DEFAULT_FINISHED_RETENTION,
    ) -> None:
        self._clock = clock or _utc_now
        self._finished_retention = finished_retention
        self._lock = asyncio.Lock()
        self._signal = _registry_signaling.RegistryStateSignal(self._lock)
        self._runs: dict[
            _registry_contracts.AgentRunKey,
            _registry_contracts.LocalAgentRun,
        ] = {}
        self._claims: dict[str, tuple[_registry_contracts.AgentRunKey, ...]] = {}
        self._claim_sequence = 0

    async def register(
        self,
        assignment: AgentAssignment,
        *,
        graph_thread_id: str,
        max_active_runs_per_task: int | None = None,
    ) -> _registry_contracts.LocalAgentRun:
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
                raise _registry_contracts.AgentRunIdentityCollisionError(
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
                    and entry.status in _registry_contracts.ACTIVE_AGENT_RUN_STATUSES
                ]
                if len(active_same_agent) >= max_active_runs_per_task:
                    raise _registry_contracts.ActiveAgentRunExistsError(
                        tenant_id=assignment.tenant_id,
                        task_id=assignment.task_id,
                        active_agent_run_id=active_same_agent[0].agent_run_id,
                    )

            entry = _registry_lifecycle.build_queued_entry(
                assignment=assignment,
                graph_thread_id=graph_thread_id,
                created_at=self._clock(),
            )
            self._runs[key] = entry
            self._signal.mark_changed_locked()
        await self._signal.notify_after_commit()
        return entry

    async def get(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> _registry_contracts.LocalAgentRun | None:
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
    ) -> _registry_contracts.LocalAgentRun:
        """Attach the non-serializable local asyncio task handle."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _registry_lifecycle.is_terminal(entry):
                return entry
            updated = self._store(
                replace(entry, task_handle=task_handle),
            )
        await self._signal.notify_after_commit()
        return updated

    async def mark_running(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> _registry_contracts.LocalAgentRun:
        """Transition a queued or waiting local run to running."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _registry_lifecycle.is_terminal(entry) or entry.status == "running":
                return entry
            updated = self._store(
                _registry_lifecycle.build_running_entry(
                    entry,
                    started_at=entry.started_at or self._clock(),
                )
            )
        await self._signal.notify_after_commit()
        return updated

    async def mark_waiting_for_approval(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        accounted_usage_record_count: int | None = None,
    ) -> _registry_contracts.LocalAgentRun:
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
    ) -> _registry_contracts.AgentRunTransition:
        """Transition to approval wait and report whether state changed."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if (
                _registry_lifecycle.is_terminal(entry)
                or entry.status == "waiting_for_approval"
            ):
                return _registry_contracts.AgentRunTransition(
                    entry=entry,
                    changed=False,
                )
            updated = self._store(
                _registry_lifecycle.build_waiting_for_approval_entry(
                    entry,
                    accounted_usage_record_count=accounted_usage_record_count,
                )
            )
        await self._signal.notify_after_commit()
        return _registry_contracts.AgentRunTransition(entry=updated, changed=True)

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> _registry_contracts.LocalAgentRun:
        """Set the local cancellation flag and cancel the owned task handle."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _registry_lifecycle.is_terminal(entry):
                return entry
            if entry.task_handle is not None and not entry.task_handle.done():
                entry.task_handle.cancel()
            if entry.status == "waiting_for_approval":
                updated = self._store(
                    _registry_lifecycle.build_cancelled_entry(
                        entry,
                        completed_at=self._clock(),
                        cancel_requested=True,
                    )
                )
                should_notify = True
                return_entry = updated
            elif entry.cancel_requested:
                return entry
            else:
                return_entry = self._store(
                    _registry_lifecycle.build_cancellation_requested_entry(entry)
                )
                should_notify = True
        if should_notify:
            await self._signal.notify_after_commit()
        return return_entry

    async def mark_completed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        result: AgentResult,
    ) -> _registry_contracts.LocalAgentRun:
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
    ) -> _registry_contracts.AgentRunTransition:
        """Store the terminal result and report whether this call changed state."""
        if result.agent_run_id != agent_run_id:
            raise ValueError("result.agent_run_id must match agent_run_id")
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _registry_lifecycle.is_terminal(entry):
                _record_duplicate_terminal_suppressed(
                    entry,
                    requested_status="completed",
                )
                return _registry_contracts.AgentRunTransition(
                    entry=entry,
                    changed=False,
                )
            updated = self._store(
                _registry_lifecycle.build_completed_entry(
                    entry,
                    result=result,
                    completed_at=self._clock(),
                )
            )
        await self._signal.notify_after_commit()
        return _registry_contracts.AgentRunTransition(entry=updated, changed=True)

    async def mark_failed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        safe_error: str,
    ) -> _registry_contracts.LocalAgentRun:
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
    ) -> _registry_contracts.AgentRunTransition:
        """Store a safe terminal error and report whether state changed."""
        safe_error = _require_non_empty(safe_error, "safe_error")
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _registry_lifecycle.is_terminal(entry):
                _record_duplicate_terminal_suppressed(entry, requested_status="failed")
                return _registry_contracts.AgentRunTransition(
                    entry=entry,
                    changed=False,
                )
            updated = self._store(
                _registry_lifecycle.build_failed_entry(
                    entry,
                    safe_error=safe_error,
                    completed_at=self._clock(),
                )
            )
        await self._signal.notify_after_commit()
        return _registry_contracts.AgentRunTransition(entry=updated, changed=True)

    async def mark_cancelled(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> _registry_contracts.LocalAgentRun:
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
    ) -> _registry_contracts.AgentRunTransition:
        """Mark a run cancelled and report whether this call changed state."""
        async with self._lock:
            entry = self._require_entry(
                tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id
            )
            if _registry_lifecycle.is_terminal(entry):
                _record_duplicate_terminal_suppressed(
                    entry,
                    requested_status="cancelled",
                )
                return _registry_contracts.AgentRunTransition(
                    entry=entry,
                    changed=False,
                )
            updated = self._store(
                _registry_lifecycle.build_cancelled_entry(
                    entry,
                    completed_at=self._clock(),
                    cancel_requested=True,
                )
            )
        await self._signal.notify_after_commit()
        return _registry_contracts.AgentRunTransition(entry=updated, changed=True)

    async def claim_ready_handoffs(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str | None = None,
        max_results: int | None = None,
    ) -> _registry_contracts.ClaimedHandoffBatch | None:
        """Atomically claim ready results and snapshot active runs for one task."""
        if max_results is not None and max_results < 1:
            raise ValueError("max_results must be positive")
        async with self._lock:
            projection = _registry_handoffs.select_ready_handoffs(
                tuple(self._runs.values()),
                tenant_id=tenant_id,
                task_id=task_id,
                conversation_id=conversation_id,
                max_results=max_results,
            )
            if not projection.candidates:
                if projection.claimed_ready_count:
                    safe_inc("agent_run_handoff_duplicate_claim_suppressed")
                    safe_gauge(
                        "agent_run_handoff_duplicate_claim_suppressed_count",
                        projection.claimed_ready_count,
                    )
                    logger.info(
                        "Suppressed duplicate handoff claim tenant_id=%s task_id=%s "
                        "conversation_id=%s claimed_ready_count=%s",
                        tenant_id,
                        task_id,
                        conversation_id,
                        projection.claimed_ready_count,
                    )
                return None

            claim_id = self._next_claim_id_locked(
                tenant_id=tenant_id,
                task_id=task_id,
            )
            decision = _registry_handoffs.build_claim_decision(
                projection,
                claim_id=claim_id,
                tenant_id=tenant_id,
                task_id=task_id,
            )
            for replacement in decision.replacements:
                self._store(replacement.entry)
            self._claims[claim_id] = decision.claim_keys
            batch = decision.batch
            safe_inc("agent_run_handoff_claim_created")
            safe_gauge("agent_run_handoff_claim_batch_size", len(batch.agent_run_ids))
            safe_gauge(
                "agent_run_handoff_claim_active_run_count",
                len(batch.active_runs),
            )
            logger.info(
                "Created handoff claim tenant_id=%s task_id=%s "
                "conversation_id=%s claim_id=%s batch_size=%s active_run_count=%s",
                tenant_id,
                task_id,
                conversation_id,
                claim_id,
                len(batch.agent_run_ids),
                len(batch.active_runs),
            )
        await self._signal.notify_after_commit()
        return batch

    async def acknowledge_handoffs(self, claim_id: str) -> None:
        """Mark claimed results applied after parent state updates succeed."""
        claim_id = _require_non_empty(claim_id, "claim_id")
        async with self._lock:
            keys = self._claims.pop(claim_id, None)
            if keys is None:
                safe_inc("agent_run_handoff_acknowledge_missing_claim")
                raise _registry_contracts.HandoffClaimNotFoundError(claim_id)
            acknowledged = 0
            decision = _registry_handoffs.build_acknowledge_replacements(
                self._runs,
                claim_keys=keys,
                claim_id=claim_id,
            )
            for replacement in decision.replacements:
                self._store(replacement.entry)
                acknowledged += 1
            safe_inc("agent_run_handoff_claim_acknowledged")
            safe_gauge("agent_run_handoff_claim_acknowledged_count", acknowledged)
            logger.info(
                "Acknowledged handoff claim claim_id=%s acknowledged_count=%s",
                claim_id,
                acknowledged,
            )
        await self._signal.notify_after_commit()

    async def release_handoffs(self, claim_id: str) -> None:
        """Release a claimed batch so cancellation or failure can retry it."""
        claim_id = _require_non_empty(claim_id, "claim_id")
        async with self._lock:
            keys = self._claims.pop(claim_id, None)
            if keys is None:
                safe_inc("agent_run_handoff_release_missing_claim")
                raise _registry_contracts.HandoffClaimNotFoundError(claim_id)
            released = 0
            decision = _registry_handoffs.build_release_replacements(
                self._runs,
                claim_keys=keys,
                claim_id=claim_id,
            )
            for replacement in decision.replacements:
                self._store(replacement.entry)
                released += 1
            safe_inc("agent_run_handoff_claim_released")
            safe_gauge("agent_run_handoff_claim_released_count", released)
            logger.info(
                "Released handoff claim claim_id=%s released_count=%s",
                claim_id,
                released,
            )
        await self._signal.notify_after_commit()

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
            decision = _registry_handoffs.build_consumption_decision(entry)
            if decision.replacement is None:
                safe_inc("agent_run_handoff_duplicate_delivery_suppressed")
                return None
            self._store(decision.replacement.entry)
            result = decision.result
        await self._signal.notify_after_commit()
        return result

    async def cleanup_finished(self) -> int:
        """Drop finished entries older than the process-local retention window."""
        cutoff = self._clock() - self._finished_retention
        async with self._lock:
            stale_keys = _registry_queries.select_stale_finished_keys(
                self._runs,
                cutoff=cutoff,
            )
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
                self._signal.mark_changed_locked()
            count = len(stale_keys)
        if count:
            await self._signal.notify_after_commit()
        return count

    async def list_task_runs(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> list[_registry_contracts.LocalAgentRun]:
        """List process-local entries for one authorized task scope."""
        async with self._lock:
            return _registry_queries.list_task_runs(
                self._runs.values(),
                tenant_id=tenant_id,
                task_id=task_id,
            )

    async def find_active_by_graph_thread(
        self,
        *,
        task_id: int,
        graph_thread_id: str,
        tenant_id: int | None = None,
    ) -> _registry_contracts.LocalAgentRun | None:
        """Return the active local run for one task child graph thread."""
        graph_thread_id = _require_non_empty(graph_thread_id, "graph_thread_id")
        async with self._lock:
            return _registry_queries.find_active_by_graph_thread(
                self._runs.values(),
                task_id=task_id,
                graph_thread_id=graph_thread_id,
                tenant_id=tenant_id,
            )

    async def state_version(self) -> int:
        """Return the current process-local registry mutation version."""
        return await self._signal.state_version()

    async def wait_for_state_change(self, *, after_version: int) -> int:
        """Wait until the registry mutates after ``after_version``.

        The registry lock is never held while blocked on the notification
        condition, so callers can wait for child lifecycle changes without
        preventing those changes from being recorded.
        """
        return await self._signal.wait_for_state_change(after_version=after_version)

    async def wait_for_ready_handoffs_or_inactive(
        self,
        *,
        tenant_id: int,
        task_id: int,
        conversation_id: str | None = None,
        after_version: int,
    ) -> _registry_contracts.HandoffWaitStatus:
        """Wait for scoped ready handoffs or for scoped active runs to end.

        Notifications for other tenants, tasks, or conversations do not satisfy
        the wait; they only advance the observed registry version before the
        caller blocks again.
        """
        return await self._signal.wait_for_predicate(
            after_version=after_version,
            predicate=lambda: _registry_queries.handoff_wait_status(
                self._runs.values(),
                tenant_id=tenant_id,
                task_id=task_id,
                conversation_id=conversation_id,
            ),
        )

    def _require_entry(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> _registry_contracts.LocalAgentRun:
        key = _key(tenant_id=tenant_id, task_id=task_id, agent_run_id=agent_run_id)
        entry = self._runs.get(key)
        if entry is None:
            raise _registry_contracts.AgentRunNotFoundError(
                f"Process-local subagent run not found for key={key!r}"
            )
        return entry

    def _store(
        self,
        entry: _registry_contracts.LocalAgentRun,
    ) -> _registry_contracts.LocalAgentRun:
        self._runs[
            _key(
                tenant_id=entry.tenant_id,
                task_id=entry.task_id,
                agent_run_id=entry.agent_run_id,
            )
        ] = entry
        self._signal.mark_changed_locked()
        return entry

    def _next_claim_id_locked(self, *, tenant_id: int, task_id: int) -> str:
        self._claim_sequence += 1
        return f"handoff-claim:{tenant_id}:{task_id}:{self._claim_sequence}"

def _key(
    *,
    tenant_id: int,
    task_id: int,
    agent_run_id: str,
) -> _registry_contracts.AgentRunKey:
    return (tenant_id, task_id, agent_run_id)


def _record_duplicate_terminal_suppressed(
    entry: _registry_contracts.LocalAgentRun,
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


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["ProcessLocalAgentRunRegistry"]
