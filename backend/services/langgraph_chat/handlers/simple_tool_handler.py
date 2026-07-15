"""Simple-tool handler that streams LangGraph events using the shared contract.

This module wires the simple-tool graph through the LangGraph streaming adapter
and `build_agent_turn_metadata` so reasoning/tool phases follow the common
contract. Tool selection/execution logic lives inside the graph nodes; the
handler orchestrates streaming and final metadata only.

Token usage is extracted from the final state and returned in the result.
Persistence is handled via ChatMessage updates in the completion callback."""

from __future__ import annotations

import logging
from typing import Optional

from agent.graph import InteractiveState
from agent.graph.builders.simple_tool_builder import GRAPH_NAME, build_simple_tool_graph
from agent.graph.streaming import build_agent_turn_metadata
from backend.config import E2E_DETERMINISTIC_MODE
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.execution.completion_callback import (
    run_turn_with_completion_callback,
    StreamEmitter,
)

from ..contracts import ExecutionMode, LangGraphChatResult, LangGraphRuntimeConfig
from ..hitl_constants import GRAPH_NAME_SIMPLE_TOOL
from ..diagnostic_logger import log_handler_flow
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


class SimpleToolHandler(BaseLangGraphHandler):
    """Handles simple tool execution."""

    async def handle(
        self, runtime_config: LangGraphRuntimeConfig
    ) -> LangGraphChatResult:
        """Execute the simple tool graph via streaming and emit summary metadata."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id

        logger.info(
            "[HANDLER] Using turn-based persistence for task %s",
            task_id,
        )
        return await self._handle_with_turn_based_persistence(runtime_config)

    async def _handle_with_turn_based_persistence(
        self, runtime_config: LangGraphRuntimeConfig
    ) -> LangGraphChatResult:
        """Execute simple tool with new turn-based persistence pattern."""
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
            graph_name=GRAPH_NAME_SIMPLE_TOOL,
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

        # Result holder for completion callback (HITL interrupt: handler sets result_holder["interrupted"])
        result_holder: dict = {}
        cancellation_checker = self._build_cancellation_checker(task_id, turn_id)

        # Define the graph execution function for completion callback
        async def execute_graph(emitter: StreamEmitter, result_holder: dict) -> Optional[str]:
            """Execute graph and handle interrupts."""
            async with self._checkpointer.get_checkpointer(task_id) as checkpointer:
                # Build and compile graph
                if deterministic_mode:
                    compiled_graph = get_scenario_graph(GRAPH_NAME_SIMPLE_TOOL, checkpointer)
                    logger.info(
                        "[HANDLER] Using deterministic scenario graph for task %s",
                        task_id,
                    )
                else:
                    compiled_graph = build_simple_tool_graph(checkpointer=checkpointer)
                logger.info(
                    f"[HANDLER] Compiled simple tool graph with "
                    f"{type(checkpointer).__name__} for task {task_id}"
                )

                # Execute with streaming
                agent_mode = runtime_config.metadata.get("agent_mode", "unknown")
                logger.info(
                    f"[HANDLER] Starting streaming execution for task {task_id}\n"
                    f"  Graph: {GRAPH_NAME}\n"
                    f"  Thread ID: {config['configurable']['thread_id']}\n"
                    f"  Agent Mode: {agent_mode}\n"
                    f"  Message: {chat_inputs.message[:100]}..."
                )

                logger.info(f"[HANDLER] Calling stream_graph for task {task_id}...")
                execution_result = await self._executor.stream_graph(
                    compiled_graph,
                    graph_input,
                    config,
                    task_id,
                    state_container=state_container,
                    should_cancel=cancellation_checker,
                )
                final_state = execution_result.final_state
                record_execution_metadata(captured_state, execution_result.metadata)
                logger.info(
                    f"[HANDLER] stream_graph returned for task {task_id}, "
                    f"state_keys={list(final_state.keys()) if isinstance(final_state, dict) else None}, "
                    f"interrupted={execution_result.interrupted}"
                )

                # HITL: Check if graph was interrupted (tool approval pending)
                if execution_result.interrupted:
                    if not final_state:
                        msg = f"Streaming did not capture interrupt state for task {task_id}"
                        log_handler_flow(
                            task_id,
                            "SimpleToolHandler",
                            "streaming_complete",
                            False,
                            "no_interrupt_state",
                        )
                        raise RuntimeError(msg)

                    logger.info(
                        f"[HANDLER] Graph interrupted for task {task_id}, awaiting user response"
                    )
                    log_handler_flow(
                        task_id, "SimpleToolHandler", "graph_interrupted", True, "tool_approval_pending"
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
                    on_missing_state=lambda: log_handler_flow(
                        task_id,
                        "SimpleToolHandler",
                        "streaming_complete",
                        False,
                        "no_final_state",
                    ),
                )
                logger.info(
                    f"[HANDLER] Streaming completed successfully for task {task_id}\n"
                    f"  Final text length: {len(interactive_state.trace.final_text or '')}\n"
                    f"  Reasoning steps: {len(interactive_state.trace.reasoning)}"
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
                graph_name=GRAPH_NAME_SIMPLE_TOOL,
                captured_state=captured_state,
            )

        # Handle interrupt case
        if captured_state["interrupted"]:
            logger.info(
                f"[HANDLER] Returning interrupt result for task {task_id} "
                f"(persistence handled by callback)"
            )
            return build_interrupted_result(
                chat_inputs=chat_inputs,
                thread_id=thread_id,
                graph_name=GRAPH_NAME_SIMPLE_TOOL,
                captured_state=captured_state,
            )

        # Extract results from captured state
        interactive_state = captured_state["interactive_state"]
        if not interactive_state:
            raise RuntimeError(f"Simple tool execution did not capture a final state for task {task_id}")

        final_text = interactive_state.trace.final_text or interactive_state.facts.message
        interactive_state.trace.final_text = final_text
        persist_intent_context(runtime_config, interactive_state)

        # No adapter buffer; events list is empty (persistence via completion callback).
        events = []

        result_metadata = attach_conversation_ids(
            {"role": "assistant", "streaming": False, "mode": ExecutionMode.SIMPLE_TOOL.value},
            chat_inputs.conversation_id or "",
        )
        merge_execution_metadata(result_metadata, captured_state)

        turn_metadata = build_agent_turn_metadata(interactive_state)
        for key, value in turn_metadata.items():
            if value is not None:
                result_metadata[key] = value
        # Extract usage from state (Phase 3)
        usage = _extract_usage_from_state(
            interactive_state,
            execution_branch="simple_tool",
            turn_index=turn_number if isinstance(turn_number, int) else None,
        )
        if usage:
            logger.info(
                f"[HANDLER] Extracted {len(usage)} usage records for task {task_id}, "
                f"total_tokens={sum(entry.usage.total_tokens for entry in usage)}"
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

__all__ = ["SimpleToolHandler"]
