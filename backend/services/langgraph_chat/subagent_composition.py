"""Compose the process-local subagent chat branch from focused services.

This module is the construction boundary for the subagent branch. It wires the
worker, launcher, dispatcher, handoff coordinator, and parent finalizer while
keeping those construction decisions out of the chat adapter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.services.agent_runs.dispatch_service import (
    AgentRunLaunchService,
    LifecyclePublisher,
    SubagentDispatchService,
)
from backend.services.agent_runs.launcher import AgentRunLauncher, AgentRunWorker
from backend.services.agent_runs.parent_handoff_coordinator import (
    ParentHandoffCoordinator,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import AgentRunResultProjector
from backend.services.agent_runs.worker import ProcessLocalAgentRunWorker

from .checkpoint.checkpointer_service import CheckpointerService
from .execution.graph_executor import LangGraphExecutor
from .execution.subagent_parent_finalizer import SubagentParentFinalizer
from .handlers.base_handler import build_cancellation_checker
from .handlers.subagent_handler import SubagentHandler
from .streaming.adapter import LangGraphStreamingAdapter


ParentProgressPublisher = Callable[
    [int, tuple[dict[str, Any], ...]], Awaitable[None]
]


def build_subagent_handler(
    checkpointer_service: CheckpointerService,
    executor: LangGraphExecutor,
    streaming_adapter: LangGraphStreamingAdapter,
    *,
    registry: ProcessLocalAgentRunRegistry,
    launcher: AgentRunLaunchService | None = None,
    worker: AgentRunWorker | None = None,
    lifecycle_publisher: LifecyclePublisher | None = None,
    result_projector: AgentRunResultProjector | None = None,
    subagent_registry: SubagentRegistry | None = None,
) -> SubagentHandler:
    """Build the subagent chat adapter and its process-local collaborators."""
    publisher = lifecycle_publisher or publish_agent_run_event_to_hub
    definitions = subagent_registry or get_subagent_registry()
    projector = result_projector or AgentRunResultProjector(registry=registry)
    resolved_launcher = launcher
    if resolved_launcher is None:
        resolved_worker = worker or ProcessLocalAgentRunWorker(
            registry=registry,
            checkpointer_service=checkpointer_service,
            executor=executor,
        )
        resolved_launcher = AgentRunLauncher(
            registry=registry,
            worker=resolved_worker,
            lifecycle_publisher=publisher,
        )

    dispatch_service = SubagentDispatchService(
        registry=registry,
        launcher=resolved_launcher,
        subagent_registry=definitions,
        lifecycle_publisher=publisher,
    )
    handoff_coordinator = ParentHandoffCoordinator(
        registry=registry,
        result_projector=projector,
        parent_progress_publisher=_parent_progress_publisher(publisher),
    )
    parent_finalizer = SubagentParentFinalizer(
        executor=executor,
        cancellation_checker_factory=build_cancellation_checker,
    )
    return SubagentHandler(
        checkpointer_service,
        executor,
        streaming_adapter,
        subagent_registry=definitions,
        dispatch_service=dispatch_service,
        parent_handoff_coordinator=handoff_coordinator,
        parent_finalizer=parent_finalizer,
    )


async def publish_agent_run_event_to_hub(
    task_id: int,
    event: dict[str, Any],
) -> None:
    """Publish one agent-run event through the existing task stream hub."""
    from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

    await get_in_memory_stream_hub().publish(task_id, event)


def _parent_progress_publisher(
    publisher: LifecyclePublisher,
) -> ParentProgressPublisher:
    async def publish(
        task_id: int,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        for event in events:
            await publisher(task_id, event)

    return publish


__all__ = ["build_subagent_handler", "publish_agent_run_event_to_hub"]
