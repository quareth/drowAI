"""Canonical streaming event schema and normalization utilities.

This module enforces a typed, graph-agnostic event contract at the backend
boundary. It normalizes raw event dicts into a consistent shape so the
frontend can reliably parse and group messages.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Literal, Annotated, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, TypeAdapter


def _load_agent_streaming_constants():
    """Load literal constants from agent without importing agent.graph (avoids heavy graph bootstrap)."""
    constants_path = (
        Path(__file__).resolve().parents[3] / "agent" / "graph" / "contracts" / "streaming_constants.py"
    )
    spec = importlib.util.spec_from_file_location(
        "drowai_agent_graph_contracts_streaming_constants",
        constants_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load streaming constants from {constants_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_sc = _load_agent_streaming_constants()
ANSWER_PHASE_INDEX = _sc.ANSWER_PHASE_INDEX
OBSERVATION_PHASE_INDEX = _sc.OBSERVATION_PHASE_INDEX
REASONING_PHASE_INDEX = _sc.REASONING_PHASE_INDEX
STEP_ASSISTANT_DELTA = _sc.STEP_ASSISTANT_DELTA
STEP_ASSISTANT_MESSAGE = _sc.STEP_ASSISTANT_MESSAGE
STEP_MESSAGE_DELTA = _sc.STEP_MESSAGE_DELTA
STEP_MESSAGE_SECTION_END = _sc.STEP_MESSAGE_SECTION_END
STEP_MESSAGE_START = _sc.STEP_MESSAGE_START
STEP_OBSERVATION_DELTA = _sc.STEP_OBSERVATION_DELTA
STEP_OBSERVATION_SECTION_END = _sc.STEP_OBSERVATION_SECTION_END
STEP_OBSERVATION_START = _sc.STEP_OBSERVATION_START
STEP_REASONING_DELTA = _sc.STEP_REASONING_DELTA
STEP_REASONING_SECTION_END = _sc.STEP_REASONING_SECTION_END
STEP_REASONING_START = _sc.STEP_REASONING_START
STEP_RETRY_ATTEMPT = _sc.STEP_RETRY_ATTEMPT
STEP_RETRY_START = _sc.STEP_RETRY_START
STEP_TOOL_DELTA = _sc.STEP_TOOL_DELTA
STEP_TOOL_END = _sc.STEP_TOOL_END
STEP_TOOL_START = _sc.STEP_TOOL_START
TOOL_PHASE_INDEX = _sc.TOOL_PHASE_INDEX

logger = logging.getLogger(__name__)


StreamEventType = Literal[
    "user_message",
    "assistant_delta",
    "assistant_message",
    "assistant_final",
    "message_start",
    "message_delta",
    "message_section_end",
    "section_end",
    "reasoning_start",
    "reasoning_delta",
    "reasoning_section_end",
    "tool_start",
    "tool_delta",
    "tool_end",
    "tool_batch_start",
    "tool_batch_end",
    "observation_start",
    "observation_delta",
    "observation_section_end",
    "retry_start",
    "retry_attempt",
    "graph_interrupt",
    "plan_created",
    "todo_progress",
    "stream_error",
    "status",
    "agent_pause_request",
    "intent_summary",
]

STREAM_EVENT_TYPE_SET = {
    "user_message",
    "assistant_delta",
    "assistant_message",
    "assistant_final",
    "message_start",
    "message_delta",
    "message_section_end",
    "section_end",
    "reasoning_start",
    "reasoning_delta",
    "reasoning_section_end",
    "tool_start",
    "tool_delta",
    "tool_end",
    "tool_batch_start",
    "tool_batch_end",
    "observation_start",
    "observation_delta",
    "observation_section_end",
    "retry_start",
    "retry_attempt",
    "graph_interrupt",
    "plan_created",
    "todo_progress",
    "stream_error",
    "status",
    "agent_pause_request",
    "intent_summary",
}

STEP_TYPE_DEFAULTS = {
    "user_message": "user_message",
    "assistant_delta": STEP_ASSISTANT_DELTA,
    "assistant_message": STEP_ASSISTANT_MESSAGE,
    "assistant_final": STEP_ASSISTANT_MESSAGE,
    "message_start": STEP_MESSAGE_START,
    "message_delta": STEP_MESSAGE_DELTA,
    "message_section_end": STEP_MESSAGE_SECTION_END,
    "section_end": STEP_MESSAGE_SECTION_END,
    "reasoning_start": STEP_REASONING_START,
    "reasoning_delta": STEP_REASONING_DELTA,
    "reasoning_section_end": STEP_REASONING_SECTION_END,
    "tool_start": STEP_TOOL_START,
    "tool_delta": STEP_TOOL_DELTA,
    "tool_end": STEP_TOOL_END,
    "tool_batch_start": "tool_batch_start",
    "tool_batch_end": "tool_batch_end",
    "observation_start": STEP_OBSERVATION_START,
    "observation_delta": STEP_OBSERVATION_DELTA,
    "observation_section_end": STEP_OBSERVATION_SECTION_END,
    "retry_start": STEP_RETRY_START,
    "retry_attempt": STEP_RETRY_ATTEMPT,
    "plan_created": "plan_created",
    "todo_progress": "todo_progress",
    "graph_interrupt": "graph_interrupt",
    "stream_error": "stream_error",
    "status": "status",
    "agent_pause_request": "agent_pause_request",
    "intent_summary": "intent_summary",
}

IND_DEFAULTS = {
    "user_message": -1,
    "assistant_delta": ANSWER_PHASE_INDEX,
    "assistant_message": ANSWER_PHASE_INDEX,
    "assistant_final": ANSWER_PHASE_INDEX,
    "message_start": ANSWER_PHASE_INDEX,
    "message_delta": ANSWER_PHASE_INDEX,
    "message_section_end": ANSWER_PHASE_INDEX,
    "section_end": ANSWER_PHASE_INDEX,
    "reasoning_start": REASONING_PHASE_INDEX,
    "reasoning_delta": REASONING_PHASE_INDEX,
    "reasoning_section_end": REASONING_PHASE_INDEX,
    "tool_start": TOOL_PHASE_INDEX,
    "tool_delta": TOOL_PHASE_INDEX,
    "tool_end": TOOL_PHASE_INDEX,
    "tool_batch_start": TOOL_PHASE_INDEX,
    "tool_batch_end": TOOL_PHASE_INDEX,
    "observation_start": OBSERVATION_PHASE_INDEX,
    "observation_delta": OBSERVATION_PHASE_INDEX,
    "observation_section_end": OBSERVATION_PHASE_INDEX,
    "retry_start": REASONING_PHASE_INDEX,
    "retry_attempt": REASONING_PHASE_INDEX,
}

STREAMING_DEFAULT_TRUE = {
    "assistant_delta",
    "message_start",
    "message_delta",
    "message_section_end",
    "section_end",
    "reasoning_start",
    "reasoning_delta",
    "reasoning_section_end",
    "tool_start",
    "tool_delta",
    "observation_start",
    "observation_delta",
    "retry_start",
    "retry_attempt",
}

TURN_SCOPED_TYPES = {
    "assistant_delta",
    "assistant_message",
    "assistant_final",
    "message_start",
    "message_delta",
    "message_section_end",
    "section_end",
    "reasoning_start",
    "reasoning_delta",
    "reasoning_section_end",
    "tool_start",
    "tool_delta",
    "tool_end",
    "observation_start",
    "observation_delta",
    "observation_section_end",
    "retry_start",
    "retry_attempt",
    "plan_created",
    "todo_progress",
    "user_message",
    "agent_pause_request",
    "intent_summary",
}


class StreamEventMetadata(BaseModel):
    conversation_id: Optional[str] = None
    conversationId: Optional[str] = None
    id: Optional[str] = None
    step_type: Optional[str] = None
    ind: Optional[int] = None
    sequence: Optional[int] = None
    turn_sequence: Optional[int] = None
    phase_sequence: Optional[int] = None
    reasoning_section_id: Optional[str] = None
    streaming: Optional[bool] = None
    section_name: Optional[str] = None
    tool: Optional[str] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_batch_id: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    command: Optional[str] = None
    retry_attempt: Optional[int] = None
    retry_max_attempts: Optional[int] = None
    failure_category: Optional[str] = None
    error: Optional[str] = None
    subtype: Optional[str] = None
    internal_only: Optional[bool] = None
    requires_user_action: Optional[bool] = None
    role: Optional[str] = None
    message_type: Optional[str] = None
    timestamp: Optional[str | float] = None

    model_config = ConfigDict(extra="allow")


class StreamEvent(BaseModel):
    type: StreamEventType
    content: str = ""
    metadata: StreamEventMetadata = Field(default_factory=StreamEventMetadata)
    sequence: Optional[int] = None
    task_id: Optional[int] = None
    timestamp: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class UserMessageEvent(StreamEvent):
    type: Literal["user_message"]


class AssistantDeltaEvent(StreamEvent):
    type: Literal["assistant_delta"]


class AssistantMessageEvent(StreamEvent):
    type: Literal["assistant_message"]


class AssistantFinalEvent(StreamEvent):
    type: Literal["assistant_final"]


class MessageStartEvent(StreamEvent):
    type: Literal["message_start"]


class MessageDeltaEvent(StreamEvent):
    type: Literal["message_delta"]


class MessageSectionEndEvent(StreamEvent):
    type: Literal["message_section_end"]


class SectionEndEvent(StreamEvent):
    type: Literal["section_end"]


class ReasoningStartEvent(StreamEvent):
    type: Literal["reasoning_start"]


class ReasoningDeltaEvent(StreamEvent):
    type: Literal["reasoning_delta"]


class ReasoningSectionEndEvent(StreamEvent):
    type: Literal["reasoning_section_end"]


class ToolStartEvent(StreamEvent):
    type: Literal["tool_start"]


class ToolDeltaEvent(StreamEvent):
    type: Literal["tool_delta"]


class ToolEndEvent(StreamEvent):
    type: Literal["tool_end"]


class ToolBatchStartEvent(StreamEvent):
    type: Literal["tool_batch_start"]


class ToolBatchEndEvent(StreamEvent):
    type: Literal["tool_batch_end"]


class ObservationStartEvent(StreamEvent):
    type: Literal["observation_start"]


class ObservationDeltaEvent(StreamEvent):
    type: Literal["observation_delta"]


class ObservationSectionEndEvent(StreamEvent):
    type: Literal["observation_section_end"]


class RetryStartEvent(StreamEvent):
    type: Literal["retry_start"]


class RetryAttemptEvent(StreamEvent):
    type: Literal["retry_attempt"]


class GraphInterruptEvent(StreamEvent):
    type: Literal["graph_interrupt"]
    thread_id: Optional[str] = None
    interrupt_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    interrupt_type: Optional[Literal["tool_approval", "plan_review", "clarify_request"]] = None
    payload: Optional[Dict[str, Any]] = None
    graph_name: Optional[str] = None


class PlanCreatedEvent(StreamEvent):
    type: Literal["plan_created"]


class TodoProgressEvent(StreamEvent):
    type: Literal["todo_progress"]


class StreamErrorEvent(StreamEvent):
    type: Literal["stream_error"]


class StatusEvent(StreamEvent):
    type: Literal["status"]


class AgentPauseRequestEvent(StreamEvent):
    type: Literal["agent_pause_request"]


class IntentSummaryEvent(StreamEvent):
    type: Literal["intent_summary"]


PacketObj = Annotated[
    Union[
        UserMessageEvent,
        AssistantDeltaEvent,
        AssistantMessageEvent,
        AssistantFinalEvent,
        MessageStartEvent,
        MessageDeltaEvent,
        MessageSectionEndEvent,
        SectionEndEvent,
        ReasoningStartEvent,
        ReasoningDeltaEvent,
        ReasoningSectionEndEvent,
        ToolStartEvent,
        ToolDeltaEvent,
        ToolEndEvent,
        ToolBatchStartEvent,
        ToolBatchEndEvent,
        ObservationStartEvent,
        ObservationDeltaEvent,
        ObservationSectionEndEvent,
        RetryStartEvent,
        RetryAttemptEvent,
        GraphInterruptEvent,
        PlanCreatedEvent,
        TodoProgressEvent,
        StreamErrorEvent,
        StatusEvent,
        AgentPauseRequestEvent,
        IntentSummaryEvent,
    ],
    Field(discriminator="type"),
]

_PACKET_OBJ_ADAPTER = TypeAdapter(PacketObj)


class Placement(BaseModel):
    """Ordering metadata for packetized streaming events."""

    turn_index: int
    tab_index: int = 0
    sub_turn_index: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class Packet(BaseModel):
    """Packet envelope for streaming events."""

    placement: Placement
    obj: PacketObj
    sequence: Optional[int] = None
    task_id: Optional[int] = None
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            return int(float(value))
        except ValueError:
            return None
    if isinstance(value, float):
        return int(value)
    return None


def _first_str(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _json_safe(value: Any) -> Any:
    """Convert nested values into JSON-serializable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(exclude_none=True))
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def normalize_stream_event(
    event: Mapping[str, Any],
    *,
    task_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize raw event dicts into the canonical streaming contract."""
    if not isinstance(event, Mapping):
        logger.warning("Stream event normalization skipped: event is not a mapping")
        return None

    raw_type = event.get("type")
    if not raw_type:
        logger.warning("Stream event normalization dropped: missing type")
        return None
    event_type = str(raw_type)

    data: Dict[str, Any] = dict(event)
    metadata: Dict[str, Any] = dict(data.get("metadata") or {})
    if "error" in metadata and metadata["error"] is not None and not isinstance(metadata["error"], str):
        metadata["error"] = str(metadata["error"])

    conversation_id = _first_str(
        metadata.get("conversation_id"),
        metadata.get("conversationId"),
        data.get("conversation_id"),
        data.get("conversationId"),
    )
    if conversation_id:
        metadata.setdefault("conversation_id", conversation_id)
        metadata.setdefault("conversationId", conversation_id)

    turn_id = _first_str(metadata.get("id"), data.get("turn_id"), data.get("id"))
    if turn_id:
        metadata.setdefault("id", turn_id)

    if "streaming" not in metadata:
        metadata["streaming"] = event_type in STREAMING_DEFAULT_TRUE

    step_type = STEP_TYPE_DEFAULTS.get(event_type)
    if step_type:
        metadata.setdefault("step_type", step_type)

    ind = IND_DEFAULTS.get(event_type)
    if ind is not None:
        metadata.setdefault("ind", ind)

    sequence_value = _coerce_int(data.get("sequence"))
    if sequence_value is not None:
        data["sequence"] = sequence_value
        metadata.setdefault("sequence", sequence_value)

    turn_sequence_value = _coerce_int(metadata.get("turn_sequence") or data.get("turn_sequence"))
    if turn_sequence_value is not None:
        metadata["turn_sequence"] = turn_sequence_value
    elif sequence_value is not None and event_type in TURN_SCOPED_TYPES:
        metadata["turn_sequence"] = sequence_value

    if task_id is not None and "task_id" not in data:
        data["task_id"] = task_id

    content = data.get("content")
    if content is None:
        data["content"] = ""
    elif not isinstance(content, str):
        data["content"] = str(content)

    data["metadata"] = metadata

    if event_type not in STREAM_EVENT_TYPE_SET:
        logger.debug("Stream event normalization: unknown type %s", event_type)
        return data

    try:
        validated = _PACKET_OBJ_ADAPTER.validate_python(data)
    except ValidationError as exc:
        logger.warning("Stream event validation failed for type %s: %s", event_type, exc)
        return data

    return validated.model_dump(exclude_none=True)


def _resolve_placement_from_event(
    event: Mapping[str, Any],
) -> Placement:
    metadata = event.get("metadata") or {}
    turn_index = _coerce_int(
        metadata.get("turn_sequence")
        or metadata.get("sequence")
        or event.get("turn_sequence")
        or event.get("sequence")
    )
    tab_index = _coerce_int(metadata.get("ind") or event.get("ind"))
    sub_turn_index = _coerce_int(
        metadata.get("run_id")
        or metadata.get("iteration")
        or metadata.get("sub_turn_index")
        or event.get("run_id")
        or event.get("iteration")
        or event.get("sub_turn_index")
    )

    return Placement(
        turn_index=turn_index if turn_index is not None else 0,
        tab_index=tab_index if tab_index is not None else 0,
        sub_turn_index=sub_turn_index,
    )


def normalize_stream_packet(
    event: Mapping[str, Any],
    *,
    task_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize raw events into the canonical packet contract.

    Accepts either:
    - Already packetized payloads {placement, obj, ...}
    - Legacy stream event dicts {type, content, metadata}
    """
    if not isinstance(event, Mapping):
        logger.warning("Stream packet normalization skipped: event is not a mapping")
        return None

    if "placement" in event and "obj" in event:
        data: Dict[str, Any] = dict(event)
        if task_id is not None and "task_id" not in data:
            data["task_id"] = task_id
        try:
            validated = Packet.model_validate(data)
        except ValidationError as exc:
            logger.warning("Stream packet validation failed: %s", exc)
            return _json_safe(data)
        return validated.model_dump(exclude_none=True)

    # Legacy stream event; normalize and wrap in packet envelope.
    normalized_event = normalize_stream_event(event, task_id=task_id)
    if normalized_event is None:
        return None

    placement = _resolve_placement_from_event(normalized_event)
    metadata = normalized_event.get("metadata") or {}
    # Ensure obj.metadata has sub_turn_index when placement has it (history/replay robustness)
    if placement.sub_turn_index is not None and metadata.get("sub_turn_index") is None:
        metadata["sub_turn_index"] = placement.sub_turn_index
        normalized_event["metadata"] = metadata
    conversation_id = _first_str(
        metadata.get("conversation_id"),
        metadata.get("conversationId"),
        normalized_event.get("conversation_id"),
        normalized_event.get("conversationId"),
    )
    turn_id = _first_str(
        metadata.get("id"),
        metadata.get("turn_id"),
        normalized_event.get("turn_id"),
        normalized_event.get("id"),
    )
    sequence_value = _coerce_int(
        normalized_event.get("sequence")
        or metadata.get("sequence")
    )
    turn_sequence_value = _coerce_int(metadata.get("turn_sequence"))
    if (
        sequence_value is not None
        and turn_sequence_value is not None
        and sequence_value == turn_sequence_value
    ):
        sequence_value = None

    packet_data: Dict[str, Any] = {
        "placement": placement,
        "obj": normalized_event,
        "sequence": sequence_value,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
    }
    if task_id is not None:
        packet_data["task_id"] = task_id
    elif normalized_event.get("task_id") is not None:
        packet_data["task_id"] = normalized_event.get("task_id")

    try:
        validated = Packet.model_validate(packet_data)
    except ValidationError as exc:
        logger.warning("Stream packet validation failed: %s", exc)
        return _json_safe(packet_data)
    return validated.model_dump(exclude_none=True)


__all__ = [
    "StreamEvent",
    "StreamEventMetadata",
    "StreamEventType",
    "normalize_stream_event",
    "normalize_stream_packet",
    "PacketObj",
    "Packet",
    "Placement",
]
