"""Translate message and reasoning stream events into canonical chat payloads.

Responsibilities:
- build live answer-phase events such as ``message_start`` and ``message_delta``
- build reasoning-phase events and reasoning section boundaries
- translate ``stream_error`` events into the established streaming schema
- mutate ``ChatStateContainer`` for answer and reasoning accumulation
- emit the existing metrics for these event families

This module is concerned only with message/reasoning-style event construction.
It does not own shared metadata forwarding, dispatch, tool execution events, or
post-run final event creation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING
from uuid import uuid4

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    REASONING_PHASE_INDEX,
    STEP_MESSAGE_DELTA,
    STEP_MESSAGE_SECTION_END,
    STEP_MESSAGE_START,
    STEP_REASONING_DELTA,
    STEP_REASONING_SECTION_END,
    STEP_REASONING_START,
)
from agent.graph.streaming import build_delta_event
from backend.services.metrics.utils import safe_inc

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

logger = logging.getLogger("backend.services.langgraph_chat.streaming_adapter")


class MessageReasoningEventProcessor:
    """Own message, reasoning, and stream-error event construction."""

    def __init__(
        self,
        *,
        metric_inc: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._metric_inc = metric_inc or safe_inc
        self._local_phase_sequence_by_turn: Dict[tuple[str, str], int] = {}
        self._local_active_reasoning_by_turn: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._local_closed_reasoning_by_turn: Dict[tuple[str, str], Dict[str, Any]] = {}

    @staticmethod
    def _turn_key(conversation_id: str, turn_id: str) -> tuple[str, str]:
        """Return the in-memory lifecycle key for one chat turn."""
        return (conversation_id or "", turn_id or "")

    @staticmethod
    def _build_unique_reasoning_section_id(*, turn_id: str) -> str:
        """Build a lifecycle identity that is independent from ordering."""
        scope = turn_id or "turn"
        return f"{scope}:reasoning:{uuid4().hex}"

    def _claim_local_phase_sequence(self, *, conversation_id: str, turn_id: str) -> int:
        """Claim the next processor-local phase sequence for a turn."""
        key = self._turn_key(conversation_id, turn_id)
        phase_sequence = self._local_phase_sequence_by_turn.get(key, 0)
        self._local_phase_sequence_by_turn[key] = phase_sequence + 1
        return phase_sequence

    def _observe_phase_sequence(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        phase_sequence: Any,
    ) -> None:
        """Advance the local counter past an externally claimed phase."""
        if not isinstance(phase_sequence, int) or phase_sequence < 0:
            return
        key = self._turn_key(conversation_id, turn_id)
        current = self._local_phase_sequence_by_turn.get(key, 0)
        if phase_sequence >= current:
            self._local_phase_sequence_by_turn[key] = phase_sequence + 1

    def _start_reasoning_identity(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        section_name: str,
        sub_turn_index: Any,
        event_timestamp: Any,
        state_container: Optional["ChatStateContainer"],
    ) -> Dict[str, Any]:
        """Open a reasoning lifecycle and return mandatory section metadata."""
        phase_sequence = self._claim_local_phase_sequence(
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        reasoning_section_id = self._build_unique_reasoning_section_id(turn_id=turn_id)
        if state_container is not None:
            identity = state_container.start_reasoning(
                section_name=section_name,
                sub_turn_index=sub_turn_index,
                timestamp=event_timestamp,
                identity_scope=turn_id,
                phase_sequence=phase_sequence,
                reasoning_section_id=reasoning_section_id,
            )
        else:
            identity = {
                "phase_sequence": phase_sequence,
                "reasoning_section_id": reasoning_section_id,
                "section_name": section_name,
                "sub_turn_index": sub_turn_index,
            }
        self._observe_phase_sequence(
            conversation_id=conversation_id,
            turn_id=turn_id,
            phase_sequence=identity.get("phase_sequence"),
        )
        self._local_active_reasoning_by_turn[self._turn_key(conversation_id, turn_id)] = dict(identity)
        return dict(identity)

    def _require_active_reasoning_identity(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        state_container: Optional["ChatStateContainer"],
        allow_closed_snapshot: bool = False,
    ) -> Dict[str, Any]:
        """Return active section metadata or raise on stream contract violation."""
        identity: Optional[Dict[str, Any]] = None
        if state_container is not None:
            current = state_container.get_current_reasoning_identity()
            if current is not None:
                identity = dict(current)
        if identity is None:
            identity = self._local_active_reasoning_by_turn.get(
                self._turn_key(conversation_id, turn_id)
            )
        if identity is None and allow_closed_snapshot:
            identity = self._local_closed_reasoning_by_turn.get(
                self._turn_key(conversation_id, turn_id)
            )
        if identity is None:
            raise ValueError("reasoning event missing active reasoning_section_id")
        return dict(identity)

    def _end_reasoning_identity(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        sub_turn_index: Any,
        event_timestamp: Any,
        state_container: Optional["ChatStateContainer"],
    ) -> Dict[str, Any]:
        """Close the active reasoning lifecycle and return its section metadata."""
        identity = self._require_active_reasoning_identity(
            conversation_id=conversation_id,
            turn_id=turn_id,
            state_container=state_container,
        )
        if state_container is not None:
            closed = state_container.end_reasoning(
                sub_turn_index=sub_turn_index,
                timestamp=event_timestamp,
            )
            if closed is not None:
                identity = dict(closed)
        key = self._turn_key(conversation_id, turn_id)
        self._local_active_reasoning_by_turn.pop(key, None)
        self._local_closed_reasoning_by_turn[key] = dict(identity)
        return identity

    @staticmethod
    def _apply_reasoning_identity(
        processed: dict[str, Any],
        identity: Dict[str, Any],
        *,
        section_name: str,
    ) -> None:
        """Stamp mandatory reasoning-card identity metadata onto a live event."""
        metadata = processed.setdefault("metadata", {})
        metadata["phase_sequence"] = identity["phase_sequence"]
        metadata["reasoning_section_id"] = identity["reasoning_section_id"]
        metadata["section_name"] = section_name
        if identity.get("sub_turn_index") is not None:
            metadata["sub_turn_index"] = identity["sub_turn_index"]

    def process_message_start(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process message start event."""
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")

        processed = build_delta_event("", conversation_id, turn_id=turn_id)
        processed["type"] = "message_start"
        processed.setdefault("metadata", {})
        processed["metadata"]["subtype"] = "message_start"
        processed["metadata"]["source"] = "langgraph_stream"
        processed["metadata"]["timestamp"] = event.get("timestamp", time.time())
        processed["metadata"]["step_type"] = STEP_MESSAGE_START
        processed["metadata"]["ind"] = ind if ind is not None else ANSWER_PHASE_INDEX
        return processed

    def process_message_delta(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> Optional[dict[str, Any]]:
        """Process message delta event."""
        if "content" not in event:
            logger.warning("message_delta missing content field")
            return None

        content = event["content"]
        if state_container is not None:
            state_container.append_answer(content)

        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")

        processed = build_delta_event(content, conversation_id, turn_id=turn_id)
        processed.setdefault("metadata", {})
        processed["type"] = "message_delta"
        processed["metadata"]["subtype"] = "message_delta"
        processed["metadata"]["source"] = "langgraph_stream"
        processed["metadata"]["timestamp"] = event.get("timestamp", time.time())
        processed["metadata"]["step_type"] = STEP_MESSAGE_DELTA
        processed["metadata"]["ind"] = ind if ind is not None else ANSWER_PHASE_INDEX

        self._metric_inc("langgraph_stream_deltas_processed")
        return processed

    def process_section_end(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process section end event."""
        section_name = event.get("section_name", "final_answer")
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")

        return {
            "type": "section_end",
            "content": f"[Section complete: {section_name}]",
            "metadata": {
                "subtype": "section_end",
                "section_name": section_name,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "source": "langgraph_stream",
                "streaming": False,
                "timestamp": event.get("timestamp", time.time()),
                "step_type": STEP_MESSAGE_SECTION_END,
                "ind": ind if ind is not None else ANSWER_PHASE_INDEX,
            },
        }

    def process_stream_error(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process stream error event."""
        error = event.get("error", "Unknown error")
        recoverable = event.get("recoverable", True)
        details = event.get("details", {})
        internal_only = False
        if isinstance(details, Mapping):
            internal_only = bool(details.get("internal_only"))

        processed = {
            "type": "stream_error",
            "content": f"Error: {error}",
            "metadata": {
                "subtype": "stream_error",
                "error": error,
                "recoverable": recoverable,
                "details": details,
                "source": "langgraph_stream",
                "timestamp": event.get("timestamp", time.time()),
            },
        }
        if internal_only:
            processed["metadata"]["internal_only"] = True

        self._metric_inc("langgraph_stream_errors_processed")
        return processed

    def process_reasoning_start(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> dict[str, Any]:
        """Process reasoning start event.

        Opens a structured reasoning section on the state container when present,
        carrying step/section_name and sub_turn_index for later persistence.
        """
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        step = event.get("step", "thinking")
        ind = event.get("ind")
        section_name = event.get("section_name", step)
        sub_turn_index = event.get("sub_turn_index")
        event_timestamp = event.get("timestamp", time.time())

        identity = self._start_reasoning_identity(
            conversation_id=conversation_id,
            turn_id=turn_id,
            section_name=section_name,
            sub_turn_index=sub_turn_index,
            event_timestamp=event_timestamp,
            state_container=state_container,
        )

        processed = {
            "type": "reasoning_start",
            "content": "",
            "metadata": {
                "subtype": "reasoning_start",
                "step": step,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "streaming": True,
                "source": "langgraph_stream",
                "timestamp": event_timestamp,
            },
        }
        processed["metadata"]["step_type"] = STEP_REASONING_START
        processed["metadata"]["ind"] = ind if ind is not None else REASONING_PHASE_INDEX
        self._apply_reasoning_identity(processed, identity, section_name=section_name)

        self._metric_inc("langgraph_reasoning_starts_processed")
        return processed

    def process_reasoning_delta(
        self,
        event: dict[str, Any],
        *,
        streaming_flag: bool,
        state_container: Optional["ChatStateContainer"] = None,
    ) -> Optional[dict[str, Any]]:
        """Process reasoning delta event."""
        if "content" not in event:
            logger.warning("reasoning_delta missing content field")
            return None

        content = event["content"]
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")
        is_snapshot = bool(event.get("snapshot"))
        identity = self._require_active_reasoning_identity(
            conversation_id=conversation_id,
            turn_id=turn_id,
            state_container=state_container,
            allow_closed_snapshot=is_snapshot,
        )
        if state_container is not None and not is_snapshot:
            state_container.append_reasoning(content)
        section_name = str(identity.get("section_name") or "thinking")

        processed = {
            "type": "reasoning_delta",
            "content": content,
            "metadata": {
                "subtype": "reasoning_delta",
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "source": "langgraph_stream",
                "timestamp": time.time(),
                "step_type": STEP_REASONING_DELTA,
                "ind": ind if ind is not None else REASONING_PHASE_INDEX,
            },
        }
        processed["metadata"]["streaming"] = streaming_flag
        if is_snapshot:
            processed["metadata"]["snapshot"] = True
        self._apply_reasoning_identity(processed, identity, section_name=section_name)

        self._metric_inc("langgraph_reasoning_deltas_processed")
        return processed

    def process_reasoning_section_end(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> dict[str, Any]:
        """Process reasoning section end event.

        Finalizes the active structured reasoning section on the state container
        when present, so the accumulated text becomes a persisted section.
        """
        section_name = event.get("section_name", "thinking")
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")
        sub_turn_index = event.get("sub_turn_index")
        event_timestamp = event.get("timestamp", time.time())

        identity = self._end_reasoning_identity(
            conversation_id=conversation_id,
            turn_id=turn_id,
            sub_turn_index=sub_turn_index,
            event_timestamp=event_timestamp,
            state_container=state_container,
        )

        processed = {
            "type": "reasoning_section_end",
            "content": f"[Reasoning complete: {section_name}]",
            "metadata": {
                "subtype": "reasoning_section_end",
                "section_name": section_name,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "source": "langgraph_stream",
                "streaming": False,
                "timestamp": event_timestamp,
                "step_type": STEP_REASONING_SECTION_END,
                "ind": ind if ind is not None else REASONING_PHASE_INDEX,
            },
        }
        self._apply_reasoning_identity(processed, identity, section_name=section_name)

        self._metric_inc("langgraph_reasoning_ends_processed")
        return processed


__all__ = ["MessageReasoningEventProcessor"]
