"""Build post-run turn outcome events from completed LangGraph state.

Responsibilities:
- format the final assistant event while preserving the established metadata
  contract used by streaming consumers
- build the dedicated ``agent_pause_request`` event when the graph records a
  pause decision in state metadata
- emit the existing pause-request metric without taking over pause policy

This module is intentionally separate from live event processing. It only works
with completed ``InteractiveState`` objects after or at the end of a turn.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional
from uuid import uuid4

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    STEP_ASSISTANT_MESSAGE,
)
from agent.graph.state import InteractiveState
from agent.graph.streaming import (
    build_agent_turn_metadata,
    build_delta_event,
    build_final_event as build_graph_final_event,
)
from backend.services.metrics.utils import safe_inc


class TurnOutcomeEventBuilder:
    """Formats post-run streaming events without owning pause decisions."""

    def __init__(
        self,
        *,
        metric_inc: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._metric_inc = metric_inc or safe_inc

    def build_final_event(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """Return a final assistant event mirroring existing SSE schema."""
        event = build_graph_final_event(state, turn_id=turn_id)
        event.setdefault("metadata", {})
        event["metadata"].setdefault("role", "assistant")
        event["metadata"].setdefault("subtype", "assistant_final")
        event["metadata"].setdefault("internal_only", True)
        event["metadata"].setdefault("step_type", STEP_ASSISTANT_MESSAGE)
        event["metadata"].setdefault(
            "ind",
            state.facts.metadata.get("answer_ind", ANSWER_PHASE_INDEX),
        )

        turn_metadata = build_agent_turn_metadata(state)
        for key, value in turn_metadata.items():
            if value is not None:
                event["metadata"][key] = value

        return event

    def build_agent_pause_request_event(
        self,
        state: InteractiveState,
        *,
        turn_id: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        """Create a dedicated agent pause request event for UI rendering."""
        metadata = state.facts.safe_metadata
        pause_request = metadata.get("agent_pause_request")

        if not pause_request:
            return None

        resolved_turn_id = turn_id or f"lg-{state.facts.task_id}-{uuid4()}"

        reason = pause_request.get("reason", "unknown")
        question = pause_request.get("question", "Should I continue?")
        current_progress = pause_request.get("current_progress", {})
        remaining_todos = pause_request.get("remaining_todos", [])
        estimated_time = pause_request.get("estimated_time")
        estimated_tool_calls = pause_request.get("estimated_tool_calls")
        pause_timestamp = pause_request.get("pause_timestamp")

        content_parts = [f"🛑 **Agent Pause Request**\n\n{question}"]

        if current_progress:
            content_parts.append(
                f"\n**Current Progress:**\n"
                f"- Completed todos: {current_progress.get('completed_todos', 0)}\n"
                f"- Remaining todos: {current_progress.get('remaining_todos', 0)}\n"
                f"- Tools executed: {current_progress.get('tools_executed', 0)}\n"
                f"- Iterations: {current_progress.get('iterations', 0)}"
            )

        if remaining_todos:
            todos_preview = remaining_todos[:3]
            todos_text = "\n".join([f"- {todo}" for todo in todos_preview])
            if len(remaining_todos) > 3:
                todos_text += f"\n- ... and {len(remaining_todos) - 3} more"
            content_parts.append(f"\n**Remaining Tasks:**\n{todos_text}")

        if estimated_time or estimated_tool_calls:
            estimates = []
            if estimated_time:
                minutes = estimated_time // 60
                seconds = estimated_time % 60
                if minutes > 0:
                    estimates.append(f"~{minutes}m {seconds}s")
                else:
                    estimates.append(f"~{seconds}s")
            if estimated_tool_calls:
                estimates.append(f"~{estimated_tool_calls} tools")
            content_parts.append(f"\n**Estimated:** {', '.join(estimates)}")

        content_parts.append(
            f"\n\n*Waiting for user response... (reason: {reason})*"
        )

        event = build_delta_event(
            "\n".join(content_parts),
            state.facts.conversation_id,
            turn_id=resolved_turn_id,
        )
        event["type"] = "agent_pause_request"
        event.setdefault("metadata", {})
        event["metadata"]["subtype"] = "agent_pause_request"
        event["metadata"]["requires_user_action"] = True
        event["metadata"]["pause_request"] = {
            "reason": reason,
            "question": question,
            "current_progress": current_progress,
            "remaining_todos": remaining_todos,
            "estimated_time": estimated_time,
            "estimated_tool_calls": estimated_tool_calls,
            "pause_timestamp": pause_timestamp,
        }

        try:
            self._metric_inc("agent_pause_requests_emitted")
        except Exception:
            pass

        return event


__all__ = ["TurnOutcomeEventBuilder"]
