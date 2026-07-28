"""Generic process-local worker for declarative subagent runs.

Purpose
-------
Execute one registered process-local child graph by resolving its declarative
definition from the assignment identity and reusing the existing checkpointer,
streaming executor, cancellation, HITL pause, and lifecycle event boundaries.

Responsibility boundary
-----------------------
This module owns only process-local graph execution and terminal result
extraction for generic subagent definitions. It does not launch local tasks,
store durable coordination state, authorize requests, or choose between legacy
and generic execution stacks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent.subagents.definition import SubagentDefinition
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from agent.subagents.runtime.graph import build_subagent_graph
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from agent.subagents.runtime.state import build_subagent_initial_state
from backend.services.agent_runs.contracts import AgentAssignment, AgentResult
from backend.services.agent_runs.event_projection import build_agent_run_lifecycle_event
from backend.services.agent_runs.launcher import SubagentRunPaused
from backend.services.agent_runs.registry import LocalAgentRun, ProcessLocalAgentRunRegistry
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.hitl_constants import GRAPH_RECURSION_LIMIT
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

logger = logging.getLogger(__name__)


class ProcessLocalAgentRunWorker:
    """Run one registered declarative subagent graph in the current process."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        definition_registry: SubagentRegistry | None = None,
        checkpointer_service: CheckpointerService | None = None,
        executor: LangGraphExecutor | None = None,
    ) -> None:
        self._registry = registry
        self._definition_registry = definition_registry or get_subagent_registry()
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
        """Execute a subagent until terminal result, cancellation, or HITL pause."""

        definition = resolve_definition_for_assignment(
            self._definition_registry,
            assignment=assignment,
        )
        await self._verify_registered_child(
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
        graph_input = build_subagent_initial_state(
            definition=definition,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
        config = prepare_subagent_child_config(
            runtime_config,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )
        cancellation_probe = _AsyncCancellationProbe(is_cancel_requested)

        async with self._checkpointer_service.get_checkpointer(
            assignment.task_id
        ) as checkpointer:
            compiled = build_subagent_graph(definition, checkpointer=checkpointer)
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
            raise SubagentRunPaused(execution_result)
        if not execution_result.final_state:
            raise RuntimeError("Subagent graph completed without final state")
        return extract_subagent_result_from_state(
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
            raise RuntimeError("Subagent child run is not registered")
        if entry.graph_thread_id != graph_thread_id:
            raise RuntimeError("Subagent child thread does not match registry")


async def mark_subagent_completed_from_state(
    *,
    registry: ProcessLocalAgentRunRegistry,
    entry: LocalAgentRun,
    final_state: Mapping[str, Any],
    parent_run_id: str | None = None,
) -> LocalAgentRun:
    """Store and publish a terminal subagent result after HITL continuation."""

    result = extract_subagent_result_from_state(
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


def resolve_definition_for_assignment(
    definition_registry: SubagentRegistry,
    *,
    assignment: AgentAssignment,
) -> SubagentDefinition:
    """Return the single enabled definition matching assignment identity."""

    matches = tuple(
        definition
        for definition in definition_registry.definitions()
        if definition.id == assignment.agent_id
    )
    if not matches:
        raise RuntimeError(
            "No subagent definition matches assignment agent_id "
            f"{assignment.agent_id!r}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            "Multiple subagent definitions match assignment agent_id "
            f"{assignment.agent_id!r}"
        )
    definition = matches[0]
    if definition.kind != assignment.agent_kind:
        raise RuntimeError("Subagent definition kind does not match assignment")
    return definition


def extract_subagent_result_from_state(
    final_state: Mapping[str, Any],
    *,
    expected_agent_run_id: str,
    expected_agent_id: str,
    expected_agent_kind: str,
) -> AgentResult:
    """Read a generic subagent terminal result from final graph metadata."""

    facts = final_state.get("facts")
    metadata = facts.get("metadata") if isinstance(facts, Mapping) else None
    result_payload = (
        metadata.get(SUBAGENT_RESULT_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(result_payload, Mapping):
        raise RuntimeError("Subagent graph completed without a terminal result")
    result = AgentResult.model_validate(dict(result_payload))
    if result.agent_run_id != expected_agent_run_id:
        raise RuntimeError("Subagent result agent_run_id does not match assignment")
    if result.agent_id != expected_agent_id:
        raise RuntimeError("Subagent result agent_id does not match assignment")
    if result.agent_kind != expected_agent_kind:
        raise RuntimeError("Subagent result agent_kind does not match assignment")
    return result


def prepare_subagent_child_config(
    runtime_config: Any,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
) -> dict[str, Any]:
    """Return child graph config with safe runtime identity projection."""

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
                logger.debug("Subagent cancellation probe failed", exc_info=True)
            finally:
                self._pending = None
        if self._pending is None:
            self._pending = asyncio.create_task(self._check())
        return self._cancelled


__all__ = [
    "ProcessLocalAgentRunWorker",
    "extract_subagent_result_from_state",
    "mark_subagent_completed_from_state",
    "prepare_subagent_child_config",
    "resolve_definition_for_assignment",
]
