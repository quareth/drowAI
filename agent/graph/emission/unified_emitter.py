"""Unified event emission with guaranteed metadata.

All events emitted through UnifiedEventEmitter include complete metadata:
- ind (stream segment index)
- step_type (event type)
- conversation_id, turn_id
- turn_sequence (canonical per-turn ordering)
- sequence (optional per-event sequence)
- streaming flag

Segment indices: reasoning=0, tool=1, answer=2, observation=3.
Thread-safe: StreamWriter may be called concurrently."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)
from core.llm import iter_with_idle_timeout  # noqa: E402

from agent.graph.contracts.streaming_constants import (  # noqa: E402
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
    REASONING_PHASE_INDEX,
    STEP_MESSAGE_DELTA,
    STEP_MESSAGE_SECTION_END,
    STEP_MESSAGE_START,
    STEP_OBSERVATION_DELTA,
    STEP_OBSERVATION_SECTION_END,
    STEP_OBSERVATION_START,
    STEP_REASONING_DELTA,
    STEP_REASONING_SECTION_END,
    STEP_REASONING_START,
    STEP_RETRY_ATTEMPT,
    STEP_RETRY_START,
    STEP_TOOL_DELTA,
    STEP_TOOL_END,
    STEP_TOOL_START,
    TOOL_PHASE_INDEX,
)
from agent.graph.utils.dr_iteration_state import (  # noqa: E402
    _advance_dr_iteration,
    _dr_iteration_metadata,
)
from agent.graph.utils.event_identity import (  # noqa: E402
    resolve_canonical_identity as resolve_event_identity,
    resolve_sub_turn_index,
)


class StreamWriter(Protocol):
    """LangGraph StreamWriter protocol: callable that accepts an event dict."""

    def __call__(self, event: Dict[str, Any]) -> None: ...


@dataclass
class EventMetadata:
    """Complete metadata for all events. Ensures type safety and validation."""

    ind: int
    step_type: str
    conversation_id: str
    turn_id: str
    sequence: Optional[int]
    turn_sequence: Optional[int]
    streaming: bool
    sub_turn_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for event emission. Includes both snake and camel keys for compatibility."""
        result: Dict[str, Any] = {
            "ind": self.ind,
            "step_type": self.step_type,
            "conversation_id": self.conversation_id,
            "conversationId": self.conversation_id,
            "id": self.turn_id,
            "turn_id": self.turn_id,
            "streaming": self.streaming,
        }
        if self.sequence is not None:
            result["sequence"] = self.sequence
        if self.turn_sequence is not None:
            result["turn_sequence"] = self.turn_sequence
        if self.sub_turn_index is not None:
            result["sub_turn_index"] = self.sub_turn_index
        return result

    def validate(self) -> bool:
        """Validate required fields are present and phase index is in range."""
        if not isinstance(self.ind, int) or self.ind < 0 or self.ind > 3:
            return False
        if not self.step_type or not isinstance(self.step_type, str):
            return False
        if not isinstance(self.conversation_id, str):
            return False
        if not isinstance(self.turn_id, str):
            return False
        if self.sequence is not None and (not isinstance(self.sequence, int) or self.sequence < 0):
            return False
        if self.turn_sequence is not None and (
            not isinstance(self.turn_sequence, int) or self.turn_sequence < 0
        ):
            return False
        return True


