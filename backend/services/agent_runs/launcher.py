"""Process-local asyncio launcher for subagent runs.

The launcher owns local task creation, handle attachment, cancellation
signaling, and terminal registry cleanup for process-local subagent runs.
Graph execution is supplied as an injectable worker so this module stays
inside the process-local lifecycle boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Coroutine, Protocol

from agent.subagents.registry import SubagentRegistry

from .contracts import AgentAssignment
from .completion import AgentRunCompletion, child_usage_records_from_state
from .event_projection import build_agent_run_lifecycle_event
from .registry import ProcessLocalAgentRunRegistry
from .registry_contracts import LocalAgentRun

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
    ) -> AgentRunCompletion:
        """Run subagent work and return a safe terminal result."""


TaskFactory = Callable[
    [Coroutine[Any, Any, AgentRunCompletion]], asyncio.Task[AgentRunCompletion]
]
LifecyclePublisher = Callable[[int, dict[str, Any]], Awaitable[None]]


class SubagentRunPaused(RuntimeError):
    """Raised by a worker when a subagent is waiting on shared HITL approval."""

    def __init__(self, execution_result: Any) -> None:
        super().__init__("Subagent run paused for approval")
        self.execution_result = execution_result


class SubagentRunCancelled(asyncio.CancelledError):
    """Raised by a worker when cancellation occurs after child graph execution."""

    def __init__(self, execution_result: Any) -> None:
        super().__init__("Subagent run cancelled after graph execution")
        self.execution_result = execution_result


class SubagentRunFailed(RuntimeError):
    """Raised when a child graph fails after producing inspectable graph state."""

    def __init__(self, message: str, execution_result: Any) -> None:
        super().__init__(message)
        self.execution_result = execution_result


class AgentRunLauncher:
    """Create one task that executes and settles each local subagent run."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        subagent_registry: SubagentRegistry,
        worker: AgentRunWorker | None = None,
        task_factory: TaskFactory = asyncio.create_task,
        lifecycle_publisher: LifecyclePublisher | None = None,
    ) -> None:
        self._registry = registry
        self._subagent_registry = subagent_registry
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
    ) -> asyncio.Task[AgentRunCompletion]:
        """Create the local subagent task and attach its handle to the registry."""
        lifecycle_started = asyncio.Event()
        attachment_finished = asyncio.Event()
        worker_start = asyncio.Event()
        task_coro = self._run_lifecycle(
            assignment=assignment,
            runtime_config=runtime_config,
            graph_thread_id=graph_thread_id,
            parent_run_id=parent_run_id,
            lifecycle_started=lifecycle_started,
            attachment_finished=attachment_finished,
            worker_start=worker_start,
        )
        try:
            task = self._task_factory(task_coro)
        except Exception:
            task_coro.close()
            raise

        await lifecycle_started.wait()
        try:
            await self._registry.attach_task_handle(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                task_handle=task,
            )
        except Exception:
            attachment_finished.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
        worker_start.set()
        attachment_finished.set()
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

    async def _run_lifecycle(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: Any,
        graph_thread_id: str,
        parent_run_id: str | None,
        lifecycle_started: asyncio.Event,
        attachment_finished: asyncio.Event,
        worker_start: asyncio.Event,
    ) -> AgentRunCompletion:
        """Run the worker, settle its lifecycle, then preserve its outcome."""
        lifecycle_started.set()
        try:
            await worker_start.wait()
            completion = await self._worker(
                assignment=assignment,
                runtime_config=runtime_config,
                graph_thread_id=graph_thread_id,
                is_cancel_requested=lambda: self.is_cancel_requested(assignment),
            )
        except asyncio.CancelledError:
            await attachment_finished.wait()
            if not worker_start.is_set():
                raise
            transition = await self._registry.record_cancelled(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            if transition.changed:
                await self._publish_terminal_lifecycle(
                    transition.entry,
                    parent_run_id=parent_run_id,
                )
            raise
        except SubagentRunPaused as exc:
            usage_records = child_usage_records_from_state(
                getattr(exc.execution_result, "final_state", None),
                assignment=assignment,
                graph_thread_id=graph_thread_id,
            )
            transition = await self._registry.record_waiting_for_approval(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                accounted_usage_record_count=len(usage_records),
            )
            if transition.changed:
                await self._publish_terminal_lifecycle(
                    transition.entry,
                    parent_run_id=parent_run_id,
                )
            raise
        except Exception:
            logger.warning(
                "Subagent worker failed for tenant_id=%s task_id=%s agent_run_id=%s",
                assignment.tenant_id,
                assignment.task_id,
                assignment.agent_run_id,
                exc_info=True,
            )
            transition = await self._registry.record_failed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                safe_error="Subagent worker failed",
            )
            if transition.changed:
                await self._publish_terminal_lifecycle(
                    transition.entry,
                    parent_run_id=parent_run_id,
                )
            raise

        transition = await self._registry.record_completed(
            tenant_id=assignment.tenant_id,
            task_id=assignment.task_id,
            agent_run_id=assignment.agent_run_id,
            result=completion.result,
        )
        if transition.changed:
            await self._publish_terminal_lifecycle(
                transition.entry,
                parent_run_id=parent_run_id,
            )
        return completion

    async def _publish_terminal_lifecycle(
        self,
        entry: LocalAgentRun,
        *,
        parent_run_id: str | None,
    ) -> None:
        if self._publish_lifecycle is None:
            return
        event = build_agent_run_lifecycle_event(
            entry,
            display_metadata=self._subagent_registry.display_metadata(entry.agent_id),
            parent_run_id=parent_run_id,
        )
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

async def _unavailable_worker(
    *,
    assignment: AgentAssignment,
    runtime_config: Any,
    graph_thread_id: str,
    is_cancel_requested: Callable[[], Awaitable[bool]],
) -> AgentRunCompletion:
    _ = (assignment, runtime_config, graph_thread_id, is_cancel_requested)
    raise RuntimeError("Subagent worker is not configured")


__all__ = [
    "AgentRunLauncher",
    "AgentRunWorker",
    "LifecyclePublisher",
    "SubagentRunCancelled",
    "SubagentRunFailed",
    "SubagentRunPaused",
]
