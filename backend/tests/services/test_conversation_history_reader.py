"""Unit tests for prompt-authoritative conversation history reading."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.chat.conversation_history_reader import (
    SYSTEM_SUMMARY_MESSAGE_TYPE,
    ConversationHistoryReader,
)


def _make_chatmessage_execute_result(rows: list[SimpleNamespace]) -> Mock:
    result = Mock()
    result.scalars.return_value.unique.return_value.all.return_value = rows
    return result


def _message(
    *,
    message_id: int,
    parent_message_id: int | None,
    message_type: str,
    message: str,
    created_at: datetime,
    tool_calls: list[SimpleNamespace] | None = None,
    task_id: int = 1,
    conversation_id: str = "conv-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        task_id=task_id,
        conversation_id=conversation_id,
        parent_message_id=parent_message_id,
        message_type=message_type,
        message=message,
        created_at=created_at,
        tool_calls=tool_calls or [],
    )


def _reader_for_rows(rows: list[SimpleNamespace]) -> ConversationHistoryReader:
    db = Mock()
    db.execute.return_value = _make_chatmessage_execute_result(rows)
    return ConversationHistoryReader(db)


def _dt(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=timezone.utc)


def test_get_conversation_history_orders_roots_and_child_subtrees() -> None:
    root = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="root",
        created_at=_dt(1),
    )
    first_child = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="first child",
        created_at=_dt(2),
    )
    nested_child = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="nested child",
        created_at=_dt(3),
    )
    regenerated_child = _message(
        message_id=4,
        parent_message_id=1,
        message_type="assistant",
        message="regenerated child",
        created_at=_dt(4),
    )

    history = _reader_for_rows(
        [regenerated_child, nested_child, first_child, root]
    ).get_conversation_history(task_id=1, conversation_id="conv-1")

    assert [message.id for message in history] == [1, 2, 3, 4]


def test_get_conversation_history_preserves_created_at_sibling_subtree_order() -> None:
    """Each earlier sibling subtree is completed before the next sibling."""
    root = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="root",
        created_at=_dt(1),
    )
    earlier_sibling = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="earlier sibling",
        created_at=_dt(2),
    )
    earlier_grandchild = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="earlier subtree child",
        created_at=_dt(3),
    )
    later_sibling = _message(
        message_id=4,
        parent_message_id=1,
        message_type="assistant",
        message="later sibling",
        created_at=_dt(4),
    )

    history = _reader_for_rows(
        [later_sibling, earlier_grandchild, root, earlier_sibling]
    ).get_conversation_history(task_id=1, conversation_id="conv-1")

    assert [message.id for message in history] == [1, 2, 3, 4]


def test_get_conversation_history_applies_cursor_and_limit_semantics() -> None:
    rows = [
        _message(
            message_id=index,
            parent_message_id=None if index == 1 else index - 1,
            message_type="user" if index % 2 else "assistant",
            message=f"message {index}",
            created_at=_dt(index),
        )
        for index in range(1, 6)
    ]

    after_page = _reader_for_rows(rows).get_conversation_history(
        task_id=1,
        conversation_id="conv-1",
        after=2,
        limit=2,
    )
    before_page = _reader_for_rows(rows).get_conversation_history(
        task_id=1,
        conversation_id="conv-1",
        before=5,
        limit=2,
    )
    latest_page = _reader_for_rows(rows).get_conversation_history(
        task_id=1,
        conversation_id="conv-1",
        limit=2,
    )

    assert [message.id for message in after_page] == [3, 4]
    assert [message.id for message in before_page] == [3, 4]
    assert [message.id for message in latest_page] == [4, 5]


def test_get_conversation_history_filters_summary_markers_by_default() -> None:
    rows = [
        _message(
            message_id=1,
            parent_message_id=None,
            message_type="user",
            message="hello",
            created_at=_dt(1),
        ),
        _message(
            message_id=2,
            parent_message_id=1,
            message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
            message="summary",
            created_at=_dt(2),
        ),
        _message(
            message_id=3,
            parent_message_id=2,
            message_type="assistant",
            message="after summary",
            created_at=_dt(3),
        ),
    ]

    filtered = _reader_for_rows(rows).get_conversation_history(task_id=1, conversation_id="conv-1")
    with_markers = _reader_for_rows(rows).get_conversation_history(
        task_id=1,
        conversation_id="conv-1",
        include_summary_markers=True,
    )

    assert [message.id for message in filtered] == [1, 3]
    assert [message.id for message in with_markers] == [1, 2, 3]


def test_build_openai_conversation_history_uses_latest_summary_window() -> None:
    rows = [
        _message(
            message_id=1,
            parent_message_id=None,
            message_type="user",
            message="old question",
            created_at=_dt(1),
        ),
        _message(
            message_id=2,
            parent_message_id=1,
            message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
            message="old summary",
            created_at=_dt(2),
        ),
        _message(
            message_id=3,
            parent_message_id=2,
            message_type="user",
            message="mid question",
            created_at=_dt(3),
        ),
        _message(
            message_id=4,
            parent_message_id=3,
            message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
            message="latest summary",
            created_at=_dt(4),
        ),
        _message(
            message_id=5,
            parent_message_id=4,
            message_type="assistant",
            message="new answer",
            created_at=_dt(5),
        ),
    ]
    rows[3].citations = {
        "context_compression": {"through_message_id": 3}
    }

    history = _reader_for_rows(rows).build_openai_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )

    assert history == [
        {"role": "system", "content": "latest summary"},
        {"role": "assistant", "content": "new answer"},
    ]


def test_aligned_history_preserves_duplicate_content_ids_from_one_query() -> None:
    """Source IDs align by traversal position, never by matching content."""
    rows = [
        _message(
            message_id=1,
            parent_message_id=None,
            message_type="user",
            message="same content",
            created_at=_dt(1),
        ),
        _message(
            message_id=2,
            parent_message_id=1,
            message_type="assistant",
            message="same content",
            created_at=_dt(2),
        ),
        _message(
            message_id=3,
            parent_message_id=2,
            message_type="user",
            message="same content",
            created_at=_dt(3),
        ),
    ]
    reader = _reader_for_rows(rows)

    aligned = reader.build_aligned_openai_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )

    assert [message["content"] for message in aligned.messages] == [
        "same content",
        "same content",
        "same content",
    ]
    assert aligned.source_message_ids == (1, 2, 3)
    assert all("source_message_id" not in message for message in aligned.messages)
    assert reader.db.execute.call_count == 1


def test_summary_cutoff_reconstruction_keeps_retained_tail_and_raw_visibility() -> None:
    """Target lock: summaries hide prompt prefixes without deleting raw history."""
    old_user = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="old question",
        created_at=_dt(1),
    )
    cutoff = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="old answer",
        created_at=_dt(2),
    )
    retained_user = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="retained question",
        created_at=_dt(3),
    )
    retained_assistant = _message(
        message_id=4,
        parent_message_id=3,
        message_type="assistant",
        message="retained answer",
        created_at=_dt(4),
    )
    summary = _message(
        message_id=5,
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="summary through message 2",
        created_at=_dt(5),
    )
    summary.citations = {
        "context_compression": {
            "epoch_id": "epoch-1",
            "source_tokens": 500,
            "through_message_id": 2,
        }
    }
    rows = [old_user, cutoff, retained_user, retained_assistant, summary]
    reader = _reader_for_rows(rows)

    raw_history = reader.get_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )
    prompt_history = reader.build_aligned_openai_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )

    assert [message.id for message in raw_history] == [1, 2, 3, 4]
    assert list(prompt_history.messages) == [
        {"role": "system", "content": "summary through message 2"},
        {"role": "user", "content": "retained question"},
        {"role": "assistant", "content": "retained answer"},
    ]
    assert prompt_history.source_message_ids == (5, 3, 4)


def test_summary_reconstruction_excludes_reserved_current_turn_rows() -> None:
    """Queued reconstruction keeps the active user and placeholder separate."""
    old_user = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="old question",
        created_at=_dt(1),
    )
    retained_assistant = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="retained answer",
        created_at=_dt(2),
    )
    current_user = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="active user message",
        created_at=_dt(3),
    )
    reserved_assistant = _message(
        message_id=4,
        parent_message_id=3,
        message_type="assistant",
        message="",
        created_at=_dt(4),
    )
    summary = _message(
        message_id=5,
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="summary through old question",
        created_at=_dt(5),
    )
    summary.citations = {
        "context_compression": {"through_message_id": 1}
    }

    history = _reader_for_rows(
        [old_user, retained_assistant, current_user, reserved_assistant, summary]
    ).build_aligned_openai_conversation_history(
        task_id=1,
        conversation_id="conv-1",
        exclude_message_ids={3, 4},
    )

    assert list(history.messages) == [
        {"role": "system", "content": "summary through old question"},
        {"role": "assistant", "content": "retained answer"},
    ]
    assert history.source_message_ids == (5, 2)
    assert "active user message" not in str(history.messages)


def test_cutoff_reconstruction_preserves_sibling_subtree_order() -> None:
    """Cutoffs retain every sibling subtree in the reader's canonical order."""
    root = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="root question",
        created_at=_dt(1),
    )
    earlier_sibling = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="earlier reply",
        created_at=_dt(2),
    )
    earlier_grandchild = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="earlier follow-up",
        created_at=_dt(3),
    )
    later_sibling = _message(
        message_id=4,
        parent_message_id=1,
        message_type="assistant",
        message="later regenerated reply",
        created_at=_dt(4),
    )
    summary = _message(
        message_id=5,
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="summary through the root",
        created_at=_dt(5),
    )
    summary.citations = {
        "context_compression": {"through_message_id": 1}
    }
    reader = _reader_for_rows(
        [later_sibling, summary, earlier_grandchild, root, earlier_sibling]
    )

    raw_history = reader.get_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )
    prompt_history = reader.build_aligned_openai_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )

    assert [message.id for message in raw_history] == [1, 2, 3, 4]
    assert [message["content"] for message in prompt_history.messages] == [
        "summary through the root",
        "earlier reply",
        "earlier follow-up",
        "later regenerated reply",
    ]
    assert prompt_history.source_message_ids == (5, 2, 3, 4)


