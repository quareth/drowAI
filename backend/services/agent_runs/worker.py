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

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent.subagents.definition import SubagentDefinition
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from agent.subagents.runtime.graph import build_subagent_graph
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from agent.subagents.runtime.state import build_subagent_initial_state
from backend.services.agent_runs.contracts import AgentAssignment, AgentResult
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    build_agent_run_completion,
)
from backend.services.agent_runs.event_projection import build_agent_run_lifecycle_event
from backend.services.agent_runs.launcher import (
    LifecyclePublisher,
    SubagentRunCancelled,
    SubagentRunInterrupted,
    SubagentRunPaused,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.registry_contracts import LocalAgentRun
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.hitl_constants import GRAPH_RECURSION_LIMIT
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter
from runtime_shared.shell_session_contracts import format_shell_execution_owner_id

from .cancellation import AsyncCancellationProbe

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
    ) -> AgentRunCompletion:
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
        cancellation_probe = AsyncCancellationProbe(is_cancel_requested)

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
            raise SubagentRunCancelled(execution_result)
        if execution_result.interrupted:
            raise SubagentRunPaused(execution_result)
        if not execution_result.final_state:
            raise SubagentRunInterrupted(
                "Subagent graph completed without final state",
                execution_result,
            )
        try:
            result = extract_subagent_result_from_state(
                execution_result.final_state,
                assignment=assignment,
            )
        except Exception as exc:
            raise SubagentRunInterrupted(
                "Subagent graph completed without a valid terminal result",
                execution_result,
            ) from exc
        return build_agent_run_completion(
            result=result,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
            final_state=execution_result.final_state,
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
    definition_registry: SubagentRegistry,
    entry: LocalAgentRun,
    final_state: Mapping[str, Any],
    lifecycle_publisher: LifecyclePublisher,
    parent_run_id: str | None = None,
) -> AgentRunCompletion:
    """Store, publish, and return terminal subagent completion after HITL resume."""

    result = extract_subagent_result_from_state(
        final_state,
        assignment=entry.assignment,
    )
    completion = build_agent_run_completion(
        result=result,
        assignment=entry.assignment,
        graph_thread_id=entry.graph_thread_id,
        final_state=final_state,
        skip_usage_records=entry.accounted_usage_record_count,
    )
    completed = await registry.mark_completed(
        tenant_id=entry.tenant_id,
        task_id=entry.task_id,
        agent_run_id=entry.agent_run_id,
        result=completion.result,
    )
    event = build_agent_run_lifecycle_event(
        completed,
        display_metadata=definition_registry.display_metadata(completed.agent_id),
        parent_run_id=parent_run_id,
    )
    try:
        await lifecycle_publisher(completed.task_id, event)
    except Exception:
        logger.debug(
            "Subagent lifecycle publish failed after approval continuation "
            "for tenant_id=%s task_id=%s agent_run_id=%s",
            completed.tenant_id,
            completed.task_id,
            completed.agent_run_id,
            exc_info=True,
        )
    return completion


def resolve_definition_for_assignment(
    definition_registry: SubagentRegistry,
    *,
    assignment: AgentAssignment,
) -> SubagentDefinition:
    """Return the single enabled definition matching assignment identity."""

    definition = definition_registry.get(assignment.agent_id)
    if definition is None:
        raise RuntimeError(
            "No subagent definition matches assignment agent_id "
            f"{assignment.agent_id!r}"
        )
    if definition.kind != assignment.agent_kind:
        raise RuntimeError("Subagent definition kind does not match assignment")
    return definition


def extract_subagent_result_from_state(
    final_state: Mapping[str, Any],
    *,
    assignment: AgentAssignment,
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
    if result.agent_run_id != assignment.agent_run_id:
        raise RuntimeError("Subagent result agent_run_id does not match assignment")
    if result.agent_id != assignment.agent_id:
        raise RuntimeError("Subagent result agent_id does not match assignment")
    if result.agent_kind != assignment.agent_kind:
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
    configurable["graph_runtime_context"] = graph_runtime_context
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
    context["execution_owner_id"] = format_shell_execution_owner_id(
        "subagent", assignment.agent_run_id
    )
    turn_sequence = assignment.relevant_context.get("turn_sequence")
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        context["turn_sequence"] = turn_sequence
    context.pop("credential_ref", None)
    return context


__all__ = [
    "ProcessLocalAgentRunWorker",
    "extract_subagent_result_from_state",
    "mark_subagent_completed_from_state",
    "prepare_subagent_child_config",
    "resolve_definition_for_assignment",
]
