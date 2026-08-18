"""Shared process-local subagent runtime objects for the backend process.

This module owns the single in-memory registry used by process-local subagent
runs when no test-specific registry is injected. The objects are intentionally
process-local and are not durable or distributed coordination primitives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter
from core.skills.registry import SkillRegistry, get_skill_registry

from .launcher import AgentRunLauncher, LifecyclePublisher
from .parent_handoff_continuation import ParentHandoffContinuationBroker
from .parent_handoff_coordinator import ParentHandoffGuardPool
from .registry import ProcessLocalAgentRunRegistry
from .worker import ProcessLocalAgentRunWorker


@dataclass(frozen=True, slots=True)
class ProcessLocalAgentRunRuntime:
    """Shared process-local collaborators for subagent execution and control."""

    registry: ProcessLocalAgentRunRegistry
    subagent_registry: SubagentRegistry
    skill_registry: SkillRegistry
    streaming_adapter: LangGraphStreamingAdapter
    executor: LangGraphExecutor
    worker: ProcessLocalAgentRunWorker
    launcher: AgentRunLauncher
    lifecycle_publisher: LifecyclePublisher
    parent_handoff_guard_pool: ParentHandoffGuardPool
    parent_handoff_continuation_broker: ParentHandoffContinuationBroker


_PROCESS_LOCAL_RUNTIME: ProcessLocalAgentRunRuntime | None = None


async def publish_process_local_agent_run_event(
    task_id: int,
    event: dict[str, Any],
) -> None:
    """Publish one process-local agent-run event through the task stream hub."""
    from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

    await get_in_memory_stream_hub().publish(task_id, event)


def get_process_local_agent_run_runtime() -> ProcessLocalAgentRunRuntime:
    """Return the lazily composed process-local subagent runtime."""
    global _PROCESS_LOCAL_RUNTIME
    if _PROCESS_LOCAL_RUNTIME is None:
        subagent_registry = get_subagent_registry()
        skill_registry = get_skill_registry()
        registry = ProcessLocalAgentRunRegistry()
        streaming_adapter = LangGraphStreamingAdapter()
        executor = LangGraphExecutor(streaming_adapter=streaming_adapter)
        worker = ProcessLocalAgentRunWorker(
            registry=registry,
            definition_registry=subagent_registry,
            checkpointer_service=get_shared_checkpointer_service(),
            executor=executor,
            skill_registry=skill_registry,
        )
        launcher = AgentRunLauncher(
            registry=registry,
            subagent_registry=subagent_registry,
            worker=worker,
            lifecycle_publisher=publish_process_local_agent_run_event,
        )
        parent_handoff_guard_pool = ParentHandoffGuardPool()
        parent_handoff_continuation_broker = ParentHandoffContinuationBroker()
        _PROCESS_LOCAL_RUNTIME = ProcessLocalAgentRunRuntime(
            registry=registry,
            subagent_registry=subagent_registry,
            skill_registry=skill_registry,
            streaming_adapter=streaming_adapter,
            executor=executor,
            worker=worker,
            launcher=launcher,
            lifecycle_publisher=publish_process_local_agent_run_event,
            parent_handoff_guard_pool=parent_handoff_guard_pool,
            parent_handoff_continuation_broker=parent_handoff_continuation_broker,
        )
    return _PROCESS_LOCAL_RUNTIME


def get_process_local_agent_run_registry() -> ProcessLocalAgentRunRegistry:
    """Return this process' shared subagent registry."""
    return get_process_local_agent_run_runtime().registry


def get_process_local_agent_run_launcher() -> AgentRunLauncher:
    """Return this process' shared launcher facade for scoped cancellation."""
    return get_process_local_agent_run_runtime().launcher


__all__ = [
    "ProcessLocalAgentRunRuntime",
    "get_process_local_agent_run_launcher",
    "get_process_local_agent_run_registry",
    "get_process_local_agent_run_runtime",
    "publish_process_local_agent_run_event",
]
