"""Process-local asyncio launcher for subagent runs.

The launcher owns local task creation, handle attachment, cancellation
signaling, and terminal registry cleanup for process-local subagent runs.
Graph execution is supplied as an injectable worker so this module stays
inside the process-local lifecycle boundary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Coroutine, Protocol

from .contracts import AgentAssignment, AgentResult
from .event_projection import build_agent_run_lifecycle_event
from .registry import LocalAgentRun, ProcessLocalAgentRunRegistry

logger = logging.getLogger(__name__)


class AgentRunWorker(Protocol):
    """Callable boundary for process-local subagent graph workers."""

    async def __call__(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: Any,
        graph_thread_id: str,
        is_cancel_requested: Callable[[], Awaitable[bool]],
    ) -> AgentResult:
        """Run subagent work and return a safe terminal result."""


TaskFactory = Callable[[Coroutine[Any, Any, AgentResult]], asyncio.Task[AgentResult]]
LifecyclePublisher = Callable[[int, dict[str, Any]], Awaitable[None]]


class SubagentRunPaused(RuntimeError):
    """Raised by a worker when a subagent is waiting on shared HITL approval."""

    def __init__(self, execution_result: Any) -> None:
        super().__init__("Subagent run paused for approval")
        self.execution_result = execution_result


class AgentRunLauncher:
    """Creates and observes one local asyncio task per subagent assignment."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        worker: AgentRunWorker | None = None,
        task_factory: TaskFactory = asyncio.create_task,
        lifecycle_publisher: LifecyclePublisher | None = None,
    ) -> None:
        self._registry = registry
        self._worker = worker or _unavailable_worker
        self._task_factory = task_factory
        self._publish_lifecycle = lifecycle_publisher

    async def launch(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: Any,
        graph_thread_id: str,
        parent_run_id: str | None = None,
    ) -> asyncio.Task[AgentResult]:
        """Create the local subagent task and attach its handle to the registry."""
        task_coro = self._run_worker(
            assignment=assignment,
            runtime_config=runtime_config,
            graph_thread_id=graph_thread_id,
        )
        try:
            task = self._task_factory(task_coro)
        except Exception:
            task_coro.close()
            raise

        try:
            await self._registry.attach_task_handle(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                task_handle=task,
            )
        except Exception:
            task.cancel()
            raise
        task.add_done_callback(
            lambda completed: self._schedule_completion(
                assignment,
                completed,
                parent_run_id=parent_run_id,
            )
        )
        return task

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        """Signal cancellation for exactly one process-local subagent run."""
        previous = await self._registry.get(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        entry = await self._registry.request_cancellation(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if (
            entry.status == "cancelled"
            and previous is not None
            and entry.lifecycle_version > previous.lifecycle_version
        ):
            await self._publish_terminal_lifecycle(entry, parent_run_id=None)
        return entry

    async def is_cancel_requested(self, assignment: AgentAssignment) -> bool:
        """Return whether cancellation was requested for this assignment."""
        entry = await self._registry.get(
            tenant_id=assignment.tenant_id,
            task_id=assignment.task_id,
            agent_run_id=assignment.agent_run_id,
        )
        return bool(entry and entry.cancel_requested)

    async def _run_worker(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: Any,
        graph_thread_id: str,
    ) -> AgentResult:
        return await self._worker(
            assignment=assignment,
            runtime_config=runtime_config,
            graph_thread_id=graph_thread_id,
            is_cancel_requested=lambda: self.is_cancel_requested(assignment),
        )

    def _schedule_completion(
        self,
        assignment: AgentAssignment,
        completed: asyncio.Task[AgentResult],
        *,
        parent_run_id: str | None,
    ) -> None:
        cleanup = asyncio.create_task(
            self._complete_task(
                assignment,
                completed,
                parent_run_id=parent_run_id,
            )
        )
        cleanup.add_done_callback(
            lambda task: self._log_cleanup_failure(assignment, task)
        )

    async def _complete_task(
        self,
        assignment: AgentAssignment,
        completed: asyncio.Task[AgentResult],
        *,
        parent_run_id: str | None,
    ) -> None:
        try:
            result = completed.result()
        except asyncio.CancelledError:
            entry = await self._registry.mark_cancelled(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            await self._publish_terminal_lifecycle(entry, parent_run_id=parent_run_id)
            return
        except SubagentRunPaused:
            entry = await self._registry.mark_waiting_for_approval(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            await self._publish_terminal_lifecycle(entry, parent_run_id=parent_run_id)
            return
        except Exception:
            logger.warning(
                "Subagent worker failed for tenant_id=%s task_id=%s agent_run_id=%s",
                assignment.tenant_id,
                assignment.task_id,
                assignment.agent_run_id,
                exc_info=True,
            )
            entry = await self._registry.mark_failed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                safe_error="Subagent worker failed",
            )
            await self._publish_terminal_lifecycle(entry, parent_run_id=parent_run_id)
            return

        entry = await self._registry.mark_completed(
            tenant_id=assignment.tenant_id,
            task_id=assignment.task_id,
            agent_run_id=assignment.agent_run_id,
            result=result,
        )
        await self._publish_terminal_lifecycle(entry, parent_run_id=parent_run_id)

    async def _publish_terminal_lifecycle(
        self,
        entry: LocalAgentRun,
        *,
        parent_run_id: str | None,
    ) -> None:
        if self._publish_lifecycle is None:
            return
        event = build_agent_run_lifecycle_event(entry, parent_run_id=parent_run_id)
        try:
            await self._publish_lifecycle(entry.task_id, event)
        except Exception:
            logger.debug(
                "Subagent lifecycle publish failed for tenant_id=%s task_id=%s agent_run_id=%s",
                entry.tenant_id,
                entry.task_id,
                entry.agent_run_id,
                exc_info=True,
            )

    def _log_cleanup_failure(
        self,
        assignment: AgentAssignment,
        cleanup: asyncio.Task[None],
    ) -> None:
        try:
            cleanup.result()
        except Exception:
            logger.exception(
                "Subagent completion cleanup failed for tenant_id=%s task_id=%s agent_run_id=%s",
                assignment.tenant_id,
                assignment.task_id,
                assignment.agent_run_id,
            )


async def _unavailable_worker(
    *,
    assignment: AgentAssignment,
    runtime_config: Any,
    graph_thread_id: str,
    is_cancel_requested: Callable[[], Awaitable[bool]],
) -> AgentResult:
    _ = (assignment, runtime_config, graph_thread_id, is_cancel_requested)
    raise RuntimeError("Subagent worker is not configured")


__all__ = [
    "AgentRunLauncher",
    "AgentRunWorker",
    "LifecyclePublisher",
    "SubagentRunPaused",
]
