"""Deep reasoning handler that streams LangGraph events in real time.

Token usage is extracted from the final state and returned in the result.
Deep reasoning may involve multiple LLM calls per iteration, all of which are
accumulated in trace.usage_records.
Persistence is handled via ChatMessage updates in the completion callback."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.graph import InteractiveState
from agent.graph.builders.deep_reasoning_builder import compile_deep_reasoning_graph
from backend.config import E2E_DETERMINISTIC_MODE
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.execution.completion_callback import (
    run_turn_with_completion_callback,
    StreamEmitter,
)
from backend.services.metrics.utils import safe_inc

from ..contracts import ExecutionMode, LangGraphChatResult, LangGraphRuntimeConfig
from ..hitl_constants import GRAPH_NAME_DEEP_REASONING, GRAPH_NAME_INTERRUPT_RESUME
from ..facade_helpers import (
    build_result,
    build_thread_config,
)
from ..intent.persistence import persist_intent_context
from backend.services.langgraph_chat.execution.scenario_factory import get_scenario_graph
from .base_handler import BaseLangGraphHandler
from .normal_chat_handler import _extract_usage_from_state
from .turn_runtime import (
    apply_agent_thread_config,
    build_cancelled_result,
    build_initial_interactive_state,
    build_interrupted_result,
    build_or_reuse_state_container,
    drain_completion_callback,
    ensure_turn_identity,
    merge_execution_metadata,
    new_captured_state,
    parse_interactive_state_from_final,
    prefill_reasoning_tokens_from,
    record_execution_metadata,
)

logger = logging.getLogger(__name__)


def _deep_reasoning_observability_profile(metadata: Dict[str, Any]) -> str:
    """Return the rollout metric profile for a DR turn."""
    if metadata.get("plan_mode") is True or metadata.get("plan_review_required") is True:
        return "plan"
    return "unknown"


def _emit_deep_reasoning_observability_metric(
    metadata: Dict[str, Any],
    suffix: str,
    value: int = 1,
) -> None:
    """Emit profile-specific DR rollout counters without failing the turn."""
    profile = _deep_reasoning_observability_profile(metadata)
    if profile != "plan":
        return
    safe_inc(f"langgraph_dr_{profile}_{suffix}", value)


class DeepReasoningHandler(BaseLangGraphHandler):
    """Handles deep reasoning execution."""

    async def handle(
        self, runtime_config: LangGraphRuntimeConfig
    ) -> LangGraphChatResult:
        """Execute deep reasoning branch."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id

        logger.info(
            "[HANDLER] Using turn-based persistence for task %s",
            task_id,
        )
        _emit_deep_reasoning_observability_metric(runtime_config.metadata, "runs")
        try:
            return await self._handle_with_turn_based_persistence(runtime_config)
        except Exception:
            _emit_deep_reasoning_observability_metric(runtime_config.metadata, "failures")
            raise

    async def _handle_with_turn_based_persistence(
        self, runtime_config: LangGraphRuntimeConfig
    ) -> LangGraphChatResult:
        """Execute deep reasoning with new turn-based persistence pattern."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id

        turn = ensure_turn_identity(runtime_config, logger_=logger)
        turn_id = turn.turn_id
        turn_number = turn.turn_number
        meta = turn.metadata
        deterministic_mode = E2E_DETERMINISTIC_MODE or bool(meta.get("deterministic_mode"))

        initial_state, injected_tokens = build_initial_interactive_state(runtime_config)
        if injected_tokens is not None:
            logger.debug("[HANDLER] Injected intent classifier usage: %s tokens", injected_tokens)

        starting_state = InteractiveState.from_mapping(initial_state)

        config = build_thread_config(runtime_config, task_id)
        thread_id = apply_agent_thread_config(
            config,
            task_id=task_id,
            graph_name=GRAPH_NAME_DEEP_REASONING,
            turn=turn,
            conversation_id=chat_inputs.conversation_id,
        )
        graph_input = starting_state.as_graph_state()

        captured_state = new_captured_state(include_interrupted=True)
        reserved_message_id = meta.get("reserved_message_id")
        state_container = build_or_reuse_state_container(
            runtime_config,
            reserved_message_id=reserved_message_id,
        )
        result_holder: dict = {}
        cancellation_checker = self._build_cancellation_checker(task_id, turn_id)

        # Define the graph execution function for completion callback
        async def execute_graph(emitter: StreamEmitter, result_holder: dict) -> Optional[str]:
            """Execute graph and handle interrupts."""
            async with self._checkpointer.get_checkpointer(task_id) as checkpointer:
                # Build graph and compile with persistent checkpointer
                if deterministic_mode:
                    scenario_name = GRAPH_NAME_DEEP_REASONING
                    if "deterministic-interrupt" in chat_inputs.message.lower():
                        scenario_name = GRAPH_NAME_INTERRUPT_RESUME
                    compiled_graph = get_scenario_graph(scenario_name, checkpointer)
                    logger.info(
                        "[HANDLER] Using deterministic %s scenario for task %s",
                        scenario_name,
                        task_id,
                    )
                else:
                    compiled_graph = compile_deep_reasoning_graph(
                        checkpointer=checkpointer
                    )
                logger.info(
                    f"[HANDLER] Compiled deep reasoning graph with "
                    f"{type(checkpointer).__name__} for task {task_id}"
                )

                # Stream graph execution (state_container for ChatMessage)
                execution_result = await self._executor.stream_graph(
                    compiled_graph=compiled_graph,
                    graph_input=graph_input,
                    config=config,
                    task_id=task_id,
                    state_container=state_container,
                    should_cancel=cancellation_checker,
                )
                final_state = execution_result.final_state
                record_execution_metadata(captured_state, execution_result.metadata)

                # HITL: Check if graph was interrupted (tool approval pending)
                if execution_result.interrupted:
                    if not final_state:
                        raise RuntimeError(
                            f"Streaming did not capture interrupt state for task {task_id}"
                        )
                    logger.info(
                        f"[HANDLER] Deep Reasoning Graph interrupted for task {task_id}, awaiting user response"
                    )

                    result_holder["interrupted"] = True
                    captured_state["interrupted"] = True
                    captured_state["final_state"] = final_state

                    # No final message for interrupt
                    return None

                interactive_state = parse_interactive_state_from_final(
                    final_state=final_state,
                    starting_state=starting_state,
                    deterministic_mode=deterministic_mode,
                    state_container=state_container,
                    task_id=task_id,
                    missing_state_message=f"Streaming did not capture final state for task {task_id}",
                )

                # Capture state for later use
                captured_state["final_state"] = final_state
                captured_state["interactive_state"] = interactive_state

                # Return final message
                final_text = interactive_state.trace.final_text or interactive_state.facts.message
                return final_text

        # Execute with completion callback (Phase 3: container + reserved_message_id for ChatMessage)
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
            prefill_reasoning_tokens=prefill_reasoning_tokens_from(meta),
        )

        if result_holder.get("cancelled") is True:
            logger.info("[HANDLER] Returning cancelled result for task %s", task_id)
            return build_cancelled_result(
                chat_inputs=chat_inputs,
                thread_id=thread_id,
                graph_name=GRAPH_NAME_DEEP_REASONING,
                captured_state=captured_state,
            )

        # Handle interrupt case
        if captured_state["interrupted"]:
            logger.info(
                f"[HANDLER] Returning interrupt result for task {task_id} "
                f"(persistence handled by callback)"
            )

            interrupt_graph_name = (
                GRAPH_NAME_INTERRUPT_RESUME
                if deterministic_mode and "deterministic-interrupt" in chat_inputs.message.lower()
                else GRAPH_NAME_DEEP_REASONING
            )
            _emit_deep_reasoning_observability_metric(meta, "interruptions")
            return build_interrupted_result(
                chat_inputs=chat_inputs,
                thread_id=thread_id,
                graph_name=interrupt_graph_name,
                captured_state=captured_state,
            )

        # Extract results from captured state
        interactive_state = captured_state["interactive_state"]
        if not interactive_state:
            raise RuntimeError(f"Deep reasoning did not capture a final state for task {task_id}")

        final_text = interactive_state.trace.final_text or interactive_state.facts.message
        interactive_state.trace.final_text = final_text
        persist_intent_context(runtime_config, interactive_state)

        # No adapter buffer; build result events from final/pause only.
        events: List[Dict[str, Any]] = []
        pause_event = self._adapter.build_agent_pause_request_event(interactive_state)
        if pause_event:
            pause_event.setdefault("metadata", {})
            events.append(pause_event)

        result_metadata = attach_conversation_ids(
            {"role": "assistant", "streaming": False, "mode": ExecutionMode.DEEP_REASONING.value},
            chat_inputs.conversation_id or "",
        )
        merge_execution_metadata(result_metadata, captured_state)

        # Emit DR metrics
        try:
            _emit_deep_reasoning_observability_metric(meta, "completed")
            safe_inc("langgraph_dr_runs")
            safe_inc("langgraph_dr_iterations", interactive_state.facts.iterations)
            safe_inc("langgraph_dr_tool_calls", interactive_state.facts.tool_calls_used)
        except Exception:
            pass

        # Extract usage from state (Phase 3)
        usage = _extract_usage_from_state(
            interactive_state,
            execution_branch="deep_reasoning",
            turn_index=turn_number if isinstance(turn_number, int) else None,
        )
        if usage:
            logger.info(
                f"[HANDLER] Extracted {len(usage)} usage records for task {task_id}, "
                f"total_tokens={sum(entry.usage.total_tokens for entry in usage)}, "
                f"iterations={interactive_state.facts.iterations}"
            )

        # Build and return result
        # Persistence was already handled by completion callback
        result = build_result(
            final_text=final_text,
            conversation_id=chat_inputs.conversation_id,
            interactive_state=interactive_state,
            metadata=result_metadata,
            events=events,
            turn_id=turn_id,
            usage=usage,
        )
        result.persistence_handled = True
        return result

__all__ = ["DeepReasoningHandler"]
