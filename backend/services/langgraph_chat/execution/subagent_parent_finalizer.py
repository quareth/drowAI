"""Execute the parent graph after completed subagent handoffs.

The finalizer owns parent graph state construction, execution, cancellation,
result metadata, and parent/child usage aggregation. It does not plan or launch
child runs, claim registry handoffs, or decide follow-up delegation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from agent.graph import InteractiveState
from agent.graph.builders.common_edges import ensure_metadata_runtime_budgets
from agent.graph.builders.parent_handoff_builder import build_parent_handoff_graph
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.runtime_state import refresh_bundle_from_working_memory
from agent.graph.graph_names import GRAPH_NAME_PARENT_HANDOFF
from agent.graph.streaming import build_agent_turn_metadata
from agent.graph.utils.event_identity import POST_ACTION_STREAM_SEQUENCE_METADATA_KEY
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    usage_envelopes_from_child_records,
)
from backend.services.agent_runs.parent_handoff_continuation import (
    ParentHandoffContinuationBroker,
)
from backend.services.agent_runs.result_projection import (
    ACTIVE_AGENT_RUNS_KEY,
    COMPLETED_AGENT_RESULTS_KEY,
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
from backend.services.langgraph_chat.execution.completion_callback import (
    StreamEmitter,
    run_turn_with_completion_callback,
)
from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.facade_helpers import build_result, build_thread_config
from backend.services.langgraph_chat.handlers.turn_runtime import (
    TurnIdentity,
    apply_agent_thread_config,
    build_cancelled_result,
    build_initial_interactive_state,
    build_or_reuse_state_container,
    drain_completion_callback,
    extract_usage_from_state,
    merge_execution_metadata,
    new_captured_state,
    parse_interactive_state_from_final,
    prefill_reasoning_tokens_from,
    record_execution_metadata,
)


CancellationCheckerFactory = Callable[[int, str], Callable[[], bool]]
_HANDOFF_CONTEXT_KEYS = (COMPLETED_AGENT_RESULTS_KEY, ACTIVE_AGENT_RUNS_KEY)


def _reuse_parent_continuation_state(
    previous: InteractiveState,
    current: InteractiveState,
) -> InteractiveState:
    """Preserve parent progress while replacing the latest handoff snapshot."""
    starting_state = InteractiveState.from_mapping(previous.as_graph_state())
    starting_metadata = starting_state.facts.ensure_metadata()
    current_metadata = current.facts.safe_metadata

    for key in _HANDOFF_CONTEXT_KEYS:
        if key in current_metadata:
            starting_metadata[key] = deepcopy(current_metadata[key])
        else:
            starting_metadata.pop(key, None)

    current_bundle = current_metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    starting_bundle = starting_metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
    if not isinstance(starting_bundle, dict):
        starting_bundle = (
            deepcopy(current_bundle) if isinstance(current_bundle, Mapping) else {}
        )
        starting_metadata[METADATA_CONTEXT_BUNDLE_KEY] = starting_bundle
    if isinstance(current_bundle, Mapping):
        for key in _HANDOFF_CONTEXT_KEYS:
            starting_bundle[key] = deepcopy(current_bundle.get(key, []))

    starting_metadata.pop("router_outcome", None)
    starting_state.facts.set_candidate_decision(None)
    starting_state.trace.final_text = None
    starting_state.trace.final_error = None
    refresh_bundle_from_working_memory(starting_metadata)
    return starting_state


class SubagentParentFinalizer:
    """Run the canonical parent finalizer over completed child results once."""

    def __init__(
        self,
        *,
        checkpointer_service: CheckpointerService,
        executor: LangGraphExecutor,
        cancellation_checker_factory: CancellationCheckerFactory,
        continuation_broker: ParentHandoffContinuationBroker,
    ) -> None:
        self._checkpointer = checkpointer_service
        self._executor = executor
        self._build_cancellation_checker = cancellation_checker_factory
        self._continuation_broker = continuation_broker

    async def finalize(
        self,
        runtime_config: LangGraphRuntimeConfig,
        *,
        turn: TurnIdentity,
        child_completions: tuple[AgentRunCompletion, ...],
        continuation_state: InteractiveState | None = None,
    ) -> LangGraphChatResult:
        """Execute the parent handoff graph and build its persisted chat result."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id
        initial_state, _injected_tokens = build_initial_interactive_state(
            runtime_config
        )
        current_state = InteractiveState.from_mapping(initial_state)
        starting_state = (
            current_state
            if continuation_state is None
            else _reuse_parent_continuation_state(
                continuation_state,
                current_state,
            )
        )
        starting_metadata = starting_state.facts.ensure_metadata()
        persisted_runtime_budgets = runtime_config.metadata.get("runtime_budgets")
        if isinstance(persisted_runtime_budgets, Mapping):
            starting_metadata["runtime_budgets"] = dict(persisted_runtime_budgets)
        ensure_metadata_runtime_budgets(starting_metadata)

        config = build_thread_config(runtime_config, task_id)
        thread_id = apply_agent_thread_config(
            config,
            task_id=task_id,
            graph_name=GRAPH_NAME_PARENT_HANDOFF,
            turn=turn,
            conversation_id=chat_inputs.conversation_id,
        )
        graph_input = starting_state.as_graph_state()
        captured_state = new_captured_state()
        reserved_message_id = turn.metadata.get("reserved_message_id")
        state_container = build_or_reuse_state_container(
            runtime_config,
            reserved_message_id=reserved_message_id,
        )
        result_holder: dict[str, Any] = {}
        cancellation_checker = self._build_cancellation_checker(
            task_id,
            str(turn.turn_id),
        )
        continuation_session = self._continuation_broker.open(
            task_id=task_id,
            thread_id=thread_id,
            state_container=state_container,
        )

        async def execute_graph(
            emitter: StreamEmitter,
            callback_result_holder: dict[str, Any],
        ) -> str:
            _ = (emitter, callback_result_holder)
            async with self._checkpointer.get_checkpointer(task_id) as checkpointer:
                execution_result = await self._executor.stream_graph(
                    build_parent_handoff_graph(checkpointer=checkpointer),
                    graph_input,
                    config,
                    task_id,
                    state_container=state_container,
                    should_cancel=cancellation_checker,
                )
            record_execution_metadata(captured_state, execution_result.metadata)
            if execution_result.interrupted:
                execution_result = await self._continuation_broker.wait(
                    continuation_session,
                    should_cancel=cancellation_checker,
                )
                record_execution_metadata(captured_state, execution_result.metadata)
            interactive_state = parse_interactive_state_from_final(
                final_state=execution_result.final_state,
                starting_state=starting_state,
                deterministic_mode=False,
                state_container=state_container,
                task_id=task_id,
                missing_state_message=(
                    f"Parent finalizer did not capture final state for task {task_id}"
                ),
            )
            captured_state["final_state"] = execution_result.final_state
            captured_state["interactive_state"] = interactive_state
            return interactive_state.trace.final_text or interactive_state.facts.message

        try:
            await drain_completion_callback(
                callback_runner=run_turn_with_completion_callback,
                turn=turn,
                task_id=task_id,
                conversation_id=chat_inputs.conversation_id or "",
                llm_func=execute_graph,
                should_cancel=cancellation_checker,
                state_container=state_container,
                reserved_message_id=reserved_message_id,
                result_holder=result_holder,
                prefill_reasoning_tokens=prefill_reasoning_tokens_from(turn.metadata),
            )
        finally:
            self._continuation_broker.close(continuation_session)

        if result_holder.get("cancelled") is True:
            return build_cancelled_result(
                chat_inputs=chat_inputs,
                thread_id=thread_id,
                graph_name=GRAPH_NAME_PARENT_HANDOFF,
                captured_state=captured_state,
            )

        interactive_state = captured_state["interactive_state"]
        if not isinstance(interactive_state, InteractiveState):
            raise RuntimeError(
                f"Parent finalizer did not capture interactive state for task {task_id}"
            )
        next_post_action_stream_sequence = (
            interactive_state.facts.safe_metadata.get(
                POST_ACTION_STREAM_SEQUENCE_METADATA_KEY
            )
        )
        if isinstance(next_post_action_stream_sequence, int):
            runtime_config.metadata[POST_ACTION_STREAM_SEQUENCE_METADATA_KEY] = (
                next_post_action_stream_sequence
            )
        final_runtime_budgets = interactive_state.facts.safe_metadata.get(
            "runtime_budgets"
        )
        if isinstance(final_runtime_budgets, Mapping):
            runtime_config.metadata["runtime_budgets"] = dict(final_runtime_budgets)
        final_text = (
            interactive_state.trace.final_text or interactive_state.facts.message
        )
        interactive_state.trace.final_text = final_text

        result_metadata = attach_conversation_ids(
            {
                "role": "assistant",
                "streaming": False,
                "mode": ExecutionMode.SIMPLE_TOOL.value,
                "branch": "subagent",
                "graph_name": GRAPH_NAME_PARENT_HANDOFF,
                "status": "completed",
                "handoff_agent_run_id": child_completions[0].result.agent_run_id,
                "handoff_agent_id": child_completions[0].result.agent_id,
                "handoff_agent_kind": child_completions[0].result.agent_kind,
                "handoff_graph_thread_id": child_completions[0].graph_thread_id,
                "handoff_agent_run_ids": [
                    completion.result.agent_run_id
                    for completion in child_completions
                ],
                "handoff_agent_ids": [
                    completion.result.agent_id for completion in child_completions
                ],
                "handoff_agent_kinds": [
                    completion.result.agent_kind for completion in child_completions
                ],
                "handoff_graph_thread_ids": [
                    completion.graph_thread_id for completion in child_completions
                ],
            },
            chat_inputs.conversation_id or "",
        )
        merge_execution_metadata(result_metadata, captured_state)
        router_outcome = interactive_state.facts.safe_metadata.get("router_outcome")
        if isinstance(router_outcome, Mapping):
            result_metadata.setdefault("router_outcome", dict(router_outcome))
        for key, value in build_agent_turn_metadata(interactive_state).items():
            if value is not None:
                result_metadata[key] = value

        turn_index = turn.turn_number if isinstance(turn.turn_number, int) else None
        parent_usage = extract_usage_from_state(
            interactive_state,
            execution_branch="subagent_parent_finalizer",
            turn_index=turn_index,
        )
        child_usage = [
            usage
            for completion in child_completions
            for usage in usage_envelopes_from_child_records(
                completion.usage_records,
                execution_branch="subagent_child",
                turn_index=turn_index,
            )
        ]
        usage = [*child_usage, *(parent_usage or [])] or None
        result = build_result(
            final_text=final_text,
            conversation_id=chat_inputs.conversation_id,
            interactive_state=interactive_state,
            metadata=result_metadata,
            events=[],
            turn_id=turn.turn_id,
            usage=usage,
        )
        result.persistence_handled = True
        return result


__all__ = ["CancellationCheckerFactory", "SubagentParentFinalizer"]
