"""Facade handler for process-local subagent runs and parent handoff.

The handler keeps the original parent turn open while the subagent executes. It
streams its own attributed events, returns a bounded ``AgentResult``, and the
handler projects that result into the parent context before running the existing
main finalizer. Subagent graph execution and lifecycle cleanup remain launcher
responsibilities.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.graph import InteractiveState
from agent.subagents.registry import SubagentRegistry
from agent.graph.context.contracts import ActiveAgentRun
from backend.services.agent_runs.dispatch_contracts import (
    AgentRunDispatchStop,
)
from backend.services.agent_runs.dispatch_service import (
    SubagentDispatchService,
)
from backend.services.agent_runs.dispatch_plan import (
    build_dispatch_plan,
)
from backend.services.agent_runs.completion import AgentRunCompletion
from backend.services.agent_runs.parent_handoff_coordinator import (
    ParentHandoffCoordinator,
    ParentHandoffOutcome,
)
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
)
from backend.services.langgraph_chat.contracts import (
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.execution.subagent_parent_finalizer import (
    SubagentParentFinalizer,
)
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter

from .base_handler import BaseLangGraphHandler
from .turn_runtime import (
    ensure_turn_identity,
)

logger = logging.getLogger(__name__)


class SubagentHandler(BaseLangGraphHandler):
    """Run subagent and finalize its bounded result in the original parent turn."""

    def __init__(
        self,
        checkpointer_service: CheckpointerService,
        executor: LangGraphExecutor,
        streaming_adapter: LangGraphStreamingAdapter,
        *,
        subagent_registry: SubagentRegistry,
        dispatch_service: SubagentDispatchService,
        parent_handoff_coordinator: ParentHandoffCoordinator,
        parent_finalizer: SubagentParentFinalizer,
    ) -> None:
        super().__init__(
            checkpointer_service,
            executor,
            streaming_adapter,
        )
        self._subagent_registry = subagent_registry
        self._dispatch_service = dispatch_service
        self._parent_handoff_coordinator = parent_handoff_coordinator
        self._parent_finalizer = parent_finalizer

    async def handle(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        continuation_state: InteractiveState | None = None,
    ) -> LangGraphChatResult:
        """Run requested subagents, hand bounded results to the parent, then finalize.

        ``continuation_state`` preserves already-accumulated direct-execution
        progress when PTR delegates through the classifier handoff pipeline.
        """
        chat_inputs = runtime_config.chat_inputs
        turn = ensure_turn_identity(runtime_config, logger_=logger)

        plan = build_dispatch_plan(
            runtime_config,
            parent_turn_id=str(turn.turn_id),
            subagent_registry=self._subagent_registry,
        )
        tenant_id = int(runtime_config.metadata["tenant_id"])
        child_completion_by_run_id: dict[str, AgentRunCompletion] = {}
        parent_continuation_state = continuation_state

        async def run_parent_continuation(
            handoff: Any,
            _active_runs: tuple[ActiveAgentRun, ...],
        ) -> LangGraphChatResult:
            nonlocal parent_continuation_state
            child_completions = await self._dispatch_service.completions_for_handoff(
                handoff,
                tenant_id=tenant_id,
                task_id=chat_inputs.task_id,
                completion_by_run_id=child_completion_by_run_id,
            )
            result = await self._parent_finalizer.finalize(
                runtime_config,
                turn=turn,
                child_completions=child_completions,
                continuation_state=parent_continuation_state,
            )
            if result.interactive_state is not None:
                parent_continuation_state = result.interactive_state
            return result

        async def process_ready_handoffs(
            child_completions: tuple[AgentRunCompletion, ...],
            wait_for_initial_handoff: bool = False,
        ) -> ParentHandoffOutcome | None:
            for completion in child_completions:
                child_completion_by_run_id[completion.result.agent_run_id] = completion
            return await self._parent_handoff_coordinator.process_ready_handoffs(
                tenant_id=tenant_id,
                task_id=chat_inputs.task_id,
                conversation_id=chat_inputs.conversation_id or "",
                parent_turn_id=str(turn.turn_id),
                metadata=runtime_config.metadata,
                run_parent_continuation=run_parent_continuation,
                dispatch_followup_delegation=lambda agent_handoff, decision_id: (
                    self._dispatch_service.dispatch_followup(
                        runtime_config,
                        parent_turn_id=str(turn.turn_id),
                        agent_handoff=agent_handoff,
                        decision_id=decision_id,
                    )
                ),
                child_completions=child_completions,
                wait_for_initial_handoff=wait_for_initial_handoff,
            )

        async def project_ready_handoffs(
            child_completions: tuple[AgentRunCompletion, ...],
        ) -> None:
            for completion in child_completions:
                child_completion_by_run_id[completion.result.agent_run_id] = completion
            await self._parent_handoff_coordinator.project_ready_handoffs(
                tenant_id=tenant_id,
                task_id=chat_inputs.task_id,
                conversation_id=chat_inputs.conversation_id or "",
                parent_turn_id=str(turn.turn_id),
                metadata=runtime_config.metadata,
            )

        dispatch_result = await self._dispatch_service.dispatch(
            plan,
            runtime_config,
            parent_turn_sequence=(
                turn.turn_number if isinstance(turn.turn_number, int) else None
            ),
            process_ready_handoffs=process_ready_handoffs,
            project_ready_handoffs=project_ready_handoffs,
        )
        if dispatch_result.stop is not None:
            return _ack_for_dispatch_stop(
                dispatch_result.stop,
                runtime_config=runtime_config,
                turn_id=str(turn.turn_id),
                turn_sequence=(
                    turn.turn_number if isinstance(turn.turn_number, int) else None
                ),
            )

        child_completions = dispatch_result.child_completions
        outcome = dispatch_result.parent_handoff_outcome
        if outcome is None:
            outcome = await process_ready_handoffs(child_completions, False)
        if outcome is None:
            raise RuntimeError("No completed subagent handoff was available to process")
        return outcome.result

def _ack_result(
    runtime_config: LangGraphRuntimeConfig,
    *,
    turn_id: str,
    turn_sequence: int | None,
    agent_run_id: str,
    agent_id: str,
    agent_kind: str,
    agent_display_name: str,
    graph_thread_id: str,
    status: str,
    usage: list[Any] | None = None,
) -> LangGraphChatResult:
    conversation_id = runtime_config.chat_inputs.conversation_id
    metadata = attach_conversation_ids(
        {
            "role": "assistant",
            "streaming": False,
            "mode": ExecutionMode.SIMPLE_TOOL.value,
            "branch": "subagent",
            "agent_run_id": agent_run_id,
            "agent_id": agent_id,
            "agent_kind": agent_kind,
            "agent_display_name": agent_display_name,
            "graph_thread_id": graph_thread_id,
            "status": status,
            "id": turn_id,
        },
        conversation_id or "",
    )
    if turn_sequence is not None:
        metadata["turn_sequence"] = turn_sequence
    display_name = agent_display_name
    return LangGraphChatResult(
        final_text={
            "interrupted": f"{display_name} subagent infrastructure was interrupted.",
            "cancelled": f"{display_name} subagent run was cancelled.",
            "waiting_for_approval": f"{display_name} is waiting for tool approval.",
            "running": (
                f"{display_name} has started a subagent run and will hand off findings "
                "when it finishes."
            ),
        }.get(status, f"{display_name} subagent status changed."),
        conversation_id=conversation_id,
        metadata=metadata,
        usage=usage,
    )


def _ack_for_dispatch_stop(
    stop: AgentRunDispatchStop,
    *,
    runtime_config: LangGraphRuntimeConfig,
    turn_id: str,
    turn_sequence: int | None,
) -> LangGraphChatResult:
    """Present a transport-neutral dispatch stop as the existing chat result."""
    item = stop.invocation
    assignment = item.assignment
    return _ack_result(
        runtime_config,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        agent_run_id=assignment.agent_run_id,
        agent_id=assignment.agent_id,
        agent_kind=assignment.agent_kind,
        agent_display_name=item.display_name,
        graph_thread_id=item.graph_thread_id,
        status=stop.status,
        usage=list(stop.usage) or None,
    )


__all__ = ["SubagentHandler"]
