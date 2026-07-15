"""Unit tests for ChatMessageService update behavior."""

from types import SimpleNamespace
from unittest.mock import Mock

from backend.services.chat.message_service import ChatMessageService
from backend.services.chat.tool_call_repository import ToolCallRepository


def _build_message(*, observation_tokens: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        message="",
        reasoning_tokens=None,
        observation_tokens=observation_tokens,
        citations=None,
        error=None,
        token_count=0,
    )


def _build_service(message: SimpleNamespace) -> tuple[ChatMessageService, Mock]:
    db = Mock()
    db.get.return_value = message
    return ChatMessageService(db), db


def test_update_message_preserves_observation_when_not_provided() -> None:
    message = _build_message(observation_tokens='["obs-one"]')
    service, _db = _build_service(message)

    service.update_message(
        message_id=1,
        message_text="final",
        reasoning_tokens="reasoning",
    )

    assert message.observation_tokens == '["obs-one"]'


def test_update_message_merges_observations_across_updates() -> None:
    message = _build_message(observation_tokens='[{"content": "obs-one", "sub_turn_index": 0}]')
    service, _db = _build_service(message)

    service.update_message(
        message_id=1,
        message_text="final",
        observation_tokens='[{"content": "obs-two", "sub_turn_index": 1}]',
    )

    assert message.observation_tokens == (
        '[{"content": "obs-one", "sub_turn_index": 0}, '
        '{"content": "obs-two", "sub_turn_index": 1}]'
    )


def test_update_message_uses_superset_payload_without_duplication() -> None:
    message = _build_message(observation_tokens='["obs-one"]')
    service, _db = _build_service(message)

    service.update_message(
        message_id=1,
        message_text="final",
        observation_tokens='["obs-one", "obs-two"]',
    )

    assert message.observation_tokens == '["obs-one", "obs-two"]'


def test_create_tool_calls_persists_rows_when_turn_index_collides() -> None:
    message = _build_message()
    _service, db = _build_service(message)
    tenant_result = Mock()
    tenant_result.scalar_one_or_none.return_value = 7
    no_existing_result = Mock()
    no_existing_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [
        tenant_result,
        no_existing_result,
        tenant_result,
        no_existing_result,
    ]

    ToolCallRepository(db).create_tool_calls(
        chat_message_id=42,
        tool_calls=[
            {
                "tool_call_id": "tc-a",
                "tool_name": "first",
                "tool_arguments": {"x": 1},
                "tool_result": "ok-a",
                "turn_index": 0,
            },
            {
                "tool_call_id": "tc-b",
                "tool_name": "second",
                "tool_arguments": {"x": 2},
                "tool_result": "ok-b",
                "turn_index": 0,
            },
        ],
    )

    assert db.add.call_count == 2
    added_rows = [call.args[0] for call in db.add.call_args_list]
    assert [row.tool_call_id for row in added_rows] == ["tc-a", "tc-b"]
    assert [row.turn_index for row in added_rows] == [0, 0]
