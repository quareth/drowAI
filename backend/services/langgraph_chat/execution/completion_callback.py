"""Completion callback pattern for guaranteed ChatMessage persistence.

This module provides a wrapper function that guarantees ChatMessage updates
even on crashes, cancellations, or errors. It accumulates state during
streaming and updates the reserved ChatMessage row in a finally block.

Design Principles:
1. Streaming Continues - Events still stream to frontend in real-time
2. Guaranteed Persistence - ChatMessage update happens in finally block
3. All Exit Paths - Handles normal completion, cancellation, error, crash
4. No Delays - Streaming latency unchanged (< 100ms)

Pattern Flow:
1. Create stream queue + emitter for the turn
2. Execute LLM function (which emits events)
3. Stream events to frontend (yield)
4. On any exit (normal, cancel, error): update ChatMessage from state container

Usage:
    from backend.services.langgraph_chat.execution.completion_callback import (
        run_turn_with_completion_callback,
    )

    async def my_llm_function(emitter):
        # Generate events
        event = {"type": "reasoning_delta", "content": "Thinking..."}
        await emitter.emit(event)  # Stream to frontend

    async for event in run_turn_with_completion_callback(
        turn_id="task-123-turn-1",
        turn_number=1,
        task_id=123,
        conversation_id="conv-1",
        llm_func=my_llm_function,
        is_connected=lambda: True,
    ):
        # Events streamed to frontend
        yield event
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Dict, Optional, TYPE_CHECKING
import json

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

from backend.database import SessionLocal
from backend.services.chat.message_service import ChatMessageService
from backend.services.chat.observation_sections import parse_observation_sections
from backend.services.chat.turn_event_service import ChatTurnEventService


logger = logging.getLogger(__name__)
try:
    from backend.services.langgraph_chat.diagnostic_logger import (
        get_diagnostic_logger,
        log_timeout_event,
    )
    _diag_logger = get_diagnostic_logger()
except Exception:  # pragma: no cover - diagnostics unavailable
    _diag_logger = None
    log_timeout_event = None  # type: ignore[assignment]

def _diag_info(message: str, *args: object) -> None:
    if _diag_logger is not None:
        _diag_logger.info(message, *args)

def _diag_warning(message: str, *args: object) -> None:
    if _diag_logger is not None:
        _diag_logger.warning(message, *args)


class StreamEmitter:
    """Helper class for emitting events during LLM execution.

    Provides a simple interface for LLM functions to emit events
    that will be streamed to the frontend. A lightweight on_emit
    callback can be provided for counters/telemetry.

    Attributes:
        _queue: AsyncIO queue for passing events to the stream
        _on_emit: Optional callback invoked on each emitted event
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        on_emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """Initialize stream emitter.

        Args:
            queue: Queue to send events for streaming
            on_emit: Optional callback invoked per emitted event
        """
        self._queue = queue
        self._on_emit = on_emit

    async def emit(self, event: Dict[str, Any]) -> None:
        """Emit an event for streaming.

        Args:
            event: Event dict with type, content, metadata
        """
        if self._on_emit:
            self._on_emit(event)
        await self._queue.put(event)


_INTERRUPTED_ERROR = "interrupted"
_RUN_CANCELLED_ERROR = "run_cancelled"
_STOPPED_MESSAGE = "[Stopped]"


def build_container_payload(
    state_container: "ChatStateContainer",
    final_message: Optional[str],
    *,
    prefill_reasoning_tokens: Optional[str] = None,
) -> tuple[str, Optional[str], Optional[list], Optional[str]]:
    # Prefer final_message (complete text from graph state) over accumulated
    # deltas.  The container accumulates streaming chunks which may be partial
    # (e.g. "are" instead of the full answer) whereas final_message comes from
    # interactive_state.trace.final_text — the definitive complete response.
    container_answer = state_container.get_answer_tokens()
    if final_message:
        message_text = final_message
    elif container_answer:
        message_text = container_answer
    else:
        message_text = ""
    reasoning_tokens = _merge_reasoning_tokens(
        prefill_reasoning_tokens,
        state_container.get_reasoning_tokens() or None,
    )
    observation_sections = state_container.get_observation_tokens()
    observation_tokens = json.dumps(observation_sections) if observation_sections else None
    tool_calls = state_container.get_tool_calls() or None
    if tool_calls == []:
        tool_calls = None
    return message_text, reasoning_tokens, tool_calls, observation_tokens


