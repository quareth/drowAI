"""Unit tests for stream status event contracts."""

from unittest.mock import AsyncMock

import pytest

from backend.services.langgraph_chat.streaming.status_events import (
    build_context_window_lifecycle_event,
    build_retry_state_event,
    emit_context_window_event,
    emit_interrupt_state_event,
    publish_context_window_lifecycle_event,
)


def test_retry_state_event_accepts_declined_terminal_state() -> None:
    """Provider refusals use a first-class terminal retry lifecycle state."""
    event = build_retry_state_event(task_id=31, state="declined", turn_id="turn-31")

    assert event is not None
    assert event["metadata"]["state"] == "declined"


def test_emit_context_window_event_uses_deterministic_handoff_defaults(monkeypatch) -> None:
    """Event should include stable non-blocking handoff defaults."""
    captured: list[tuple[int, dict]] = []

    def _capture(task_id: int, event: dict) -> None:
        captured.append((task_id, event))

    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events._publish_task_status",
        _capture,
    )

    emit_context_window_event(
        task_id=11,
        conversation_id="conv-11",
        max_tokens=128_000,
        used_tokens=450,
        remaining_tokens=127_550,
        ratio=0.0035,
        ceiling_reached=False,
        turn_sequence=4,
        revision=4,
        snapshot_kind="measured",
    )

    assert len(captured) == 1
    task_id, event = captured[0]
    assert task_id == 11
    assert event["type"] == "status"
    assert event["content"] == "context_window"
    assert event["metadata"]["recommended_next_action"] == "none"
    assert event["metadata"]["compression_candidate"] is False
    assert event["metadata"]["turn_sequence"] == 4
    assert event["metadata"]["revision"] == 4
    assert event["metadata"]["snapshot_kind"] == "measured"


def test_emit_context_window_event_normalizes_unknown_action(monkeypatch) -> None:
    """Unknown handoff action values should be normalized to deterministic default."""
    captured: list[tuple[int, dict]] = []

    def _capture(task_id: int, event: dict) -> None:
        captured.append((task_id, event))

    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events._publish_task_status",
        _capture,
    )

    emit_context_window_event(
        task_id=12,
        conversation_id="conv-12",
        max_tokens=128_000,
        used_tokens=128_000,
        remaining_tokens=0,
        ratio=1.0,
        ceiling_reached=True,
        recommended_next_action="pause",
        compression_candidate=True,
    )

    assert len(captured) == 1
    _, event = captured[0]
    assert event["metadata"]["recommended_next_action"] == "none"
    assert event["metadata"]["compression_candidate"] is True


def test_build_context_window_lifecycle_event_normalizes_identity_and_state() -> None:
    event = build_context_window_lifecycle_event(
        task_id=12,
        conversation_id=" conv-12 ",
        state=" COMPACTING ",
        turn_id=" turn-12 ",
        epoch_id=" epoch-12 ",
    )

    assert event is not None
    assert event["type"] == "status"
    assert event["content"] == "context_window"
    assert event["metadata"] == {
        "task_id": 12,
        "conversation_id": "conv-12",
        "conversationId": "conv-12",
        "state": "compacting",
        "turn_id": "turn-12",
        "epoch_id": "epoch-12",
        "timestamp": event["metadata"]["timestamp"],
    }
    assert isinstance(event["metadata"]["timestamp"], str)


@pytest.mark.asyncio
async def test_publish_context_window_lifecycle_event_reuses_awaited_publisher(
    monkeypatch,
) -> None:
    awaited_publisher = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events._publish_task_status_awaited",
        awaited_publisher,
    )

    published = await publish_context_window_lifecycle_event(
        task_id=13,
        conversation_id="conv-13",
        state="completed",
        turn_id="turn-13",
        epoch_id="epoch-13",
    )

    assert published is True
    awaited_publisher.assert_awaited_once()
    task_id, event = awaited_publisher.await_args.args
    assert task_id == 13
    assert event["metadata"]["state"] == "completed"
    assert event["metadata"]["turn_id"] == "turn-13"
    assert event["metadata"]["epoch_id"] == "epoch-13"


@pytest.mark.asyncio
async def test_publish_context_window_lifecycle_event_rejects_invalid_contract(
    monkeypatch,
) -> None:
    awaited_publisher = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events._publish_task_status_awaited",
        awaited_publisher,
    )

    published = await publish_context_window_lifecycle_event(
        task_id=14,
        conversation_id="conv-14",
        state="paused",
        turn_id="turn-14",
        epoch_id="epoch-14",
    )

    assert published is False
    awaited_publisher.assert_not_awaited()


def test_emit_interrupt_state_event_normalizes_state_and_sets_pending_and_timestamp(monkeypatch) -> None:
    captured: list[tuple[int, dict]] = []

    def _capture(task_id: int, event: dict) -> None:
        captured.append((task_id, event))

    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events._publish_task_status",
        _capture,
    )

    emit_interrupt_state_event(
        task_id=21,
        interrupt_id="intr-21",
        state=" pending ",
        interrupt_type="tool_approval",
    )

    assert len(captured) == 1
    task_id, event = captured[0]
    assert task_id == 21
    assert event["type"] == "status"
    assert event["content"] == "interrupt_state"
    assert event["metadata"]["state"] == "PENDING"
    assert event["metadata"]["has_pending"] is True
    assert isinstance(event["metadata"]["timestamp"], str)


def test_emit_interrupt_state_event_skips_unknown_state(monkeypatch) -> None:
    captured: list[tuple[int, dict]] = []

    def _capture(task_id: int, event: dict) -> None:
        captured.append((task_id, event))

    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events._publish_task_status",
        _capture,
    )

    emit_interrupt_state_event(
        task_id=22,
        interrupt_id="intr-22",
        state="UNKNOWN_STATE",
    )

    assert captured == []
