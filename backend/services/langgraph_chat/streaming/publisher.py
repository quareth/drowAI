"""Streaming lifecycle and boundary emission for LangGraph turn execution.

This module centralizes stream-state transitions and turn-completion event
publication so orchestration services can delegate streaming concerns to a
single focused component.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.services.chat.event_builders import build_turn_boundary_completion_events

logger = logging.getLogger(__name__)


class TurnStreamPublisher:
    """Own streaming state transitions and turn-boundary event fanout."""

    def set_streaming_active(self, *, task_id: int, hub: Any) -> None:
        """Mark a task as actively streaming on the shared hub."""
        try:
            hub.set_streaming_state(task_id, True)
        except Exception as exc:
            logger.warning("Unable to set streaming state for task %s: %s", task_id, exc)

    def set_streaming_inactive(
        self,
        *,
        task_id: int,
        hub: Any,
        warn_on_error: bool = True,
    ) -> None:
        """Clear active streaming state for a task on the shared hub."""
        try:
            hub.set_streaming_state(task_id, False)
        except Exception as exc:
            if warn_on_error:
                logger.warning("Failed to clear streaming state for task %s: %s", task_id, exc)

    async def publish_turn_result_events(
        self,
        *,
        hub: Any,
        task_id: int,
        result: Any,
        turn_sequence: Optional[int],
    ) -> None:
        """Publish non-final stream events produced by a turn result iterator."""
        async for event in result.iter_events():
            if event.get("type") == "assistant_final":
                continue
            event.setdefault("metadata", {})
            if turn_sequence is not None:
                event["metadata"].setdefault("turn_sequence", turn_sequence)
            await hub.publish(task_id, event)

    async def publish_boundary_completion_events(
        self,
        *,
        task_id: int,
        hub: Any,
        content: str,
        conversation_id: str,
        turn_id: Optional[str],
        turn_sequence: Optional[int],
        base_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit the canonical turn-boundary completion event pair."""
        completion_events = build_turn_boundary_completion_events(
            content,
            conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            base_metadata=base_metadata,
        )
        for event in completion_events:
            await hub.publish(task_id, event)
