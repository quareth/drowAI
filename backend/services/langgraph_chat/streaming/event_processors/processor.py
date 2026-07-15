"""Coordinate LangGraph stream event processing behind one adapter-facing entrypoint.

Responsibilities:
- accept raw LangGraph node events from ``LangGraphStreamingAdapter``
- dispatch each event type to the correct focused processor
- apply metadata forwarding that must stay consistent across all event families

This module intentionally does not build each event-family payload itself and
does not own database persistence. Payload construction lives in the family
processors, and tool snapshot persistence is delegated to ``snapshot_service``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, Optional, TYPE_CHECKING

from backend.services.metrics.utils import safe_inc

from backend.services.langgraph_chat.streaming.event_types import ensure_mutable_metadata
from backend.services.langgraph_chat.streaming.event_processors.execution_phase_processor import (
    ExecutionPhaseEventProcessor,
)
from backend.services.langgraph_chat.streaming.event_processors.message_reasoning_processor import (
    MessageReasoningEventProcessor,
)
from backend.services.langgraph_chat.streaming.event_processors.snapshot_service import (
    ToolCallSnapshotService,
)
from backend.services.langgraph_chat.streaming.event_processors.tool_event_processor import (
    ToolEventProcessor,
)

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

logger = logging.getLogger("backend.services.langgraph_chat.streaming_adapter")


class StreamEventProcessor:
    """Coordinate live LangGraph stream translation across focused collaborators."""

    def __init__(
        self,
        snapshot_service: ToolCallSnapshotService,
        *,
        metric_inc: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        metric_callback = metric_inc or safe_inc
        self._message_reasoning_processor = MessageReasoningEventProcessor(
            metric_inc=metric_callback,
        )
        self._tool_event_processor = ToolEventProcessor(
            snapshot_service,
            metric_inc=metric_callback,
        )
        self._execution_phase_processor = ExecutionPhaseEventProcessor(
            metric_inc=metric_callback,
        )

    def process_streaming_event(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> Optional[dict[str, Any]]:
        """Process a raw streaming event from LangGraph nodes."""
        event_type = event.get("type")
        if not event_type:
            logger.warning("Event missing type field, dropping: %s", event)
            return None

        processed: Optional[dict[str, Any]] = None
        if event_type == "message_start":
            processed = self._message_reasoning_processor.process_message_start(event)
        elif event_type == "message_delta":
            processed = self._message_reasoning_processor.process_message_delta(
                event,
                state_container,
            )
        elif event_type == "section_end":
            processed = self._message_reasoning_processor.process_section_end(event)
        elif event_type == "reasoning_start":
            processed = self._message_reasoning_processor.process_reasoning_start(
                event,
                state_container=state_container,
            )
        elif event_type == "reasoning_delta":
            processed = self._message_reasoning_processor.process_reasoning_delta(
                event,
                streaming_flag=self._resolve_streaming_flag(event, default=True),
                state_container=state_container,
            )
        elif event_type == "reasoning_section_end":
            processed = self._message_reasoning_processor.process_reasoning_section_end(
                event,
                state_container=state_container,
            )
        elif event_type == "tool_start":
            processed = self._tool_event_processor.process_tool_start(
                event,
                state_container,
            )
        elif event_type == "tool_batch_start":
            processed = self._tool_event_processor.process_tool_batch_start(event)
        elif event_type == "tool_delta":
            processed = self._tool_event_processor.process_tool_delta(event)
        elif event_type == "tool_end":
            processed = self._tool_event_processor.process_tool_end(
                event,
                state_container,
            )
        elif event_type == "tool_batch_end":
            processed = self._tool_event_processor.process_tool_batch_end(event)
        elif event_type == "observation_start":
            processed = self._execution_phase_processor.process_observation_start(
                event,
                state_container,
            )
        elif event_type == "observation_delta":
            processed = self._execution_phase_processor.process_observation_delta(
                event,
                streaming_flag=self._resolve_streaming_flag(event, default=True),
                state_container=state_container,
            )
        elif event_type == "observation_section_end":
            processed = self._execution_phase_processor.process_observation_section_end(
                event,
                state_container,
            )
        elif event_type == "retry_start":
            processed = self._execution_phase_processor.process_retry_start(event)
        elif event_type == "retry_attempt":
            processed = self._execution_phase_processor.process_retry_attempt(event)
        elif event_type == "plan_created":
            processed = self._execution_phase_processor.process_plan_created(event)
        elif event_type == "todo_progress":
            processed = self._execution_phase_processor.process_todo_progress(event)
        elif event_type == "stream_error":
            processed = self._message_reasoning_processor.process_stream_error(event)
        else:
            logger.debug("Unknown event type: %s", event_type)
            return None

        if processed:
            self._enrich_common_metadata(processed, event)
        return processed

    def _enrich_common_metadata(
        self,
        processed: dict[str, Any],
        raw_event: dict[str, Any],
    ) -> None:
        """Forward graph-agnostic metadata fields from raw event to processed output."""
        self._apply_sequence_metadata(processed, raw_event)
        self._apply_sub_turn_metadata(processed, raw_event)
        self._apply_task_context_metadata(processed, raw_event)

    def _apply_sequence_metadata(
        self,
        processed: dict[str, Any],
        raw_event: dict[str, Any],
    ) -> None:
        """Attach canonical turn sequence metadata if present on the raw event."""
        turn_sequence = raw_event.get("turn_sequence")
        if turn_sequence is None:
            turn_sequence = raw_event.get("sequence")
        if turn_sequence is None:
            return
        metadata = ensure_mutable_metadata(processed)
        metadata.setdefault("turn_sequence", turn_sequence)

    def _apply_sub_turn_metadata(
        self,
        processed: dict[str, Any],
        raw_event: dict[str, Any],
    ) -> None:
        """Attach sub-turn metadata when present on the raw event."""
        sub_turn_index = raw_event.get("sub_turn_index")
        if sub_turn_index is None:
            raw_metadata = raw_event.get("metadata")
            if isinstance(raw_metadata, Mapping):
                sub_turn_index = raw_metadata.get("sub_turn_index")
        if sub_turn_index is None:
            return
        metadata = ensure_mutable_metadata(processed)
        metadata.setdefault("sub_turn_index", sub_turn_index)

    def _apply_task_context_metadata(
        self,
        processed: dict[str, Any],
        raw_event: dict[str, Any],
    ) -> None:
        """Forward task context when present for replay/debug consumers."""
        task_id = raw_event.get("task_id")
        if task_id is None:
            raw_metadata = raw_event.get("metadata")
            if isinstance(raw_metadata, Mapping):
                task_id = raw_metadata.get("task_id")
        if task_id is None:
            return
        metadata = ensure_mutable_metadata(processed)
        metadata.setdefault("task_id", task_id)

    @staticmethod
    def _resolve_streaming_flag(raw_event: Mapping[str, Any], default: bool) -> bool:
        """Determine if an event should be treated as streaming or snapshot."""
        streaming_override = raw_event.get("streaming")
        if streaming_override is not None:
            return bool(streaming_override)
        return default


__all__ = ["StreamEventProcessor"]
