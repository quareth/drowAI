"""Tests for event builder helpers used by chat turn completion boundaries."""

from backend.services.chat.event_builders import build_turn_boundary_completion_events


def test_build_turn_boundary_completion_events_emits_snapshot_and_sentinel() -> None:
    events = build_turn_boundary_completion_events(
        "final answer",
        "conv-1",
        turn_id="task-1-turn-3",
        turn_sequence=3,
        base_metadata={"error": "generation_failed", "stop_reason": "error", "internal_only": True},
    )

    assert len(events) == 2
    snapshot_event, sentinel_event = events

    assert snapshot_event["type"] == "message_delta"
    snapshot_meta = snapshot_event["metadata"]
    assert snapshot_meta["conversation_id"] == "conv-1"
    assert snapshot_meta["conversationId"] == "conv-1"
    assert snapshot_meta["role"] == "assistant"
    assert snapshot_meta["streaming"] is False
    assert snapshot_meta["final_snapshot"] is True
    assert snapshot_meta["boundary_source"] == "turn_boundary"
    assert snapshot_meta["id"] == "task-1-turn-3"
    assert snapshot_meta["turn_sequence"] == 3
    assert snapshot_meta["error"] == "generation_failed"
    assert snapshot_meta["stop_reason"] == "error"
    assert "internal_only" not in snapshot_meta

    assert sentinel_event["type"] == "assistant_final"
    sentinel_meta = sentinel_event["metadata"]
    assert sentinel_meta["boundary_source"] == "turn_boundary"
    assert sentinel_meta["id"] == "task-1-turn-3"
    assert sentinel_meta["turn_sequence"] == 3
    assert sentinel_meta["error"] == "generation_failed"
    assert sentinel_meta["stop_reason"] == "error"
    assert sentinel_meta["internal_only"] is True


def test_build_turn_boundary_completion_events_defaults_empty_conversation_id() -> None:
    events = build_turn_boundary_completion_events(
        "ok",
        "",
        turn_id=None,
        turn_sequence=None,
        base_metadata=None,
    )

    snapshot_event, sentinel_event = events
    assert snapshot_event["metadata"]["conversation_id"] == ""
    assert snapshot_event["metadata"]["conversationId"] == ""
    assert sentinel_event["metadata"]["conversation_id"] == ""
    assert sentinel_event["metadata"]["conversationId"] == ""