class UnifiedEventEmitter:
    """Base class for unified event emission with complete metadata.

    All emit_* methods include ind, step_type, conversation_id, turn_id.
    Emission is thread-safe via a lock around the writer.

    Can be instantiated directly when identity is already resolved
    (via EventEmitterFactory.create_from_identity), or via subclasses
    SimpleEmitter/DeepReasoningEmitter that resolve identity from state/config.
    """

    def __init__(
        self,
        writer: StreamWriter,
        conversation_id: str,
        turn_id: str,
        turn_sequence: Optional[int] = None,
        sequence: Optional[int] = None,
        sub_turn_index: Optional[int] = None,
    ) -> None:
        self._writer = writer
        self._conversation_id = conversation_id
        self._turn_id = turn_id
        self._turn_sequence = turn_sequence
        self._sequence = sequence
        self._sub_turn_index = sub_turn_index
        self._lock = threading.Lock()

    def _build_base_metadata(
        self, ind: int, step_type: str, streaming: bool
    ) -> EventMetadata:
        """Build complete metadata for an event. ALWAYS includes ind."""
        return EventMetadata(
            ind=ind,
            step_type=step_type,
            conversation_id=self._conversation_id,
            turn_id=self._turn_id,
            sequence=self._sequence,
            turn_sequence=self._turn_sequence,
            streaming=streaming,
            sub_turn_index=self._sub_turn_index,
        )

    def _emit_event(self, event: Dict[str, Any]) -> None:
        """Emit event through writer. Thread-safe."""
        with self._lock:
            self._writer(event)

    # --- Reasoning phase (ind=0) ---

    def emit_reasoning_start(self, step: str = "thinking") -> None:
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, STEP_REASONING_START, streaming=True
        )
        event = {**meta.to_dict(), "type": "reasoning_start", "step": step}
        self._emit_event(event)

    def emit_reasoning_delta(self, content: str) -> None:
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, STEP_REASONING_DELTA, streaming=True
        )
        event = {**meta.to_dict(), "type": "reasoning_delta", "content": content}
        self._emit_event(event)

    def emit_reasoning_section_end(self, section_name: str = "thinking") -> None:
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, STEP_REASONING_SECTION_END, streaming=True
        )
        event = {**meta.to_dict(), "type": "reasoning_section_end", "section_name": section_name}
        self._emit_event(event)

    # --- Tool phase (ind=1) ---

    def emit_tool_start(
        self,
        tool: str,
        parameters: Optional[Dict[str, Any]] = None,
        *,
        tool_call_id: Optional[str] = None,
        tool_batch_id: Optional[str] = None,
    ) -> None:
        meta = self._build_base_metadata(
            TOOL_PHASE_INDEX, STEP_TOOL_START, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "tool_start",
            "tool": tool,
            "parameters": parameters or {},
        }
        if tool_call_id:
            event["tool_call_id"] = tool_call_id
        if tool_batch_id:
            event["tool_batch_id"] = tool_batch_id
        self._emit_event(event)

    def emit_tool_delta(
        self,
        tool: str,
        content: str,
        *,
        tool_call_id: Optional[str] = None,
        tool_batch_id: Optional[str] = None,
    ) -> None:
        meta = self._build_base_metadata(
            TOOL_PHASE_INDEX, STEP_TOOL_DELTA, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "tool_delta",
            "tool": tool,
            "content": content,
        }
        if tool_call_id:
            event["tool_call_id"] = tool_call_id
        if tool_batch_id:
            event["tool_batch_id"] = tool_batch_id
        self._emit_event(event)

    def emit_tool_end(
        self,
        tool: str,
        status: str = "success",
        duration: float = 0.0,
        summary: Optional[Dict[str, Any]] = None,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
        *,
        tool_call_id: Optional[str] = None,
        tool_batch_id: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta = self._build_base_metadata(
            TOOL_PHASE_INDEX, STEP_TOOL_END, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "tool_end",
            "tool": tool,
            "status": status,
            "duration": duration,
            "summary": summary or {},
            "exit_code": exit_code,
            "error": error,
        }
        if tool_call_id:
            event["tool_call_id"] = tool_call_id
        if tool_batch_id:
            event["tool_batch_id"] = tool_batch_id
        if extra_fields:
            event.update({k: v for k, v in extra_fields.items() if v is not None})
        self._emit_event(event)

    def emit_tool_batch_start(self, payload: Dict[str, Any]) -> None:
        """Emit a ``tool_batch_start`` lifecycle event (Phase 5 Task 5.2).

        ``payload`` is built by :func:`agent.tool_runtime.batch.emitter.build_tool_batch_start_payload`.
        """
        meta = self._build_base_metadata(
            TOOL_PHASE_INDEX, "tool_batch_start", streaming=True
        )
        event = {**meta.to_dict(), "type": "tool_batch_start", **dict(payload)}
        self._emit_event(event)

    def emit_tool_batch_end(self, payload: Dict[str, Any]) -> None:
        """Emit a ``tool_batch_end`` lifecycle event (Phase 5 Task 5.2)."""
        meta = self._build_base_metadata(
            TOOL_PHASE_INDEX, "tool_batch_end", streaming=True
        )
        event = {**meta.to_dict(), "type": "tool_batch_end", **dict(payload)}
        self._emit_event(event)

    # --- Answer phase (ind=2) ---

    def emit_message_start(
        self, extra_fields: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit message start with ind=2 (ANSWER_PHASE_INDEX)."""
        meta = self._build_base_metadata(
            ANSWER_PHASE_INDEX, STEP_MESSAGE_START, streaming=True
        )
        event = {**meta.to_dict(), "type": "message_start"}
        if extra_fields:
            event.update(
                {k: v for k, v in extra_fields.items() if v is not None}
            )
        self._emit_event(event)

    def emit_message_delta(
        self, content: str, extra_fields: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit message delta with ind=2 (ANSWER_PHASE_INDEX)."""
        meta = self._build_base_metadata(
            ANSWER_PHASE_INDEX, STEP_MESSAGE_DELTA, streaming=True
        )
        event = {**meta.to_dict(), "type": "message_delta", "content": content}
        if extra_fields:
            event.update(
                {k: v for k, v in extra_fields.items() if v is not None}
            )
        self._emit_event(event)

    def emit_section_end(
        self, section_name: str = "final_answer", ind: int = ANSWER_PHASE_INDEX
    ) -> None:
        meta = self._build_base_metadata(
            ind, STEP_MESSAGE_SECTION_END, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "section_end",
            "section_name": section_name,
        }
        self._emit_event(event)

    def emit_stream_error(
        self,
        error: str,
        recoverable: bool = True,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit streaming error event with full metadata."""
        meta = self._build_base_metadata(
            ANSWER_PHASE_INDEX, "stream_error", streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "stream_error",
            "error": error,
            "recoverable": recoverable,
            "details": details or {},
        }
        self._emit_event(event)

    # --- Observation phase (ind=3) ---

    def emit_observation_start(self, step: str = "observing") -> None:
        meta = self._build_base_metadata(
            OBSERVATION_PHASE_INDEX, STEP_OBSERVATION_START, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "observation_start",
            "step": step,
        }
        self._emit_event(event)

    def emit_observation_delta(self, content: str) -> None:
        """Emit observation delta with ind=3 (OBSERVATION_PHASE_INDEX)."""
        meta = self._build_base_metadata(
            OBSERVATION_PHASE_INDEX, STEP_OBSERVATION_DELTA, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "observation_delta",
            "content": content,
        }
        self._emit_event(event)

    def emit_observation_section_end(self, section_name: str = "observing") -> None:
        meta = self._build_base_metadata(
            OBSERVATION_PHASE_INDEX, STEP_OBSERVATION_SECTION_END, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "observation_section_end",
            "section_name": section_name,
        }
        self._emit_event(event)

    # --- Retry events ---

    def emit_retry_start(
        self,
        attempt: int,
        max_attempts: int,
        failure_category: Optional[str] = None,
    ) -> None:
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, STEP_RETRY_START, streaming=True
        )
        event = {
            **meta.to_dict(),
            "type": "retry_start",
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        if failure_category is not None:
            event["failure_category"] = failure_category
        self._emit_event(event)

    def emit_retry_attempt(
        self,
        attempt: int,
        alternative_tool: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> None:
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, STEP_RETRY_ATTEMPT, streaming=True
        )
        event = {**meta.to_dict(), "type": "retry_attempt", "attempt": attempt}
        if alternative_tool is not None:
            event["alternative_tool"] = alternative_tool
        if reasoning is not None:
            event["reasoning"] = reasoning
        self._emit_event(event)

    # --- Snapshot events (non-streaming, complete content) ---

    def emit_reasoning_snapshot(
        self, content: str, step: str = "thinking"
    ) -> None:
        """Emit non-streaming reasoning snapshot with the final accumulated text."""
        if not content:
            return
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, STEP_REASONING_DELTA, streaming=False
        )
        event = {
            **meta.to_dict(),
            "type": "reasoning_delta",
            "content": content,
            "step": step,
            "snapshot": True,
        }
        self._emit_event(event)

    def emit_observation_snapshot(
        self, content: str, step: str = "observing"
    ) -> None:
        """Emit non-streaming observation snapshot with the final articulated text."""
        if not content:
            return
        meta = self._build_base_metadata(
            OBSERVATION_PHASE_INDEX, STEP_OBSERVATION_DELTA, streaming=False
        )
        event = {
            **meta.to_dict(),
            "type": "observation_delta",
            "content": content,
            "step": step,
            "snapshot": True,
        }
        self._emit_event(event)

    # --- Plan and progress events ---

    def emit_plan_created(
        self,
        goal: str,
        plan_steps: List[str],
        todo_list: List[Dict[str, Any]],
        *,
        run_id: Optional[int] = None,
        plan_version: Optional[int] = None,
    ) -> None:
        """Emit plan creation event for frontend Plan Card."""
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, "plan_created", streaming=False
        )
        event = {
            **meta.to_dict(),
            "type": "plan_created",
            "goal": goal,
            "plan_steps": plan_steps,
            "todo_list": todo_list,
        }
        if run_id is not None:
            event["run_id"] = run_id
        if plan_version is not None:
            event["plan_version"] = plan_version
        self._emit_event(event)

    def emit_todo_progress(
        self,
        todo_updates: List[Dict[str, Any]],
        *,
        run_id: Optional[int] = None,
        plan_version: Optional[int] = None,
    ) -> None:
        """Emit todo progress update for frontend tracking."""
        meta = self._build_base_metadata(
            REASONING_PHASE_INDEX, "todo_progress", streaming=False
        )
        event = {
            **meta.to_dict(),
            "type": "todo_progress",
            "todo_updates": todo_updates,
        }
        if run_id is not None:
            event["run_id"] = run_id
        if plan_version is not None:
            event["plan_version"] = plan_version
        self._emit_event(event)

    # --- High-level streaming method ---

    async def stream_reasoning(
        self,
        llm_client: Any,
        messages: List[Dict[str, Any]],
        step: str = "thinking",
        *,
        section_name: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 800,
        reasoning_effort: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        task_id: Optional[Any] = None,
    ) -> str:
        """Stream LLM reasoning output with full identity on all events.

        Emits reasoning_start, reasoning_delta (per token), reasoning_section_end,
        and reasoning_snapshot. Returns the accumulated text.
        """
        self.emit_reasoning_start(step)
        chunks: List[str] = []
        stream_failure: Optional[BaseException] = None
        try:
            stream_iter = llm_client.stream_chat_messages(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
            if timeout_sec is not None:
                stream_iter = iter_with_idle_timeout(
                    stream_iter,
                    timeout_sec=timeout_sec,
                    component="REASONING_MAIN",
                    operation="stream_reasoning",
                    logger=logger,
                    task_id=task_id,
                    outcome="stream_idle_timeout",
                    details=f"step={step}",
                )
            async for chunk in stream_iter:
                if isinstance(chunk, str) and chunk:
                    self.emit_reasoning_delta(chunk)
                    chunks.append(chunk)
        except BaseException as exc:
            stream_failure = exc
            raise
        finally:
            try:
                self.emit_reasoning_section_end(section_name or step)
            except Exception:
                if stream_failure is None:
                    raise
                logger.exception(
                    "Failed to emit reasoning_section_end after stream failure",
                )
        final_text = "".join(chunks).strip()
        if final_text:
            self.emit_reasoning_snapshot(final_text, step=step)
        return final_text

    @staticmethod
    def resolve_canonical_identity(
        state: Any,
        config: Optional[Any],
        context: Optional[Any],
    ) -> tuple:
        """Resolve (conversation_id, turn_id, turn_sequence) from config or state.

        Priority: config["configurable"] canonical values > state-derived fallback.
        This ensures handler-established identity is used regardless of graph type.
        """
        return resolve_event_identity(
            state=state,
            config=config,
            context=context,
        )


class SimpleEmitter(UnifiedEventEmitter):
    """Emitter for simple_tool_execution and respond_only."""

    def __init__(
        self,
        writer: StreamWriter,
        state: Any,
        config: Optional[Any] = None,
        context: Optional[Any] = None,
    ) -> None:
        conversation_id, turn_id, turn_sequence = UnifiedEventEmitter.resolve_canonical_identity(
            state, config, context
        )
        super().__init__(writer, conversation_id, turn_id, turn_sequence=turn_sequence)


class DeepReasoningEmitter(UnifiedEventEmitter):
    """Emitter for deep_reasoning — uses SAME canonical identity as SimpleEmitter.

    DR iteration tracking is internal and does NOT affect turn_id.
    sub_turn_index follows canonical metadata identity resolution so repeated
    intra-turn sections stay segregated across graph variants.
    """

    def __init__(
        self,
        writer: StreamWriter,
        state: Any,
        config: Optional[Any] = None,
        context: Optional[Any] = None,
    ) -> None:
        conversation_id, turn_id, turn_sequence = UnifiedEventEmitter.resolve_canonical_identity(
            state, config, context
        )
        metadata = (getattr(getattr(state, "facts", None), "metadata", None) or {})
        sub_turn_index = resolve_sub_turn_index(metadata)
        super().__init__(
            writer, conversation_id, turn_id,
            turn_sequence=turn_sequence,
            sub_turn_index=sub_turn_index,
        )
        # Internal iteration tracking (not exposed in event identity)
        self._state = state
        self._config = config

    def advance_iteration(self) -> int:
        """Advance DR iteration counter in state metadata. Does NOT change turn_id."""
        dr_meta = _dr_iteration_metadata(self._state)
        iteration = _advance_dr_iteration(dr_meta)
        return iteration
