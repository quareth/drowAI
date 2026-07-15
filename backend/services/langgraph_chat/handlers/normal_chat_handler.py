"""Handler for normal chat (simple LLM response) execution branch.

Responsibilities:
- Build initial state for simple chat graph
- Compile and execute the simple chat graph with streaming
- Build simple chat events for SSE/WebSocket delivery
- Persist intent context
- Extract and return token usage from LLM calls
- Update ChatMessage via completion callback at turn completion

Out of scope:
- Checkpointer selection (delegated to checkpointer service)
- Event forwarding (delegated to executor)"""

from __future__ import annotations

import logging

from agent.graph import InteractiveState, build_simple_chat_graph
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.execution.completion_callback import (
    run_turn_with_completion_callback,
    StreamEmitter,
)

from ..contracts import LangGraphChatResult, LangGraphRuntimeConfig
from ..diagnostic_logger import log_handler_flow
from ..facade_helpers import (
    build_result,
    build_thread_config,
)
from ..hitl_constants import GRAPH_NAME_NORMAL_CHAT
from ..intent.persistence import persist_intent_context
from .base_handler import BaseLangGraphHandler
from .turn_runtime import (
    build_cancelled_result,
    build_initial_interactive_state,
    build_or_reuse_state_container,
    drain_completion_callback,
    ensure_turn_identity,
    extract_usage_from_state as _extract_usage_from_state,
    merge_execution_metadata,
    new_captured_state,
    prefill_reasoning_tokens_from,
    record_execution_metadata,
)

logger = logging.getLogger(__name__)


class NormalChatHandler(BaseLangGraphHandler):
    """Handles normal chat (simple LLM response) execution."""

    async def handle(
        self, runtime_config: LangGraphRuntimeConfig
    ) -> LangGraphChatResult:
        """Execute the normal chat branch via streaming."""
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
        """Execute normal chat with new turn-based persistence pattern."""
        chat_inputs = runtime_config.chat_inputs
        task_id = chat_inputs.task_id

        turn = ensure_turn_identity(runtime_config, logger_=logger)
        turn_id = turn.turn_id
        turn_number = turn.turn_number
        meta = turn.metadata

        initial_state, injected_tokens = build_initial_interactive_state(runtime_config)
        metadata = initial_state["facts"]["metadata"]
        # Phase 5+ cutover: simple chat reads its prior-turn transcript
        # from the shared ``ConversationContextBundle`` (single
        # transcript-window authority). The handler no longer copies
        # ``chat_inputs.history`` onto ``simple_chat_runtime`` — doing so
        # was a parallel transcript path that bypassed the 10/5 window
        # policy applied by ``select_recent_transcript_window``.
        metadata.setdefault("simple_chat_runtime", {})
        metadata["simple_chat_runtime"].update(
            {
                "provider": chat_inputs.provider,
                "model": chat_inputs.model,
                "credential_ref": chat_inputs.credential_ref,
            }
        )

        if injected_tokens is not None:
            logger.debug("[HANDLER] Injected intent classifier usage: %s tokens", injected_tokens)

        config = build_thread_config(runtime_config, task_id)
        thread_id = config.get("configurable", {}).get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise RuntimeError(f"Missing LangGraph thread_id for task {task_id}")

        captured_state = new_captured_state()
        reserved_message_id = meta.get("reserved_message_id")
        state_container = build_or_reuse_state_container(
            runtime_config,
            reserved_message_id=reserved_message_id,
        )
        result_holder: dict = {}
        cancellation_checker = self._build_cancellation_checker(task_id, turn_id)

        # Define the graph execution function for completion callback
        async def execute_graph(emitter: StreamEmitter, result_holder: dict) -> str:
            """Execute graph and stream events through emitter."""
            async with self._checkpointer.get_checkpointer(task_id) as checkpointer:
                # Compile graph with persistent checkpointer
                compiled = build_simple_chat_graph(checkpointer=checkpointer)
                logger.info(
                    f"[HANDLER] Compiled simple chat graph with "
                    f"{type(checkpointer).__name__} for task {task_id}"
                )

                # Execute with streaming (adapter accumulates state in state_container)
                log_handler_flow(task_id, "NormalChatHandler", "streaming_start")
                execution_result = await self._executor.stream_graph(
                    compiled,
                    initial_state,
                    config,
                    task_id,
                    state_container=state_container,
                    should_cancel=cancellation_checker,
                )
                final_state = execution_result.final_state
                record_execution_metadata(captured_state, execution_result.metadata)

                if not final_state:
                    msg = f"Streaming did not capture final state for task {task_id}"
                    log_handler_flow(task_id, "NormalChatHandler", "streaming_complete", False, "no_final_state")
                    raise RuntimeError(msg)

                interactive_state = InteractiveState.from_mapping(final_state)
                logger.info(
                    f"[HANDLER] Streaming completed successfully for task {task_id}\n"
                    f"  Final text length: {len(interactive_state.trace.final_text or '')}\n"
                    f"  Reasoning steps: {len(interactive_state.trace.reasoning)}"
                )

                # Capture state for later use
                captured_state["final_state"] = final_state
                captured_state["interactive_state"] = interactive_state

                # Return final message for callback to persist
                return interactive_state.trace.final_text or ""

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
            prefill_reasoning_tokens=prefill_reasoning_tokens_from(metadata),
        )

        if result_holder.get("cancelled") is True:
            logger.info("[HANDLER] Returning cancelled result for task %s", task_id)
            return build_cancelled_result(
                chat_inputs=chat_inputs,
                thread_id=thread_id,
                graph_name=GRAPH_NAME_NORMAL_CHAT,
                captured_state=captured_state,
            )

        # Extract results from captured state
        interactive_state = captured_state["interactive_state"]
        if not interactive_state:
            raise RuntimeError(f"Graph execution did not capture state for task {task_id}")

        final_content = interactive_state.trace.final_text or ""
        resolved_conversation_id = interactive_state.facts.conversation_id

        persist_intent_context(runtime_config, interactive_state)

        # Build events for return value (for compatibility with existing code)
        # Persistence is handled by completion callback (ChatMessage update); no adapter buffer.
        final_event = self._adapter.build_final_event(interactive_state, turn_id=turn_id)
        events = [final_event] if final_event else []

        result_metadata = attach_conversation_ids(
            {"role": "assistant", "streaming": False},
            resolved_conversation_id or "",
        )
        result_metadata.setdefault("role", "assistant")
        merge_execution_metadata(result_metadata, captured_state)

        # Extract usage from state (Phase 3)
        usage = _extract_usage_from_state(
            interactive_state,
            execution_branch="simple_chat",
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
            final_text=final_content,
            conversation_id=resolved_conversation_id,
            interactive_state=interactive_state,
            metadata=result_metadata,
            events=events,
            turn_id=turn_id,
            usage=usage,
        )
        result.persistence_handled = True
        return result

__all__ = ["NormalChatHandler", "_extract_usage_from_state"]
