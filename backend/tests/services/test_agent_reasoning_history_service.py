"""Tests for the extracted reasoning history service."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.services.streaming.reasoning_history_service import AgentReasoningHistoryService


def test_replay_history_preserves_chat_packets_and_cursor_pagination() -> None:
    db = MagicMock()
    service = AgentReasoningHistoryService(db)
    stream_store = MagicMock()
    stream_store.list_bootstrap_page.return_value = SimpleNamespace(
        rows=[
            SimpleNamespace(sequence=1, payload={"sequence": 1, "type": "user_message"}),
            SimpleNamespace(sequence=2, payload={"sequence": 2, "type": "message_start"}),
        ],
        next_after=2,
        has_more=True,
    )

    with patch(
        "backend.services.streaming.reasoning_history_service.StreamEventStore",
        return_value=stream_store,
    ):
        payload = service.get_replay_history(task_id=123, after=0, limit=2)

    assert [item["type"] for item in payload["items"]] == [
        "user_message",
        "message_start",
    ]
    assert payload["nextAfter"] == 2
    assert payload["hasMore"] is True
    stream_store.list_bootstrap_page.assert_called_once_with(
        task_id=123,
        after=0,
        limit=2,
    )


def test_stream_event_history_takes_precedence_and_preserves_filtering() -> None:
    db = MagicMock()
    service = AgentReasoningHistoryService(db)

    stream_store = MagicMock()
    stream_store.has_events.return_value = True
    stream_store.list_after.return_value = [
        SimpleNamespace(
            sequence=11,
            payload={
                "sequence": 11,
                "type": "reasoning_delta",
                "content": "keep-me",
                "metadata": {"note": "keep"},
            },
        ),
        SimpleNamespace(
            sequence=12,
            payload={
                "sequence": 12,
                "type": "assistant_delta",
                "content": "drop-me",
                "metadata": {"note": "drop"},
            },
        ),
    ]

    with patch("backend.services.streaming.reasoning_history_service.StreamEventStore", return_value=stream_store), patch(
        "backend.services.streaming.reasoning_history_service.AgentReasoningStore"
    ) as legacy_store, patch(
        "backend.services.streaming.reasoning_history_service.read_reasoning_log_entries"
    ) as file_reader:
        payload = service.get_history(task_id=123, after=0, before=None, limit=200, order="asc")

    assert [item["type"] for item in payload["items"]] == ["reasoning_delta"]
    assert payload["items"][0]["content"] == "keep-me"
    assert payload["nextAfter"] == 12
    legacy_store.assert_not_called()
    file_reader.assert_not_called()


def test_legacy_db_history_normalizes_rows_and_keeps_cursor_contract() -> None:
    db = MagicMock()
    service = AgentReasoningHistoryService(db)
    row = SimpleNamespace(
        sequence=10,
        type="reasoning_delta",
        content="history-observation",
        log_metadata={"conversation_id": "conv-1", "note": "from-db"},
        timestamp=datetime.utcnow(),
    )

    stream_store = MagicMock()
    stream_store.has_events.return_value = False
    legacy_store = MagicMock()
    legacy_store.list_after.return_value = [row]

    with patch("backend.services.streaming.reasoning_history_service.StreamEventStore", return_value=stream_store), patch(
        "backend.services.streaming.reasoning_history_service.AgentReasoningStore",
        return_value=legacy_store,
    ), patch(
        "backend.services.streaming.reasoning_history_service.normalize_stream_packet",
        side_effect=lambda payload, task_id=None: payload,
    ):
        payload = service.get_history(task_id=123, after=0, before=None, limit=200, order="asc")

    assert len(payload["items"]) == 1
    packet = payload["items"][0]
    assert packet["content"] == "history-observation"
    assert packet["metadata"]["conversation_id"] == "conv-1"
    assert payload["nextAfter"] == 10
    assert payload["hasMore"] is False


def test_file_history_normalizes_entries_and_preserves_existing_desc_after_behavior() -> None:
    db = MagicMock()
    service = AgentReasoningHistoryService(db)
    stream_store = MagicMock()
    stream_store.has_events.return_value = False

    entries = [
        {"sequence": 1, "type": "react_step", "content": "first", "metadata": {"conversationId": "conv-1"}},
        {"sequence": 2, "type": "react_step", "content": "second", "metadata": {"conversation_id": "conv-1"}},
        {"sequence": 3, "type": "react_step", "content": "third", "metadata": {"conversation_id": "conv-1"}},
    ]

    with patch("backend.services.streaming.reasoning_history_service.StreamEventStore", return_value=stream_store), patch(
        "backend.services.streaming.reasoning_history_service.AgentReasoningStore",
        side_effect=RuntimeError("legacy store unavailable"),
    ), patch(
        "backend.services.streaming.reasoning_history_service.read_reasoning_log_entries",
        return_value=entries,
    ):
        payload = service.get_history(task_id=77, after=0, before=None, limit=2, order="desc")

    sequences = [item["sequence"] for item in payload["items"]]
    assert sequences == [2, 3]
    assert payload["nextAfter"] == 2
    assert payload["hasMore"] is True