def test_cutoff_reconstruction_skips_summary_with_foreign_cutoff() -> None:
    """A cutoff outside the requested scope cannot authorize reconstruction."""
    foreign_cutoff = _message(
        message_id=99,
        parent_message_id=None,
        message_type="SYSTEM",
        message="foreign non-prompt row",
        created_at=_dt(1),
        task_id=2,
        conversation_id="conv-2",
    )
    old_user = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="old question",
        created_at=_dt(2),
    )
    cutoff = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="old answer",
        created_at=_dt(3),
    )
    retained_user = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="retained question",
        created_at=_dt(4),
    )
    valid_summary = _message(
        message_id=4,
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="valid summary",
        created_at=_dt(5),
    )
    valid_summary.citations = {
        "context_compression": {"through_message_id": 2}
    }
    foreign_summary = _message(
        message_id=5,
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="foreign-cutoff summary",
        created_at=_dt(6),
    )
    foreign_summary.citations = {
        "context_compression": {"through_message_id": 99}
    }

    history = _reader_for_rows(
        [
            foreign_cutoff,
            old_user,
            cutoff,
            retained_user,
            valid_summary,
            foreign_summary,
        ]
    ).build_openai_conversation_history(task_id=1, conversation_id="conv-1")

    assert history == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "retained question"},
    ]


