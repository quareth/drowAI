"""Tests for chat turn-number allocation transaction boundaries."""

from __future__ import annotations

from unittest.mock import Mock

from backend.services.chat.turn_number_service import TurnNumberService


def test_get_next_turn_number_in_session_uses_caller_transaction() -> None:
    db = Mock()
    result = Mock()
    result.fetchone.return_value = (7,)
    db.execute.return_value = result

    turn_number = TurnNumberService().get_next_turn_number_in_session(
        db,
        task_id=42,
        conversation_id="conv-42",
    )

    assert turn_number == 7
    db.execute.assert_called_once()
    db.commit.assert_not_called()
