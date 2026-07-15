"""Tests for canonical chat turn-event persistence service."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Task, User
from backend.services.chat.turn_event_service import ChatTurnEventService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_assistant_message(db) -> ChatMessage:
    user = User(username="chat-turn-event-user", password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, tenant_id=1, name="chat-turn-event-task")
    db.add(task)
    db.flush()
    message = ChatMessage(
        task_id=task.id,
        tenant_id=task.tenant_id,
        conversation_id="conv-1",
        parent_message_id=None,
        latest_child_message_id=None,
        message_type="assistant",
        message="",
        token_count=0,
        turn_number=7,
    )
    db.add(message)
    db.flush()
    return message


def test_replace_events_for_message_replaces_rows_and_orders_by_phase_sequence() -> None:
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        created = service.replace_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            tool_calls=[
                {
                    "tool_call_id": "tc-2",
                    "tool_batch_id": "tb-refresh",
                    "tool_name": "second_tool",
                    "tool_arguments": {"z": 1, "a": 2},
                    "tool_result": {"ok": True},
                    "phase_sequence": 2,
                    "turn_index": 1,
                }
            ],
            observation_sections=[
                {
                    "content": "first observation",
                    "phase_sequence": 1,
                    "sub_turn_index": 0,
                }
            ],
        )
        assert len(created) == 2

        db.commit()
        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()
        assert [row.phase_sequence for row in rows] == [1, 2]
        assert [row.kind for row in rows] == ["observation", "tool"]
        assert rows[1].tool_call_id == "tc-2"
        assert rows[1].event_metadata is not None
        assert rows[1].event_metadata["tool_name"] == "second_tool"
        assert rows[1].event_metadata["tool_batch_id"] == "tb-refresh"

        service.replace_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            tool_calls=[],
            observation_sections=[
                {"content": "replacement only", "phase_sequence": 0, "sub_turn_index": 0}
            ],
        )
        db.commit()
        replacement_rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()
        assert len(replacement_rows) == 1
        assert replacement_rows[0].phase_sequence == 0
        assert replacement_rows[0].kind == "observation"
    finally:
        db.close()
        engine.dispose()


def test_replace_events_for_message_rejects_duplicate_phase_sequence() -> None:
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        with pytest.raises(ValueError, match="duplicate phase_sequence"):
            service.replace_events_for_message(
                task_id=message.task_id,
                conversation_id=message.conversation_id,
                chat_message_id=message.id,
                turn_number=message.turn_number or 0,
                tool_calls=[{"tool_call_id": "tc-1", "phase_sequence": 1}],
                observation_sections=[{"content": "dup", "phase_sequence": 1}],
            )
    finally:
        db.close()
        engine.dispose()


def test_merge_events_for_message_appends_resume_segment_without_erasing_prior_rows() -> None:
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        first_created = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            tool_calls=[
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "scan_a",
                    "tool_result": "tool-a",
                    "phase_sequence": 0,
                    "turn_index": 0,
                }
            ],
            observation_sections=[
                {
                    "content": "obs-a",
                    "phase_sequence": 1,
                    "sub_turn_index": 0,
                }
            ],
        )
        assert len(first_created) == 2
        db.commit()

        second_created = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            tool_calls=[
                {
                    "tool_call_id": "tc-2",
                    "tool_name": "scan_b",
                    "tool_result": "tool-b",
                    "phase_sequence": 0,
                    "turn_index": 1,
                }
            ],
            observation_sections=[
                {
                    "content": "obs-b",
                    "phase_sequence": 1,
                    "sub_turn_index": 1,
                }
            ],
        )
        assert len(second_created) == 2
        db.commit()

        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()

        assert [row.kind for row in rows] == ["tool", "observation", "tool", "observation"]
        assert [row.content for row in rows] == ["tool-a", "obs-a", "tool-b", "obs-b"]
        assert [row.phase_sequence for row in rows] == [0, 1, 2, 3]
    finally:
        db.close()
        engine.dispose()


def test_merge_events_for_message_is_idempotent_for_duplicate_payload() -> None:
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        created = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            tool_calls=[
                {
                    "tool_call_id": "tc-dup",
                    "tool_name": "scan_dup",
                    "tool_result": "tool-dup",
                    "phase_sequence": 0,
                    "turn_index": 0,
                }
            ],
            observation_sections=[
                {
                    "content": "obs-dup",
                    "phase_sequence": 1,
                    "sub_turn_index": 0,
                }
            ],
        )
        assert len(created) == 2
        db.commit()

        created_again = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            tool_calls=[
                {
                    "tool_call_id": "tc-dup",
                    "tool_name": "scan_dup",
                    "tool_result": "tool-dup",
                    "phase_sequence": 0,
                    "turn_index": 0,
                }
            ],
            observation_sections=[
                {
                    "content": "obs-dup",
                    "phase_sequence": 1,
                    "sub_turn_index": 0,
                }
            ],
        )
        assert created_again == []
        db.commit()

        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()
        assert len(rows) == 2
        assert [row.kind for row in rows] == ["tool", "observation"]
    finally:
        db.close()
        engine.dispose()


# --- Reasoning row persistence tests ---


def test_replace_events_persists_reasoning_rows_with_correct_kind() -> None:
    """Reasoning sections are persisted as kind='reasoning' rows."""
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        created = service.replace_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=[
                {
                    "content": "Analyzing the request.",
                    "phase_sequence": 0,
                    "section_name": "intent",
                    "sub_turn_index": 0,
                    "started_at": 100.0,
                    "ended_at": 108.0,
                },
                {
                    "content": "Building execution plan.",
                    "phase_sequence": 1,
                    "section_name": "planner",
                    "sub_turn_index": 1,
                    "started_at": 108.0,
                    "ended_at": 115.5,
                },
            ],
        )
        db.commit()

        assert len(created) == 2
        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()

        assert [row.kind for row in rows] == ["reasoning", "reasoning"]
        assert [row.phase_sequence for row in rows] == [0, 1]
        assert [row.content for row in rows] == [
            "Analyzing the request.",
            "Building execution plan.",
        ]
        assert rows[0].event_metadata["section_name"] == "intent"
        assert rows[1].event_metadata["section_name"] == "planner"
        assert rows[0].event_metadata["started_at"] == 100.0
        assert rows[0].event_metadata["ended_at"] == 108.0
        assert rows[1].event_metadata["started_at"] == 108.0
        assert rows[1].event_metadata["ended_at"] == 115.5
    finally:
        db.close()
        engine.dispose()


def test_replace_events_interleaves_reasoning_tool_observation_by_phase_sequence() -> None:
    """Reasoning, tool, and observation rows are ordered by phase_sequence."""
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        service.replace_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=[
                {"content": "think-1", "phase_sequence": 0, "sub_turn_index": 0},
            ],
            tool_calls=[
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "nmap",
                    "tool_result": "scan-result",
                    "phase_sequence": 1,
                    "turn_index": 0,
                },
            ],
            observation_sections=[
                {"content": "obs-1", "phase_sequence": 2, "sub_turn_index": 0},
            ],
        )
        db.commit()

        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()

        assert [row.kind for row in rows] == ["reasoning", "tool", "observation"]
        assert [row.phase_sequence for row in rows] == [0, 1, 2]
    finally:
        db.close()
        engine.dispose()


def test_merge_reasoning_rows_do_not_collide_with_observation_rows() -> None:
    """Reasoning and observation rows with same sub_turn_index/content do not collide."""
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=[
                {"content": "same text", "phase_sequence": 0, "sub_turn_index": 0},
            ],
        )
        db.commit()

        # Now merge an observation with identical content and sub_turn_index
        created = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            observation_sections=[
                {"content": "same text", "phase_sequence": 0, "sub_turn_index": 0},
            ],
        )
        db.commit()

        # The observation should NOT be deduped against the reasoning row
        assert len(created) == 1

        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()

        assert len(rows) == 2
        kinds = {row.kind for row in rows}
        assert kinds == {"reasoning", "observation"}
    finally:
        db.close()
        engine.dispose()


def test_merge_reasoning_rows_are_idempotent() -> None:
    """Repeating the same reasoning payload does not duplicate rows."""
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        reasoning_payload = [
            {"content": "Thinking deeply.", "phase_sequence": 0, "sub_turn_index": 0},
        ]

        first = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=reasoning_payload,
        )
        db.commit()
        assert len(first) == 1

        second = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=reasoning_payload,
        )
        db.commit()
        assert second == []

        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
        ).scalars().all()
        assert len(rows) == 1
    finally:
        db.close()
        engine.dispose()


def test_merge_reasoning_rows_keep_distinct_sections_with_same_text() -> None:
    """Distinct reasoning sections must not collapse when text matches."""
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        created = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=[
                {
                    "content": "same reasoning text",
                    "phase_sequence": 0,
                    "sub_turn_index": 0,
                    "section_name": "intent",
                },
                {
                    "content": "same reasoning text",
                    "phase_sequence": 1,
                    "sub_turn_index": 0,
                    "section_name": "planner",
                },
            ],
        )
        db.commit()

        assert len(created) == 2
        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()

        assert len(rows) == 2
        assert [row.kind for row in rows] == ["reasoning", "reasoning"]
        assert [row.event_metadata["section_name"] for row in rows] == ["intent", "planner"]
    finally:
        db.close()
        engine.dispose()


def test_merge_reasoning_rows_keep_distinct_sources_with_same_text() -> None:
    """Reasoning rows remain distinct when only the source metadata differs."""
    engine, db = _build_session()
    try:
        message = _seed_assistant_message(db)
        service = ChatTurnEventService(db)

        created = service.merge_events_for_message(
            task_id=message.task_id,
            conversation_id=message.conversation_id,
            chat_message_id=message.id,
            turn_number=message.turn_number or 0,
            reasoning_sections=[
                {
                    "content": "same reasoning text",
                    "phase_sequence": 0,
                    "sub_turn_index": 0,
                    "section_name": "reasoning",
                    "source": "planner",
                },
                {
                    "content": "same reasoning text",
                    "phase_sequence": 1,
                    "sub_turn_index": 0,
                    "section_name": "reasoning",
                    "source": "executor",
                },
            ],
        )
        db.commit()

        assert len(created) == 2
        rows = db.execute(
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == message.id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        ).scalars().all()

        assert len(rows) == 2
        assert [row.event_metadata["source"] for row in rows] == ["planner", "executor"]
    finally:
        db.close()
        engine.dispose()
