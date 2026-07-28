"""Production worker for process-local Scout recon agent runs.

This module owns Scout child graph execution for the migration-free pilot. It
reuses the existing LangGraph checkpointer, streaming executor, runtime
projection, and process-local registry instead of introducing durable
scheduling or alternate runtime placement.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent.subagents.scout.graph import build_scout_recon_graph
from agent.subagents.scout.nodes.choose_action import SCOUT_RESULT_METADATA_KEY
from agent.subagents.scout.state import build_scout_initial_state
from backend.services.agent_runs.contracts import AgentAssignment, AgentResult
from backend.services.agent_runs.event_projection import build_agent_run_lifecycle_event
from backend.services.agent_runs.launcher import ScoutRunPaused
from backend.services.agent_runs.registry import LocalAgentRun, ProcessLocalAgentRunRegistry
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.execution.graph_executor import (
    GraphExecutionResult,
    LangGraphExecutor,
)
from backend.services.langgraph_chat.hitl_constants import GRAPH_RECURSION_LIMIT
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

logger = logging.getLogger(__name__)


class ProcessLocalScoutRunWorker:
    """Run one registered Scout child graph inside the current backend process."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        checkpointer_service: CheckpointerService | None = None,
        executor: LangGraphExecutor | None = None,
    ) -> None:
        self._registry = registry
        self._checkpointer_service = (
            checkpointer_service or get_shared_checkpointer_service()
        )
        self._executor = executor or LangGraphExecutor(
            streaming_adapter=LangGraphStreamingAdapter(),
        )

    async def __call__(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: Any,
        graph_thread_id: str,
        is_cancel_requested: Callable[[], Awaitable[bool]],
    ) -> AgentResult:
        """Execute Scout until terminal result, cancellation, or approval pause."""

        await self._verify_registered_child(
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
        graph_input = build_scout_initial_state(
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
        config = _prepare_child_config(
            runtime_config,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
        cancellation_probe = _AsyncCancellationProbe(is_cancel_requested)

        async with self._checkpointer_service.get_checkpointer(
            assignment.task_id
        ) as checkpointer:
            compiled = build_scout_recon_graph(checkpointer=checkpointer)
            execution_result = await self._executor.stream_graph(
                compiled,
                graph_input,
                config,
                assignment.task_id,
                state_container=None,
                should_cancel=cancellation_probe,
            )

        if await is_cancel_requested():
            raise asyncio.CancelledError
        if execution_result.interrupted:
            raise ScoutRunPaused(execution_result)
        if not execution_result.final_state:
            raise RuntimeError("Scout graph completed without final state")
        return extract_scout_result_from_state(
            execution_result.final_state,
            expected_agent_run_id=assignment.agent_run_id,
            expected_agent_id=assignment.agent_id,
            expected_agent_kind=assignment.agent_kind,
        )

    async def _verify_registered_child(
        self,
        *,
        assignment: AgentAssignment,
        graph_thread_id: str,
    ) -> None:
        entry = await self._registry.get(
            tenant_id=assignment.tenant_id,
            task_id=assignment.task_id,
            agent_run_id=assignment.agent_run_id,
        )
        if entry is None:
            raise RuntimeError("Scout child run is not registered")
        if entry.graph_thread_id != graph_thread_id:
            raise RuntimeError("Scout child thread does not match registry")


async def mark_scout_completed_from_state(
    *,
    registry: ProcessLocalAgentRunRegistry,
    entry: LocalAgentRun,
    final_state: Mapping[str, Any],
    parent_run_id: str | None = None,
) -> LocalAgentRun:
    """Store and publish a terminal Scout result after HITL continuation."""

    result = extract_scout_result_from_state(
        final_state,
        expected_agent_run_id=entry.agent_run_id,
        expected_agent_id=entry.agent_id,
        expected_agent_kind=entry.agent_kind,
    )
    completed = await registry.mark_completed(
        tenant_id=entry.tenant_id,
        task_id=entry.task_id,
        agent_run_id=entry.agent_run_id,
        result=result,
    )
    await _publish_lifecycle_to_hub(completed, parent_run_id=parent_run_id)
    return completed


def extract_scout_result_from_state(
    final_state: Mapping[str, Any],
    *,
    expected_agent_run_id: str,
    expected_agent_id: str,
    expected_agent_kind: str,
) -> AgentResult:
    """Read Scout's safe terminal result from final graph metadata."""

    facts = final_state.get("facts")
    metadata = facts.get("metadata") if isinstance(facts, Mapping) else None
    result_payload = (
        metadata.get(SCOUT_RESULT_METADATA_KEY) if isinstance(metadata, Mapping) else None
    )
    if not isinstance(result_payload, Mapping):
        raise RuntimeError("Scout graph completed without a terminal result")
    result = AgentResult.model_validate(dict(result_payload))
    if result.agent_run_id != expected_agent_run_id:
        raise RuntimeError("Scout result agent_run_id does not match assignment")
    if result.agent_id != expected_agent_id:
        raise RuntimeError("Scout result agent_id does not match assignment")
    if result.agent_kind != expected_agent_kind:
        raise RuntimeError("Scout result agent_kind does not match assignment")
    return result


def _prepare_child_config(
    runtime_config: Any,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
) -> dict[str, Any]:
    config = {
        key: (dict(value) if isinstance(value, Mapping) else value)
        for key, value in (
            runtime_config.items()
            if isinstance(runtime_config, Mapping)
            else {}
        )
    }
    configurable = dict(config.get("configurable") or {})
    runtime_projection = dict(configurable.get("runtime_projection") or {})
    graph_runtime_context = _graph_runtime_context_from_projection(
        runtime_projection,
        assignment=assignment,
        graph_thread_id=graph_thread_id,
    )
    configurable.setdefault("graph_runtime_context", graph_runtime_context)
    configurable.setdefault("canonical_conversation_id", assignment.conversation_id)
    configurable.setdefault("canonical_turn_id", assignment.parent_turn_id)
    turn_sequence = assignment.relevant_context.get("turn_sequence")
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        configurable.setdefault("canonical_turn_sequence", turn_sequence)
    config["configurable"] = configurable
    config.setdefault("recursion_limit", GRAPH_RECURSION_LIMIT)
    return config


def _graph_runtime_context_from_projection(
    runtime_projection: Mapping[str, Any],
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
) -> dict[str, Any]:
    context = dict(runtime_projection)
    context["task_id"] = assignment.task_id
    context["user_id"] = assignment.runtime_identity.user_id
    context["graph_thread_id"] = graph_thread_id
    context["tenant_id"] = assignment.tenant_id
    context["turn_id"] = assignment.parent_turn_id
    turn_sequence = assignment.relevant_context.get("turn_sequence")
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        context["turn_sequence"] = turn_sequence
    context.pop("credential_ref", None)
    return context


async def _publish_lifecycle_to_hub(
    entry: LocalAgentRun,
    *,
    parent_run_id: str | None,
) -> None:
    event = build_agent_run_lifecycle_event(entry, parent_run_id=parent_run_id)
    await get_in_memory_stream_hub().publish(entry.task_id, event)


class _AsyncCancellationProbe:
    """Expose an async cancellation check through the executor's sync callback."""

    def __init__(self, check: Callable[[], Awaitable[bool]]) -> None:
        self._check = check
        self._cancelled = False
        self._pending: asyncio.Task[bool] | None = None

    def __call__(self) -> bool:
        if self._cancelled:
            return True
        if self._pending is not None and self._pending.done():
            try:
                self._cancelled = bool(self._pending.result())
            except Exception:
                logger.debug("Scout cancellation probe failed", exc_info=True)
            finally:
                self._pending = None
        if self._pending is None:
            self._pending = asyncio.create_task(self._check())
        return self._cancelled


__all__ = [
    "ProcessLocalScoutRunWorker",
    "extract_scout_result_from_state",
    "mark_scout_completed_from_state",
]
