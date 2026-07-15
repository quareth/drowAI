"""Unit tests for chat-turn reservation orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call

import backend.services.chat.turn_orchestrator as orchestrator_module
from backend.services.chat.turn_orchestrator import ChatTurnOrchestrator


def _wire_orchestrator(
    monkeypatch,
    *,
    chat: Mock,
    reader: Mock,
    turn: Mock,
) -> Mock:
    db = Mock()
    monkeypatch.setattr(orchestrator_module, "ChatMessageService", lambda _db: chat)
    monkeypatch.setattr(
        orchestrator_module, "ConversationHistoryReader", lambda _db: reader
    )
    monkeypatch.setattr(orchestrator_module, "get_turn_number_service", lambda: turn)
    return db


def test_reserve_chat_turn_pair_uses_tail_parent_and_commits_after_assistant(
    monkeypatch,
) -> None:
    chat = Mock()
    reader = Mock()
    turn = Mock()
    db = _wire_orchestrator(monkeypatch, chat=chat, reader=reader, turn=turn)

    reader.get_conversation_history.return_value = [SimpleNamespace(id=77)]
    turn.get_next_turn_number_in_session.return_value = 5
    user_msg = SimpleNamespace(id=101)
    assistant_msg = SimpleNamespace(id=102)
    chat.reserve_message.side_effect = [user_msg, assistant_msg]

    result = ChatTurnOrchestrator(db).reserve_chat_turn_pair(
        task_id=12,
        conversation_id="conv-12",
        user_message="hello",
    )

    assert result == (101, 102, "task-12-turn-5", 5)
    reader.get_conversation_history.assert_called_once_with(
        task_id=12,
        conversation_id="conv-12",
        limit=None,
    )
    turn.get_next_turn_number_in_session.assert_called_once_with(
        db,
        task_id=12,
        conversation_id="conv-12",
    )
    turn.get_next_turn_number.assert_not_called()
    assert chat.method_calls == [
        call.reserve_message(
            task_id=12,
            conversation_id="conv-12",
            parent_message_id=77,
            message_type="user",
            turn_number=5,
        ),
        call.update_message(101, "hello", token_count=0),
        call.reserve_message(
            task_id=12,
            conversation_id="conv-12",
            parent_message_id=101,
            message_type="assistant",
            turn_number=5,
        ),
    ]
    chat.get_or_create_root_message.assert_not_called()
    db.commit.assert_called_once()


def test_reserve_user_message_uses_root_fallback_and_commits(monkeypatch) -> None:
    chat = Mock()
    reader = Mock()
    turn = Mock()
    db = _wire_orchestrator(monkeypatch, chat=chat, reader=reader, turn=turn)

    reader.get_conversation_history.return_value = []
    chat.get_or_create_root_message.return_value = SimpleNamespace(id=10)
    turn.get_next_turn_number_in_session.return_value = 2
    chat.reserve_message.return_value = SimpleNamespace(id=11)

    result = ChatTurnOrchestrator(db).reserve_user_message(
        task_id=12,
        conversation_id="conv-12",
        user_message="hello",
    )

    assert result == (11, 2)
    turn.get_next_turn_number_in_session.assert_called_once_with(
        db,
        task_id=12,
        conversation_id="conv-12",
    )
    turn.get_next_turn_number.assert_not_called()
    chat.get_or_create_root_message.assert_called_once_with(
        task_id=12,
        conversation_id="conv-12",
        message_type="SYSTEM",
        message="",
    )
    chat.reserve_message.assert_called_once_with(
        task_id=12,
        conversation_id="conv-12",
        parent_message_id=10,
        message_type="user",
        turn_number=2,
    )
    chat.update_message.assert_called_once_with(11, "hello", token_count=0)
    db.commit.assert_called_once()


def test_reserve_user_message_parents_from_canonical_sibling_tail(monkeypatch) -> None:
    """New raw turns keep canonical-tail parenting, not active-branch selection."""
    chat = Mock()
    reader = Mock()
    turn = Mock()
    db = _wire_orchestrator(monkeypatch, chat=chat, reader=reader, turn=turn)

    reader.get_conversation_history.return_value = [
        SimpleNamespace(id=1, latest_child_message_id=2),
        SimpleNamespace(id=2, latest_child_message_id=3),
        SimpleNamespace(id=3, latest_child_message_id=None),
        SimpleNamespace(id=4, latest_child_message_id=None),
    ]
    turn.get_next_turn_number_in_session.return_value = 6
    chat.reserve_message.return_value = SimpleNamespace(id=5)

    result = ChatTurnOrchestrator(db).reserve_user_message(
        task_id=12,
        conversation_id="conv-12",
        user_message="continue after all sibling branches",
    )

    assert result == (5, 6)
    chat.reserve_message.assert_called_once_with(
        task_id=12,
        conversation_id="conv-12",
        parent_message_id=4,
        message_type="user",
        turn_number=6,
    )
    chat.get_or_create_root_message.assert_not_called()
