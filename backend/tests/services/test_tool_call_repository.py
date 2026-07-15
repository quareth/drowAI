"""Unit tests for ToolCallRepository persistence and normalization behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from backend.services.chat.tool_call_repository import ToolCallRepository


def _db_with_existing(existing: object | None = None) -> Mock:
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = existing
    return db


def _assign_incrementing_ids_on_flush(db: Mock, *, start: int = 100) -> None:
    next_id = start

    def flush() -> None:
        nonlocal next_id
        for call in db.add.call_args_list:
            row = call.args[0]
            if getattr(row, "id", None) is None:
                row.id = next_id
                next_id += 1

    db.flush.side_effect = flush


def test_create_tool_calls_inserts_same_index_siblings() -> None:
    db = _db_with_existing()

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


def test_create_tool_calls_recurses_child_calls_and_children_aliases() -> None:
    db = _db_with_existing()
    _assign_incrementing_ids_on_flush(db)

    created = ToolCallRepository(db).create_tool_calls(
        chat_message_id=42,
        tool_calls=[
            {
                "tool_call_id": "parent",
                "tool_name": "parent-tool",
                "child_calls": [
                    {
                        "tool_call_id": "child",
                        "tool_name": "child-tool",
                        "children": [
                            {
                                "tool_call_id": "grandchild",
                                "tool_name": "grandchild-tool",
                            }
                        ],
                    }
                ],
            }
        ],
    )

    assert [row.tool_call_id for row in created] == ["parent", "child", "grandchild"]
    assert created[0].parent_tool_call_id is None
    assert created[1].parent_tool_call_id == created[0].id
    assert created[2].parent_tool_call_id == created[1].id


def test_create_tool_calls_sparse_update_preserves_missing_fields() -> None:
    existing = SimpleNamespace(
        parent_tool_call_id=7,
        tool_id=10,
        tool_name="existing-name",
        tool_arguments={"keep": True},
        tool_result="existing-result",
        turn_index=3,
        tab_index=4,
        reasoning_tokens="existing-reasoning",
        generated_images=[{"url": "existing"}],
        tool_call_tokens=15,
    )
    db = _db_with_existing(existing)

    result = ToolCallRepository(db).create_tool_calls(
        chat_message_id=42,
        tool_calls=[
            {
                "tool_call_id": "tc-existing",
                "tool_arguments": '{"updated": true}',
                "turn_index": "8",
            }
        ],
        parent_tool_call_id=9,
    )

    assert result == [existing]
    assert existing.parent_tool_call_id == 9
    assert existing.tool_id == 10
    assert existing.tool_name == "existing-name"
    assert existing.tool_arguments == {"updated": True}
    assert existing.tool_result == "existing-result"
    assert existing.turn_index == 8
    assert existing.tab_index == 4
    assert existing.reasoning_tokens == "existing-reasoning"
    assert existing.generated_images == [{"url": "existing"}]
    assert existing.tool_call_tokens == 15
    db.add.assert_not_called()
    db.flush.assert_called_once()


def test_create_tool_calls_normalizes_json_and_text_fields() -> None:
    db = _db_with_existing()

    ToolCallRepository(db).create_tool_calls(
        chat_message_id=42,
        tool_calls=[
            {
                "tool_call_id": "tc-json",
                "tool_name": "json-tool",
                "tool_arguments": '{"x": 1}',
                "tool_result": {"ok": True},
                "generated_images": '[{"url": "image.png"}]',
            },
            {
                "tool_call_id": "tc-invalid-json",
                "tool_name": "invalid-json-tool",
                "tool_arguments": "not-json",
                "tool_result": ["a", "b"],
                "generated_images": "null",
            },
        ],
    )

    added_rows = [call.args[0] for call in db.add.call_args_list]
    assert added_rows[0].tool_arguments == {"x": 1}
    assert added_rows[0].tool_result == '{"ok": true}'
    assert added_rows[0].generated_images == [{"url": "image.png"}]
    assert added_rows[1].tool_arguments == {}
    assert added_rows[1].tool_result == '["a", "b"]'
    assert added_rows[1].generated_images is None


def test_create_tool_calls_uses_fallback_index_for_invalid_turn_index() -> None:
    db = _db_with_existing()

    ToolCallRepository(db).create_tool_calls(
        chat_message_id=42,
        tool_calls=[
            {"tool_call_id": "tc-a", "tool_name": "first", "turn_index": "bad"},
            {"tool_call_id": "tc-b", "tool_name": "second", "turn_index": None},
        ],
    )

    added_rows = [call.args[0] for call in db.add.call_args_list]
    assert [row.turn_index for row in added_rows] == [0, 1]
