"""Unit tests for resolving turn identity from reserved ChatMessage rows."""

from types import SimpleNamespace
from unittest.mock import Mock

from backend.services.chat.turn_identity_resolver import (
    resolve_turn_identity_from_reserved_message,
    resolve_turn_identity_from_reserved_message_best_effort,
)


def test_resolve_turn_identity_uses_turn_number_from_reserved_message() -> None:
    session = Mock()
    session.get.return_value = SimpleNamespace(turn_number=2)

    resolved = resolve_turn_identity_from_reserved_message(
        session,
        task_id=9,
        reserved_message_id=12,
    )

    assert resolved == ("task-9-turn-2", 2)


def test_resolve_turn_identity_falls_back_to_message_id_without_turn_number() -> None:
    session = Mock()
    session.get.return_value = SimpleNamespace(turn_number=None)

    resolved = resolve_turn_identity_from_reserved_message(
        session,
        task_id=9,
        reserved_message_id=12,
    )

    assert resolved == ("task-9-turn-12", 12)


def test_resolve_turn_identity_best_effort_uses_session_and_closes() -> None:
    session = Mock()
    session.get.return_value = SimpleNamespace(turn_number=2)

    resolved = resolve_turn_identity_from_reserved_message_best_effort(
        task_id=9,
        reserved_message_id=12,
        session_factory=lambda: session,
    )

    assert resolved == ("task-9-turn-2", 2)
    session.close.assert_called_once()


def test_resolve_turn_identity_best_effort_returns_none_tuple_on_error() -> None:
    session = Mock()
    session.get.side_effect = RuntimeError("boom")

    resolved = resolve_turn_identity_from_reserved_message_best_effort(
        task_id=9,
        reserved_message_id=12,
        session_factory=lambda: session,
    )

    assert resolved == (None, None)
    session.close.assert_called_once()


def test_resolve_turn_identity_best_effort_short_circuits_on_none_message_id() -> None:
    session_factory = Mock()

    resolved = resolve_turn_identity_from_reserved_message_best_effort(
        task_id=9,
        reserved_message_id=None,
        session_factory=session_factory,
    )

    assert resolved == (None, None)
    session_factory.assert_not_called()


def test_resolve_turn_identity_best_effort_propagates_session_factory_error() -> None:
    session_factory = Mock(side_effect=RuntimeError("open failed"))

    try:
        resolve_turn_identity_from_reserved_message_best_effort(
            task_id=9,
            reserved_message_id=12,
            session_factory=session_factory,
        )
    except RuntimeError as exc:
        assert str(exc) == "open failed"
    else:
        raise AssertionError("expected session factory failure to propagate")
