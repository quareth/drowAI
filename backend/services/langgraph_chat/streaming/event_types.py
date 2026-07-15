"""Typed event definitions that mirror the streaming contract.

This module exists to keep backend code aligned with the canonical event schema
documented in `docs/architecture/STREAMING_CONTRACT_ARCHITECTURE.md`. Any new
reasoning/tool/observation event emitted by LangGraph must satisfy these
`TypedDict` definitions so the streaming adapter, persistence layer, and REST
history endpoints continue to operate over a single source of truth.
"""

from __future__ import annotations

from typing import Literal, Mapping, MutableMapping, TypedDict

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
    REASONING_PHASE_INDEX,
    STEP_ASSISTANT_DELTA,
    STEP_ASSISTANT_MESSAGE,
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

# Event type literals surfaced by LangGraph -> adapter pipeline.
ReasoningEventType = Literal["reasoning_start", "reasoning_delta", "reasoning_section_end"]
ObservationEventType = Literal["observation_start", "observation_delta", "observation_section_end"]
ToolEventType = Literal["tool_start", "tool_delta", "tool_end"]
MessageEventType = Literal[
    "message_start",
    "message_delta",
    "section_end",
    "assistant_delta",
    "assistant_message",
]
RetryEventType = Literal["retry_start", "retry_attempt"]
PlanEventType = Literal["plan_created", "todo_progress"]

StreamEventType = Literal[
    ReasoningEventType,
    ObservationEventType,
    ToolEventType,
    MessageEventType,
    RetryEventType,
    PlanEventType,
]


class StreamEventMetadata(TypedDict, total=False):
    """Standard metadata carried by every normalized stream event."""

    conversation_id: str
    conversationId: str  # camelCase alias consumed by legacy UI helpers
    id: str  # turn/thread identifier used for grouping a card
    step_type: str
    ind: int
    sequence: int
    turn_sequence: int
    phase_sequence: int
    reasoning_section_id: str
    sub_turn_index: int
    timestamp: float
    streaming: bool
    section_name: str
    tool: str
    tool_name: str
    parameters: Mapping[str, object]
    status: str
    command: str
    # Retry-specific fields
    attempt: int
    max_attempts: int
    failure_category: str
    alternative_tool: str
    reasoning: str


class StreamEvent(TypedDict, total=False):
    """Normalized event payload exchanged between adapter, hub, and persistence."""

    type: StreamEventType
    content: str
    metadata: StreamEventMetadata
    sequence: int


# Convenience helpers for phase/step validation used in adapter tests.
PHASE_INDEX_BY_STEP_TYPE: Mapping[str, int] = {
    STEP_REASONING_START: REASONING_PHASE_INDEX,
    STEP_REASONING_DELTA: REASONING_PHASE_INDEX,
    STEP_REASONING_SECTION_END: REASONING_PHASE_INDEX,
    STEP_OBSERVATION_START: OBSERVATION_PHASE_INDEX,
    STEP_OBSERVATION_DELTA: OBSERVATION_PHASE_INDEX,
    STEP_OBSERVATION_SECTION_END: OBSERVATION_PHASE_INDEX,
    STEP_TOOL_START: TOOL_PHASE_INDEX,
    STEP_TOOL_DELTA: TOOL_PHASE_INDEX,
    STEP_TOOL_END: TOOL_PHASE_INDEX,
    STEP_MESSAGE_START: ANSWER_PHASE_INDEX,
    STEP_MESSAGE_DELTA: ANSWER_PHASE_INDEX,
    STEP_MESSAGE_SECTION_END: ANSWER_PHASE_INDEX,
    STEP_ASSISTANT_DELTA: ANSWER_PHASE_INDEX,
    STEP_ASSISTANT_MESSAGE: ANSWER_PHASE_INDEX,
    STEP_RETRY_START: REASONING_PHASE_INDEX,
    STEP_RETRY_ATTEMPT: REASONING_PHASE_INDEX,
}


def ensure_mutable_metadata(event: StreamEvent) -> StreamEventMetadata:
    """Return a mutable metadata mapping, creating one if missing.

    The streaming adapter frequently needs to stamp sequence, timestamp, or
    streaming flags onto events. This helper centralizes the defensive copy so
    we never mutate a shared dict coming from LangGraph internals by accident.
    """

    metadata = event.get("metadata")
    if metadata is None:
        metadata = {}
        event["metadata"] = metadata
    elif not isinstance(metadata, MutableMapping):
        metadata = dict(metadata)
        event["metadata"] = metadata
    return metadata  # type: ignore[return-value]


__all__ = [
    "StreamEvent",
    "StreamEventMetadata",
    "StreamEventType",
    "ReasoningEventType",
    "ObservationEventType",
    "ToolEventType",
    "MessageEventType",
    "PlanEventType",
    "PHASE_INDEX_BY_STEP_TYPE",
    "ensure_mutable_metadata",
]