def _merge_reasoning_tokens(
    prefill_reasoning_tokens: Optional[str],
    reasoning_tokens: Optional[str],
) -> Optional[str]:
    """Merge live-only pre-branch reasoning into persisted turn reasoning."""
    prefill = (
        prefill_reasoning_tokens.strip()
        if isinstance(prefill_reasoning_tokens, str)
        else ""
    )
    accumulated = reasoning_tokens.strip() if isinstance(reasoning_tokens, str) else ""

    if prefill and accumulated:
        if accumulated.startswith(prefill):
            return accumulated
        return f"{prefill}\n\n{accumulated}"
    if accumulated:
        return accumulated
    if prefill:
        return prefill
    return None


def _cancelled_final_message(
    state_container: Optional["ChatStateContainer"],
    final_message: Optional[str],
) -> Optional[str]:
    """Return explicit stopped fallback only when no partial answer exists."""
    if isinstance(final_message, str) and final_message.strip():
        return final_message
    if state_container is not None and state_container.get_answer_tokens().strip():
        return None
    return _STOPPED_MESSAGE


def persist_chat_message_from_container(
    *,
    task_id: int,
    turn_id: str,
    reserved_message_id: Optional[int],
    state_container: Optional["ChatStateContainer"],
    final_message: Optional[str],
    error: Optional[str],
    reason: str,
    conversation_id: str,
    turn_number: int,
    prefill_reasoning_tokens: Optional[str] = None,
    replace_turn_events: bool = False,
) -> None:
    if reserved_message_id is None:
        logger.warning(
            "[COMPLETION_CALLBACK] No reserved_message_id; cannot persist ChatMessage "
            "(task=%s, turn=%s, reason=%s)",
            task_id,
            turn_id,
            reason,
        )
        _diag_warning(
            "COMPLETION_CALLBACK | missing reserved_message_id | task=%s turn=%s reason=%s",
            task_id,
            turn_id,
            reason,
        )
        return
    if state_container is None:
        logger.warning(
            "[COMPLETION_CALLBACK] No state_container; cannot persist ChatMessage "
            "(task=%s, turn=%s, reason=%s, message_id=%s)",
            task_id,
            turn_id,
            reason,
            reserved_message_id,
        )
        _diag_warning(
            "COMPLETION_CALLBACK | missing state_container | task=%s turn=%s reason=%s message_id=%s",
            task_id,
            turn_id,
            reason,
            reserved_message_id,
        )
        return

    message_text, reasoning_tokens, tool_calls, observation_tokens = build_container_payload(
        state_container,
        final_message,
        prefill_reasoning_tokens=prefill_reasoning_tokens,
    )
    observation_sections = parse_observation_sections(
        observation_tokens,
        non_list_strategy="empty",
        dict_only=True,
    )
    # Collect canonical reasoning sections for dual-write.
    # The compatibility blob (reasoning_tokens) is already written via
    # ChatMessageService.update_message; canonical reasoning rows go
    # through ChatTurnEventService alongside tool/observation rows.
    reasoning_sections = state_container.get_reasoning_sections() if state_container else []

    tool_call_count = len(tool_calls) if tool_calls else 0
    reasoning_section_count = len(reasoning_sections) if reasoning_sections else 0
    logger.info(
        "[COMPLETION_CALLBACK] Persisting ChatMessage from container "
        "(task=%s, message_id=%s, answer_len=%s, reasoning_len=%s, "
        "reasoning_sections=%s, tool_calls=%s, reason=%s)",
        task_id,
        reserved_message_id,
        len(message_text or ""),
        len(reasoning_tokens or ""),
        reasoning_section_count,
        tool_call_count,
        reason,
    )
    _diag_info(
        "COMPLETION_CALLBACK | persist | task=%s message_id=%s answer_len=%s reasoning_len=%s "
        "reasoning_sections=%s tool_calls=%s reason=%s",
        task_id,
        reserved_message_id,
        len(message_text or ""),
        len(reasoning_tokens or ""),
        reasoning_section_count,
        tool_call_count,
        reason,
    )

    if message_text or reasoning_tokens or observation_tokens or tool_calls or error:
        db = SessionLocal()
        try:
            chat_svc = ChatMessageService(db)
            chat_svc.update_message(
                reserved_message_id,
                message_text,
                reasoning_tokens=reasoning_tokens,
                observation_tokens=observation_tokens,
                tool_calls=tool_calls,
                token_count=0,
                error=error,
            )
            turn_event_svc = ChatTurnEventService(db)
            # Checkpoint retry persistence replaces the canonical detail rows
            # for the message instead of merging. Merge would keep prior
            # failed-attempt rows and surface them in the terminal projection.
            if replace_turn_events:
                turn_event_svc.replace_events_for_message(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    chat_message_id=reserved_message_id,
                    turn_number=turn_number,
                    reasoning_sections=reasoning_sections or None,
                    tool_calls=tool_calls,
                    observation_sections=observation_sections,
                )
            else:
                turn_event_svc.merge_events_for_message(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    chat_message_id=reserved_message_id,
                    turn_number=turn_number,
                    reasoning_sections=reasoning_sections or None,
                    tool_calls=tool_calls,
                    observation_sections=observation_sections,
                )
            db.commit()
            logger.info(
                "[COMPLETION_CALLBACK] ChatMessage updated from container "
                "(task=%s, message_id=%s, reason=%s)",
                task_id,
                reserved_message_id,
                reason,
            )
            _diag_info(
                "COMPLETION_CALLBACK | updated | task=%s message_id=%s reason=%s",
                task_id,
                reserved_message_id,
                reason,
            )
        except Exception as chat_exc:
            db.rollback()
            logger.error(
                "[COMPLETION_CALLBACK] Failed to update ChatMessage from container "
                "(task=%s, message_id=%s): %s",
                task_id,
                reserved_message_id,
                chat_exc,
                exc_info=True,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass
    else:
        logger.warning(
            "[COMPLETION_CALLBACK] State container empty; skipping ChatMessage update "
            "(task=%s, message_id=%s, reason=%s)",
            task_id,
            reserved_message_id,
            reason,
        )
        _diag_warning(
            "COMPLETION_CALLBACK | empty container | task=%s message_id=%s reason=%s",
            task_id,
            reserved_message_id,
            reason,
        )


async def run_turn_with_completion_callback(
    turn_id: str,
    turn_number: int,
    task_id: int,
    conversation_id: str,
    llm_func: Callable,
    is_connected: Optional[Callable[[], bool]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    *,
    final_message: Optional[str] = None,
    state_container: Optional["ChatStateContainer"] = None,
    reserved_message_id: Optional[int] = None,
    result_holder: Optional[Dict[str, Any]] = None,
    prefill_reasoning_tokens: Optional[str] = None,
    replace_turn_events: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute LLM generation with guaranteed ChatMessage updates.

    This wrapper ensures the reserved ChatMessage row is updated
    even if the LLM function crashes, is cancelled, or raises an error.
    The update happens in a finally block, guaranteeing execution.

    Pattern:
    1. Create queue and emitter for event streaming
    2. Run LLM function in background task
    3. Stream events to frontend (yield)
    4. On any exit: update ChatMessage from state container (finally block)

    Exit Scenarios Handled:
    - Normal completion: LLM finishes, ChatMessage updated
    - HITL interrupt: Handler sets result_holder["interrupted"]; partial ChatMessage update is persisted
    - Cancellation: Client disconnects, partial events streamed
    - Error: LLM raises exception, events up to error streamed
    - Crash: Process killed, ChatMessage update skipped if not reached

    Args:
        turn_id: Unique identifier for this turn (e.g., "task-123-turn-1")
        turn_number: Sequential turn number within the task
        task_id: Task ID this turn belongs to
        conversation_id: Conversation context identifier
        llm_func: Async callable; signature: async def llm_func(emitter, result_holder) -> Optional[str]
                  Should return final message text. When HITL interrupt, set result_holder["interrupted"] = True
                  before returning.
        is_connected: Deprecated transport callback retained for backward compatibility.
        should_cancel: Explicit backend lifecycle cancel callback.
        final_message: Optional final message to append (if llm_func doesn't return one)
        result_holder: Optional dict for handler to set result_holder["interrupted"] on HITL interrupt.

    Yields:
        Event dicts as they are generated by the LLM function

    Raises:
        Exception: Re-raises any exception from llm_func after cleanup
    """
    if result_holder is None:
        result_holder = {}
    # Transport connectivity is intentionally decoupled from lifecycle authority.
    _ = is_connected

    # Create queue for streaming events
    event_queue: asyncio.Queue = asyncio.Queue()
    event_count = 0

    def _on_emit(_: Dict[str, Any]) -> None:
        nonlocal event_count
        event_count += 1

    # Create emitter for LLM function
    emitter = StreamEmitter(queue=event_queue, on_emit=_on_emit)

    # Track completion state
    llm_task: Optional[asyncio.Task] = None
    llm_final_message: Optional[str] = None
    llm_exception: Optional[Exception] = None
    completion_reason = "unknown"

    logger.info(
        "[COMPLETION_CALLBACK] Starting turn execution "
        "(task=%s, turn_id=%s, turn_number=%s)",
        task_id,
        turn_id,
        turn_number,
    )

    try:
        # Start LLM function in background
        async def run_llm():
            """Run LLM function and capture result/exception."""
            nonlocal llm_final_message, llm_exception
            try:
                # Support both llm_func(emitter, result_holder) and llm_func(emitter) for backward compatibility
                try:
                    result = await llm_func(emitter, result_holder)
                except TypeError:
                    result = await llm_func(emitter)
                # LLM can return final message or None
                if result and isinstance(result, str):
                    llm_final_message = result
            except asyncio.CancelledError:
                result_holder["cancelled"] = True
            except Exception as exc:
                llm_exception = exc
                logger.error(
                    "[COMPLETION_CALLBACK] LLM function raised exception "
                    "(task=%s, turn=%s): %s",
                    task_id,
                    turn_id,
                    exc,
                    exc_info=True,
                )
            finally:
                # Signal end of streaming
                await event_queue.put(None)

        llm_task = asyncio.create_task(run_llm())

        # Stream events as they are generated
        while True:
            if should_cancel and should_cancel():
                logger.info(
                    "[COMPLETION_CALLBACK] Explicit cancel requested "
                    "(task=%s, turn=%s)",
                    task_id,
                    turn_id,
                )
                completion_reason = "explicit_cancel"
                result_holder["cancelled"] = True
                if llm_task and not llm_task.done():
                    llm_task.cancel()
                break

            # Get next event from queue (with timeout to check connection)
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # No event yet, loop to check connection
                continue

            # None signals end of stream
            if event is None:
                if llm_exception:
                    completion_reason = "error"
                else:
                    completion_reason = "normal"
                break

            # Yield event to frontend
            yield event

        # Wait for LLM task to complete (if not already done)
        if llm_task and not llm_task.done():
            try:
                await asyncio.wait_for(llm_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[COMPLETION_CALLBACK] LLM task timeout during cleanup "
                    "(task=%s, turn=%s)",
                    task_id,
                    turn_id,
                )
                if log_timeout_event is not None:
                    log_timeout_event(
                        task_id,
                        "COMPLETION_CALLBACK",
                        "llm_cleanup_wait",
                        5.0,
                        "task_cancelled",
                        f"turn_id={turn_id}",
                    )
                llm_task.cancel()
            except asyncio.CancelledError:
                logger.debug(
                    "[COMPLETION_CALLBACK] LLM task cancelled "
                    "(task=%s, turn=%s)",
                    task_id,
                    turn_id,
                )

    finally:
        logger.info(
            "[COMPLETION_CALLBACK] Finalizing turn "
            "(task=%s, turn=%s, event_count=%s, reason=%s)",
            task_id,
            turn_id,
            event_count,
            completion_reason,
        )

        final_msg = llm_final_message or final_message
        hitl_interrupted = result_holder.get("interrupted") is True
        cancellation_or_error = completion_reason in ("cancellation", "explicit_cancel", "error")

        if hitl_interrupted:
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id=turn_id,
                reserved_message_id=reserved_message_id,
                state_container=state_container,
                final_message=final_msg,
                error=_INTERRUPTED_ERROR,
                reason="hitl_interrupt",
                conversation_id=conversation_id,
                turn_number=turn_number,
                prefill_reasoning_tokens=prefill_reasoning_tokens,
                replace_turn_events=replace_turn_events,
            )
        elif completion_reason == "explicit_cancel":
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id=turn_id,
                reserved_message_id=reserved_message_id,
                state_container=state_container,
                final_message=_cancelled_final_message(state_container, final_msg),
                error=_RUN_CANCELLED_ERROR,
                reason="explicit_cancel",
                conversation_id=conversation_id,
                turn_number=turn_number,
                prefill_reasoning_tokens=prefill_reasoning_tokens,
                replace_turn_events=replace_turn_events,
            )
        elif cancellation_or_error:
            logger.info(
                "[COMPLETION_CALLBACK] Skipping ChatMessage update due to cancellation/error "
                "(task=%s, turn=%s, reason=%s, reserved_message_id=%s)",
                task_id,
                turn_id,
                completion_reason,
                reserved_message_id,
            )
            _diag_info(
                "COMPLETION_CALLBACK | skip update | task=%s turn=%s reason=%s reserved_message_id=%s",
                task_id,
                turn_id,
                completion_reason,
                reserved_message_id,
            )
        else:
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id=turn_id,
                reserved_message_id=reserved_message_id,
                state_container=state_container,
                final_message=final_msg,
                error=None,
                reason=completion_reason,
                conversation_id=conversation_id,
                turn_number=turn_number,
                prefill_reasoning_tokens=prefill_reasoning_tokens,
                replace_turn_events=replace_turn_events,
            )

        # Re-raise LLM exception if one occurred
        if llm_exception:
            raise llm_exception


__all__ = [
    "StreamEmitter",
    "build_container_payload",
    "persist_chat_message_from_container",
    "run_turn_with_completion_callback",
]