@pytest.mark.parametrize(
    "citations",
    [
        None,
        {"context_compression": {"through_message_id": "2"}},
        {"context_compression": {"through_message_id": 999}},
    ],
    ids=("missing-metadata", "malformed-cutoff", "missing-cutoff-row"),
)
def test_invalid_summary_returns_full_raw_canonical_history(
    citations: dict[str, object] | None,
) -> None:
    """An unusable latest summary must never cause raw-message loss."""
    old_user = _message(
        message_id=1,
        parent_message_id=None,
        message_type="user",
        message="old question",
        created_at=_dt(1),
    )
    old_assistant = _message(
        message_id=2,
        parent_message_id=1,
        message_type="assistant",
        message="old answer",
        created_at=_dt(2),
    )
    retained_user = _message(
        message_id=3,
        parent_message_id=2,
        message_type="user",
        message="retained question",
        created_at=_dt(3),
    )
    summary = _message(
        message_id=4,
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        message="unusable summary",
        created_at=_dt(4),
    )
    summary.citations = citations

    history = _reader_for_rows(
        [old_user, old_assistant, retained_user, summary]
    ).build_aligned_openai_conversation_history(
        task_id=1,
        conversation_id="conv-1",
    )

    assert list(history.messages) == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "retained question"},
    ]
    assert history.source_message_ids == (1, 2, 3)


def test_convert_chat_messages_to_openai_projects_assistant_tool_calls() -> None:
    tool_call = SimpleNamespace(
        tool_call_id="call-1",
        tool_name="scan_host",
        tool_arguments={"host": "127.0.0.1"},
    )
    assistant = _message(
        message_id=1,
        parent_message_id=None,
        message_type="assistant",
        message="Calling tool.",
        created_at=_dt(1),
        tool_calls=[tool_call],
    )

    history = ConversationHistoryReader(Mock()).convert_chat_messages_to_openai([assistant])

    assert history == [
        {
            "role": "assistant",
            "content": "Calling tool.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "scan_host",
                        "arguments": '{"host": "127.0.0.1"}',
                    },
                }
            ],
        }
    ]
