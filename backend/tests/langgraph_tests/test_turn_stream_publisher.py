"""Tests for TurnStreamPublisher streaming lifecycle and boundary emission."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from backend.services.langgraph_chat.streaming.publisher import TurnStreamPublisher


class _CollectingHub:
    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
        self.published.append({"task_id": task_id, "event": event})


class _EventResult:
    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._events = events

    async def iter_events(self):
        for event in self._events:
            yield dict(event)


@pytest.mark.asyncio
async def test_publish_boundary_completion_events_emits_snapshot_then_sentinel() -> None:
    publisher = TurnStreamPublisher()
    hub = _CollectingHub()

    await publisher.publish_boundary_completion_events(
        task_id=101,
        hub=hub,
        content="complete",
        conversation_id="conv-101",
        turn_id="task-101-turn-4",
        turn_sequence=4,
        base_metadata={"status": "ok"},
    )

    assert [item["event"]["type"] for item in hub.published] == ["message_delta", "assistant_final"]
    snapshot_event = hub.published[0]["event"]
    sentinel_event = hub.published[1]["event"]
    assert snapshot_event["metadata"]["conversation_id"] == "conv-101"
    assert snapshot_event["metadata"]["id"] == "task-101-turn-4"
    assert snapshot_event["metadata"]["turn_sequence"] == 4
    assert snapshot_event["metadata"]["status"] == "ok"
    assert snapshot_event["metadata"]["final_snapshot"] is True
    assert sentinel_event["metadata"]["internal_only"] is True
    assert sentinel_event["metadata"]["id"] == "task-101-turn-4"
    assert sentinel_event["metadata"]["turn_sequence"] == 4


@pytest.mark.asyncio
async def test_publish_turn_result_events_filters_assistant_final_and_sets_turn_sequence() -> None:
    publisher = TurnStreamPublisher()
    hub = _CollectingHub()
    result = _EventResult(
        events=[
            {"type": "message_delta", "content": "chunk-a"},
            {"type": "assistant_final", "content": "final"},
            {"type": "message_delta", "content": "chunk-b", "metadata": {"turn_sequence": 999}},
        ]
    )

    await publisher.publish_turn_result_events(
        hub=hub,
        task_id=202,
        result=result,
        turn_sequence=8,
    )

    assert [item["event"]["type"] for item in hub.published] == ["message_delta", "message_delta"]
    first_metadata = hub.published[0]["event"]["metadata"]
    second_metadata = hub.published[1]["event"]["metadata"]
    assert first_metadata["turn_sequence"] == 8
    assert second_metadata["turn_sequence"] == 999


def test_streaming_state_helpers_warn_and_do_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    class _FailingHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            raise RuntimeError(f"boom-{task_id}-{state}")

    publisher = TurnStreamPublisher()
    hub = _FailingHub()

    with caplog.at_level(logging.WARNING):
        publisher.set_streaming_active(task_id=303, hub=hub)
        publisher.set_streaming_inactive(task_id=303, hub=hub)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Unable to set streaming state for task 303" in message for message in messages)
    assert any("Failed to clear streaming state for task 303" in message for message in messages)

