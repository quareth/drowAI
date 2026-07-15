"""Builders for chat, stream, and interrupt event dictionaries.

These helpers centralize the dict shape used by chat submit, langgraph_chat
streaming, error handling, and interrupt flows so callers do not duplicate
metadata composition.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from backend.core.time_utils import format_iso, utc_now


def attach_conversation_ids(meta: Optional[Dict[str, Any]], conv_id: str) -> Dict[str, Any]:
    meta = dict(meta or {})
    meta["conversation_id"] = conv_id
    meta["conversationId"] = conv_id
    return meta


def build_user_message_event(content: str, conv_id: str, extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = attach_conversation_ids(extra_meta or {}, conv_id)
    meta.setdefault("role", "user")
    meta.setdefault("message_type", "user_input")
    meta.setdefault("streaming", False)
    return {
        "type": "user_message",
        "content": content,
        "metadata": meta,
        "timestamp": format_iso(utc_now()),
    }


def build_interrupt_event(
    task_id: int,
    thread_id: str,
    interrupt_type: str,
    payload: Dict[str, Any],
    graph_name: str,
    interrupt_id: str,
    checkpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build SSE/WebSocket event for graph interrupt.

    Args:
        task_id: Task ID with the interrupt.
        thread_id: LangGraph thread ID for resume.
        interrupt_type: Type of interrupt (tool_approval, plan_review).
        payload: Interrupt payload with details for user.
        graph_name: Which graph is interrupted (for correct resume).
        interrupt_id: Stable identifier for this interrupt instance.
        checkpoint_id: Optional checkpointer identifier for resume tracking.

    Returns:
        Event dict ready for streaming to frontend.
    """
    return {
        "type": "graph_interrupt",
        "task_id": task_id,
        "thread_id": thread_id,
        "interrupt_id": interrupt_id,
        "checkpoint_id": checkpoint_id,
        "interrupt_type": interrupt_type,
        "payload": payload,
        "graph_name": graph_name,
        "timestamp": format_iso(utc_now()),
    }


def build_assistant_final_event(
    content: str,
    conv_id: str,
    *,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a canonical assistant_final event for stream/replay alignment."""
    meta = attach_conversation_ids(extra_meta or {}, conv_id)
    meta.setdefault("role", "assistant")
    meta.setdefault("streaming", False)
    meta.setdefault("subtype", "assistant_final")
    # assistant_final is a turn-boundary sentinel, not user-visible content.
    meta.setdefault("internal_only", True)
    return {
        "type": "assistant_final",
        "content": content,
        "metadata": meta,
        "timestamp": format_iso(utc_now()),
    }


def build_turn_boundary_completion_events(
    content: str,
    conv_id: str,
    *,
    turn_id: Optional[str],
    turn_sequence: Optional[int],
    base_metadata: Optional[Dict[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """Build the canonical final boundary pair for a completed assistant turn."""
    resolved_conversation_id = conv_id or ""
    snapshot_metadata = attach_conversation_ids(base_metadata or {}, resolved_conversation_id)
    snapshot_metadata.setdefault("role", "assistant")
    snapshot_metadata["streaming"] = False
    snapshot_metadata["step_type"] = "message_delta"
    snapshot_metadata["subtype"] = "message_delta"
    snapshot_metadata["final_snapshot"] = True
    snapshot_metadata["boundary_source"] = "turn_boundary"
    snapshot_metadata.pop("internal_only", None)
    if turn_id:
        snapshot_metadata["id"] = turn_id
    if turn_sequence is not None:
        snapshot_metadata["turn_sequence"] = turn_sequence
    snapshot_event = {"type": "message_delta", "content": content, "metadata": snapshot_metadata}

    sentinel_meta: Dict[str, Any] = {"boundary_source": "turn_boundary"}
    for key in (
        "error",
        "stop_reason",
        "status",
        "outcome_type",
        "retryable",
        "refusal",
    ):
        if snapshot_metadata.get(key) is not None:
            sentinel_meta[key] = snapshot_metadata[key]
    if turn_id:
        sentinel_meta["id"] = turn_id
    if turn_sequence is not None:
        sentinel_meta["turn_sequence"] = turn_sequence
    sentinel_event = build_assistant_final_event(content, resolved_conversation_id, extra_meta=sentinel_meta)
    return [snapshot_event, sentinel_event]


__all__ = [
    "attach_conversation_ids",
    "build_assistant_final_event",
    "build_turn_boundary_completion_events",
    "build_interrupt_event",
    "build_user_message_event",
]
