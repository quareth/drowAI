"""Translate observation and execution-progress events for live LangGraph output.

Responsibilities:
- build observation lifecycle events and update observation state in
  ``ChatStateContainer``
- build retry/reflection progress events used during tool recovery flows
- normalize plan and todo progress payloads into the streaming contract
- emit the existing metrics for retry, plan, and todo event families

This module owns the execution-progress families that are neither answer/
reasoning output nor tool execution output. Shared dispatch and metadata
forwarding remain in the main stream event coordinator.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, TYPE_CHECKING

from agent.graph.contracts.streaming_constants import (
    OBSERVATION_PHASE_INDEX,
    REASONING_PHASE_INDEX,
    STEP_OBSERVATION_DELTA,
    STEP_OBSERVATION_SECTION_END,
    STEP_OBSERVATION_START,
    STEP_RETRY_ATTEMPT,
    STEP_RETRY_START,
)
from backend.services.metrics.utils import safe_inc

from backend.services.langgraph_chat.streaming.event_types import ensure_mutable_metadata

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

logger = logging.getLogger("backend.services.langgraph_chat.streaming_adapter")


class ExecutionPhaseEventProcessor:
    """Own observation, retry, and plan/todo event construction."""

    def __init__(
        self,
        *,
        metric_inc: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._metric_inc = metric_inc or safe_inc

    def process_observation_start(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> dict[str, Any]:
        """Process observation start event."""
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        step = event.get("step", "observing")
        ind = event.get("ind")
        sub_turn_index = event.get("sub_turn_index")

        processed = {
            "type": "observation_start",
            "content": "",
            "metadata": {
                "subtype": "observation_start",
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "streaming": True,
                "step": step,
                "source": "langgraph_stream",
                "timestamp": time.time(),
                "step_type": STEP_OBSERVATION_START,
                "ind": ind if ind is not None else OBSERVATION_PHASE_INDEX,
            },
        }
        if state_container is not None:
            state_container.start_observation(sub_turn_index=sub_turn_index)
        return processed

    def process_observation_delta(
        self,
        event: dict[str, Any],
        *,
        streaming_flag: bool,
        state_container: Optional["ChatStateContainer"] = None,
    ) -> Optional[dict[str, Any]]:
        """Process observation delta event."""
        if "content" not in event:
            logger.warning("observation_delta missing content")
            return None

        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")
        sub_turn_index = event.get("sub_turn_index")

        processed = {
            "type": "observation_delta",
            "content": event["content"],
            "metadata": {
                "subtype": "observation_delta",
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "source": "langgraph_stream",
                "timestamp": time.time(),
                "step_type": STEP_OBSERVATION_DELTA,
                "ind": ind if ind is not None else OBSERVATION_PHASE_INDEX,
            },
        }
        processed["metadata"]["streaming"] = streaming_flag
        snapshot_flag = bool(event.get("snapshot"))
        if snapshot_flag:
            processed["metadata"]["snapshot"] = True
        if state_container is not None:
            state_container.append_observation(
                event["content"],
                snapshot=snapshot_flag,
                sub_turn_index=sub_turn_index,
            )
        return processed

    def process_observation_section_end(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> dict[str, Any]:
        """Process observation section end event."""
        section_name = event.get("section_name", "observing")
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        ind = event.get("ind")
        sub_turn_index = event.get("sub_turn_index")

        processed = {
            "type": "observation_section_end",
            "content": f"[Observation complete: {section_name}]",
            "metadata": {
                "subtype": "observation_section_end",
                "section_name": section_name,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "source": "langgraph_stream",
                "streaming": False,
                "timestamp": time.time(),
                "step_type": STEP_OBSERVATION_SECTION_END,
                "ind": ind if ind is not None else OBSERVATION_PHASE_INDEX,
            },
        }
        if state_container is not None:
            state_container.end_observation(sub_turn_index=sub_turn_index)
        return processed

    def process_retry_start(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process retry start event."""
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        attempt = event.get("attempt", 1)
        max_attempts = event.get("max_attempts", 3)
        failure_category = event.get("failure_category")
        ind = event.get("ind")

        processed = {
            "type": "retry_start",
            "content": f"Retry attempt {attempt}/{max_attempts}",
            "metadata": {
                "subtype": "retry_start",
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "retry_attempt": attempt,
                "retry_max_attempts": max_attempts,
                "streaming": True,
                "source": "langgraph_stream",
                "timestamp": time.time(),
                "step_type": STEP_RETRY_START,
                "ind": ind if ind is not None else REASONING_PHASE_INDEX,
            },
        }

        if failure_category:
            processed["metadata"]["failure_category"] = failure_category
            processed["metadata"]["retry_failure_category"] = failure_category

        self._metric_inc("langgraph_retry_starts_processed")
        return processed

    def process_retry_attempt(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process retry attempt event."""
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        attempt = event.get("attempt", 1)
        alternative_tool = event.get("alternative_tool")
        reasoning = event.get("reasoning")
        ind = event.get("ind")

        processed = {
            "type": "retry_attempt",
            "content": f"Retrying with alternative approach (attempt {attempt})",
            "metadata": {
                "subtype": "retry_attempt",
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "attempt": attempt,
                "retry_attempt": attempt,
                "streaming": True,
                "source": "langgraph_stream",
                "timestamp": time.time(),
                "step_type": STEP_RETRY_ATTEMPT,
                "ind": ind if ind is not None else REASONING_PHASE_INDEX,
            },
        }

        if alternative_tool:
            processed["metadata"]["alternative_tool"] = alternative_tool
            processed["metadata"]["retry_alternative_tool"] = alternative_tool

        if reasoning:
            processed["metadata"]["reasoning"] = reasoning

        self._metric_inc("langgraph_retry_attempts_processed")
        return processed

    def process_plan_created(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process plan creation events for plan card updates."""
        processed = dict(event)
        processed.setdefault("content", "")
        metadata = ensure_mutable_metadata(processed)
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")

        metadata.setdefault("conversation_id", conversation_id)
        metadata.setdefault("conversationId", conversation_id)
        metadata.setdefault("id", turn_id)
        metadata.setdefault("step_type", event.get("step_type", "plan_created"))
        metadata.setdefault("source", "langgraph_stream")
        metadata.setdefault("timestamp", time.time())
        metadata.setdefault("streaming", False)

        self._metric_inc("langgraph_plan_created_events_processed")
        return processed

    def process_todo_progress(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process todo progress events for plan card updates."""
        processed = dict(event)
        processed.setdefault("content", "")
        metadata = ensure_mutable_metadata(processed)
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")

        metadata.setdefault("conversation_id", conversation_id)
        metadata.setdefault("conversationId", conversation_id)
        metadata.setdefault("id", turn_id)
        metadata.setdefault("step_type", event.get("step_type", "todo_progress"))
        metadata.setdefault("source", "langgraph_stream")
        metadata.setdefault("timestamp", time.time())
        metadata.setdefault("streaming", False)

        self._metric_inc("langgraph_todo_progress_events_processed")
        return processed


__all__ = ["ExecutionPhaseEventProcessor"]
