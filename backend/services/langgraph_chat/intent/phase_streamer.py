"""Pre-branch intent-phase reasoning shell.

Emits a shared "Analyzing request..." reasoning section around the intent
classifier call so the UI sees a stable thinking-shell while the classifier
runs and before any handler-graph reasoning starts. Routes events through
the streaming adapter into the turn-scoped state container.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

from agent.graph.contracts.streaming_constants import REASONING_PHASE_INDEX
from backend.services.langgraph_chat.facade_helpers import coerce_turn_sequence

if TYPE_CHECKING:
    from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

logger = logging.getLogger("backend.services.langgraph_chat.facade")

_INTENT_PHASE_REASONING_PLACEHOLDER = "Analyzing request and deciding execution path."
_INTENT_PHASE_REASONING_SECTION = "intent_classification"
_INTENT_PHASE_SUB_TURN_INDEX = -1
_INTENT_PHASE_REASONING_METADATA_KEY = "intent_phase_reasoning_text"


class IntentPhaseStreamer:
    """Emit pre-branch intent-phase reasoning events."""

    def __init__(self, streaming_adapter: Any) -> None:
        """Initialize the streamer.

        Args:
            streaming_adapter: Adapter that normalizes stream events.
        """
        self._streaming_adapter = streaming_adapter

    async def publish_event(
        self,
        *,
        task_id: int,
        event: Dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> None:
        """Publish one pre-branch intent-phase reasoning event via the shared hub.

        Args:
            task_id: Task identifier.
            event: Raw reasoning lifecycle event.
            state_container: Optional turn-scoped state accumulator.
        """
        try:
            from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

            processed = self._streaming_adapter.process_streaming_event(
                event,
                state_container=state_container,
            )
            if processed is None:
                logger.warning(
                    "[FACADE] Dropping invalid intent-phase reasoning event for task %s: %s",
                    task_id,
                    event.get("type"),
                )
                return
            await get_in_memory_stream_hub().publish(task_id=task_id, event=processed)
        except Exception:
            logger.warning(
                "[FACADE] Failed to publish intent-phase reasoning event for task %s",
                task_id,
                exc_info=True,
            )

    @asynccontextmanager
    async def stream(
        self,
        runtime_config: "LangGraphRuntimeConfig",
        state_container: Optional["ChatStateContainer"] = None,
    ) -> AsyncIterator[None]:
        """Emit a shared pre-branch reasoning shell around intent classification.

        Args:
            runtime_config: Runtime config for the current turn.
            state_container: Optional turn-scoped state accumulator.

        Yields:
            Control while intent classification runs.
        """
        metadata = runtime_config.metadata
        task_id = runtime_config.chat_inputs.task_id
        turn_id = metadata.get("turn_id") or metadata.get("id")
        turn_sequence = coerce_turn_sequence(
            metadata.get("turn_sequence", metadata.get("sequence"))
        )
        conversation_id = (
            runtime_config.chat_inputs.conversation_id
            or metadata.get("conversation_id")
            or metadata.get("conversationId")
            or ""
        )

        if not isinstance(turn_id, str) or not turn_id.strip():
            logger.debug(
                "[FACADE] Skipping intent-phase reasoning stream for task %s due to missing turn_id",
                task_id,
            )
            yield
            return

        metadata.setdefault(
            _INTENT_PHASE_REASONING_METADATA_KEY,
            _INTENT_PHASE_REASONING_PLACEHOLDER,
        )
        base_event: Dict[str, Any] = {
            "conversation_id": conversation_id,
            "turn_id": turn_id.strip(),
            "ind": REASONING_PHASE_INDEX,
            "sub_turn_index": _INTENT_PHASE_SUB_TURN_INDEX,
        }
        if turn_sequence is not None:
            base_event["turn_sequence"] = turn_sequence

        await self.publish_event(
            task_id=task_id,
            event={
                **base_event,
                "type": "reasoning_start",
                "step": _INTENT_PHASE_REASONING_SECTION,
                "section_name": _INTENT_PHASE_REASONING_SECTION,
            },
            state_container=state_container,
        )
        await self.publish_event(
            task_id=task_id,
            event={
                **base_event,
                "type": "reasoning_delta",
                "content": _INTENT_PHASE_REASONING_PLACEHOLDER,
            },
            state_container=state_container,
        )
        try:
            yield
        finally:
            await self.publish_event(
                task_id=task_id,
                event={
                    **base_event,
                    "type": "reasoning_section_end",
                    "section_name": _INTENT_PHASE_REASONING_SECTION,
                },
                state_container=state_container,
            )


__all__ = ["IntentPhaseStreamer"]
