"""Unit tests for hidden compression snapshot persistence and epoch gating."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.services.langgraph_chat.compression.snapshot_repository as repository_module
from backend.services.chat.conversation_history_reader import SYSTEM_SUMMARY_MESSAGE_TYPE
from backend.services.langgraph_chat.compression.snapshot_repository import (
    COMPRESSION_EPOCH_METADATA_KEY,
    CompressionSnapshotRepository,
)


def _db_returning(row: object | None) -> Mock:
    db = Mock()
    db.execute.return_value.scalar_one_or_none.return_value = row
    return db


def _db_for_persistence(snapshots: list[object]) -> Mock:
    """Return a DB mock for the task lock and scoped snapshot read."""
    db = Mock()
    lock_result = Mock()
    lock_result.scalar_one_or_none.return_value = 1
    snapshot_result = Mock()
    snapshot_result.scalars.return_value.unique.return_value.all.return_value = snapshots
    db.execute.side_effect = [lock_result, snapshot_result]
    return db


def test_imports_chat_message_service_and_repository_without_cycle() -> None:
    assert importlib.import_module("backend.services.chat.message_service") is not None
    assert (
        importlib.import_module(
            "backend.services.langgraph_chat.compression.snapshot_repository"
        )
        is not None
    )


def test_latest_snapshot_returns_latest_summary_row() -> None:
    summary = SimpleNamespace(id=7)
    db = _db_returning(summary)

    latest = CompressionSnapshotRepository(db).latest_snapshot(
        task_id=1,
        conversation_id="conv-1",
    )

    assert latest is summary
    db.execute.assert_called_once()


def test_latest_epoch_metadata_parses_persisted_citation_payload() -> None:
    summary = SimpleNamespace(
        citations={
            COMPRESSION_EPOCH_METADATA_KEY: {
                "epoch_id": "epoch-7",
                "source_tokens": "1200",
                "through_message_id": 77,
            }
        }
    )

    metadata = CompressionSnapshotRepository(
        _db_returning(summary)
    ).latest_epoch_metadata(
        task_id=1,
        conversation_id="conv-1",
    )

    assert metadata is not None
    assert metadata.epoch_id == "epoch-7"
    assert metadata.source_tokens == 1200
    assert metadata.through_message_id == 77


def test_latest_epoch_metadata_ignores_invalid_payloads() -> None:
    summary = SimpleNamespace(
        citations={
            COMPRESSION_EPOCH_METADATA_KEY: {
                "epoch_id": "epoch-7",
                "source_tokens": -1,
            }
        }
    )

    metadata = CompressionSnapshotRepository(
        _db_returning(summary)
    ).latest_epoch_metadata(
        task_id=1,
        conversation_id="conv-1",
    )

    assert metadata is None


def test_latest_epoch_metadata_ignores_invalid_cutoff() -> None:
    summary = SimpleNamespace(
        citations={
            COMPRESSION_EPOCH_METADATA_KEY: {
                "epoch_id": "epoch-7",
                "source_tokens": 1200,
                "through_message_id": "77",
            }
        }
    )

    metadata = CompressionSnapshotRepository(
        _db_returning(summary)
    ).latest_epoch_metadata(
        task_id=1,
        conversation_id="conv-1",
    )

    assert metadata is None


def test_persist_snapshot_writes_epoch_metadata_and_commits(monkeypatch) -> None:
    db = _db_for_persistence([])
    summary_msg = SimpleNamespace(id=99)
    fake_chat = Mock()
    fake_chat.reserve_message.return_value = summary_msg
    monkeypatch.setattr(repository_module, "ChatMessageService", lambda _db: fake_chat)

    persisted = CompressionSnapshotRepository(db).persist_snapshot(
        task_id=1,
        conversation_id="conv-1",
        summary_text="snapshot",
        token_count=123,
        compression_epoch_id="epoch-9",
        source_tokens=3210,
        through_message_id=77,
    )

    assert persisted is summary_msg
    fake_chat.reserve_message.assert_called_once_with(
        task_id=1,
        conversation_id="conv-1",
        parent_message_id=None,
        message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
        turn_number=None,
    )
    assert db.execute.call_count == 2
    assert "FOR UPDATE" in str(db.execute.call_args_list[0].args[0])
    fake_chat.update_message.assert_called_once_with(
        99,
        "snapshot",
        token_count=123,
        citations={
            COMPRESSION_EPOCH_METADATA_KEY: {
                "epoch_id": "epoch-9",
                "source_tokens": 3210,
                "through_message_id": 77,
            }
        },
    )
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(summary_msg)


def test_persist_snapshot_returns_existing_epoch_cutoff_without_duplicate(
    monkeypatch,
) -> None:
    existing = SimpleNamespace(
        id=99,
        citations={
            COMPRESSION_EPOCH_METADATA_KEY: {
                "epoch_id": "epoch-9",
                "source_tokens": 3210,
                "through_message_id": 77,
            }
        },
    )
    db = _db_for_persistence([existing])
    fake_chat = Mock()
    monkeypatch.setattr(repository_module, "ChatMessageService", lambda _db: fake_chat)

    persisted = CompressionSnapshotRepository(db).persist_snapshot(
        task_id=1,
        conversation_id="conv-1",
        summary_text="retry text must not overwrite",
        token_count=999,
        compression_epoch_id="epoch-9",
        source_tokens=9999,
        through_message_id=77,
    )

    assert persisted is existing
    fake_chat.reserve_message.assert_not_called()
    fake_chat.update_message.assert_not_called()
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(existing)
    db.rollback.assert_not_called()


def test_persist_snapshot_rejects_same_epoch_with_different_cutoff(
    monkeypatch,
) -> None:
    existing = SimpleNamespace(
        id=99,
        citations={
            COMPRESSION_EPOCH_METADATA_KEY: {
                "epoch_id": "epoch-9",
                "source_tokens": 3210,
                "through_message_id": 77,
            }
        },
    )
    db = _db_for_persistence([existing])
    fake_chat = Mock()
    monkeypatch.setattr(repository_module, "ChatMessageService", lambda _db: fake_chat)

    with pytest.raises(ValueError, match="different cutoff"):
        CompressionSnapshotRepository(db).persist_snapshot(
            task_id=1,
            conversation_id="conv-1",
            summary_text="must not advance",
            compression_epoch_id="epoch-9",
            source_tokens=3210,
            through_message_id=78,
        )

    fake_chat.reserve_message.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_persist_snapshot_rolls_back_partial_write(monkeypatch) -> None:
    db = _db_for_persistence([])
    summary_msg = SimpleNamespace(id=99)
    fake_chat = Mock()
    fake_chat.reserve_message.return_value = summary_msg
    fake_chat.update_message.side_effect = RuntimeError("update failed")
    monkeypatch.setattr(repository_module, "ChatMessageService", lambda _db: fake_chat)

    with pytest.raises(RuntimeError, match="update failed"):
        CompressionSnapshotRepository(db).persist_snapshot(
            task_id=1,
            conversation_id="conv-1",
            summary_text="snapshot",
            compression_epoch_id="epoch-9",
            source_tokens=3210,
            through_message_id=77,
        )

    fake_chat.reserve_message.assert_called_once()
    db.commit.assert_not_called()
    db.refresh.assert_not_called()
    db.rollback.assert_called_once()


def test_persist_snapshot_rejects_invalid_cutoff_before_reserving_row(
    monkeypatch,
) -> None:
    db = _db_returning(SimpleNamespace(id=42))
    fake_chat = Mock()
    monkeypatch.setattr(repository_module, "ChatMessageService", lambda _db: fake_chat)

    with pytest.raises(ValueError, match="through_message_id"):
        CompressionSnapshotRepository(db).persist_snapshot(
            task_id=1,
            conversation_id="conv-1",
            summary_text="snapshot",
            compression_epoch_id="epoch-9",
            source_tokens=3210,
            through_message_id=0,
        )

    db.execute.assert_not_called()
    fake_chat.reserve_message.assert_not_called()
    db.commit.assert_not_called()


def test_snapshot_persistence_preserves_raw_parent_and_latest_child_relationships() -> None:
    """Hidden summary persistence leaves the raw tree unchanged."""
    raw_latest = SimpleNamespace(
        id=42,
        parent_message_id=40,
        latest_child_message_id=41,
    )
    tenant_result = Mock()
    tenant_result.scalar_one_or_none.return_value = 7
    db = Mock()
    db.execute.return_value = tenant_result
    added_messages: list[object] = []

    def _add(message: object) -> None:
        added_messages.append(message)

    def _flush() -> None:
        if added_messages and getattr(added_messages[-1], "id", None) is None:
            setattr(added_messages[-1], "id", 99)

    def _get(_model: object, message_id: int) -> object | None:
        if message_id == raw_latest.id:
            return raw_latest
        if message_id == 99 and added_messages:
            return added_messages[-1]
        return None

    db.add.side_effect = _add
    db.flush.side_effect = _flush
    db.get.side_effect = _get

    summary = CompressionSnapshotRepository(db).persist_snapshot(
        task_id=1,
        conversation_id="conv-1",
        summary_text="summary",
        compression_epoch_id="epoch-1",
        source_tokens=500,
    )

    assert raw_latest.parent_message_id == 40
    assert raw_latest.latest_child_message_id == 41
    assert summary.parent_message_id is None
    assert summary.latest_child_message_id is None
    assert all(
        call.args != (repository_module.ChatMessage, raw_latest.id)
        for call in db.get.call_args_list
    )
