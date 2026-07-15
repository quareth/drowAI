"""Tests for task-local transcript context packet construction."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Engagement, Task, User
from backend.models.tenant import Tenant
from backend.services.reporting.transcript_context_builder import (
    TranscriptContextBuilder,
)


TRANSCRIPT_CONTEXT_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    ChatMessage.__table__,
    ChatTurnEvent.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=TRANSCRIPT_CONTEXT_TABLES)
    return engine, sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


def _seed_scope(session, *, label: str):
    tenant = Tenant(slug=f"tenant-{label}-{uuid.uuid4().hex}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid.uuid4().hex}", password="hashed-password")
    session.add_all([tenant, user])
    session.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
    )
    session.add(engagement)
    session.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def _add_message(
    session,
    *,
    tenant_id: int,
    task_id: int,
    conversation_id: str,
    turn_number: int,
    message_type: str,
    message: str,
    created_at: datetime | None = None,
    parent_message_id: int | None = None,
) -> ChatMessage:
    row = ChatMessage(
        tenant_id=tenant_id,
        task_id=task_id,
        conversation_id=conversation_id,
        turn_number=turn_number,
        message_type=message_type,
        message=message,
        created_at=created_at,
        parent_message_id=parent_message_id,
    )
    session.add(row)
    session.flush()
    return row


def _add_event(
    session,
    *,
    tenant_id: int,
    task_id: int,
    conversation_id: str,
    chat_message_id: int,
    turn_number: int,
    phase_sequence: int,
    kind: str,
    content: str | None,
) -> ChatTurnEvent:
    row = ChatTurnEvent(
        tenant_id=tenant_id,
        task_id=task_id,
        conversation_id=conversation_id,
        chat_message_id=chat_message_id,
        turn_number=turn_number,
        phase_sequence=phase_sequence,
        kind=kind,
        content=content,
        created_at=datetime(2026, 6, 9, 10, phase_sequence, tzinfo=timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def test_transcript_context_orders_messages_and_detail_events_by_turn_phase() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="ordering")
        conversation_id = "conv-ordering"
        first = _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=1,
            message_type="user",
            message="Run a web scan",
        )
        second = _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=2,
            message_type="assistant",
            message="The scan completed",
        )
        _add_event(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            chat_message_id=second.id,
            turn_number=2,
            phase_sequence=2,
            kind="observation",
            content="Found HTTP service",
        )
        _add_event(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            chat_message_id=second.id,
            turn_number=2,
            phase_sequence=1,
            kind="tool",
            content="nmap completed",
        )

        context = TranscriptContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert context.conversation_id == conversation_id
        assert [(item.source, item.text) for item in context.items] == [
            ("message", first.message),
            ("message", second.message),
            ("detail_event", "nmap completed"),
            ("detail_event", "Found HTTP service"),
        ]
        assert [
            item.phase_sequence
            for item in context.items
            if item.source == "detail_event"
        ] == [1, 2]
        assert context.message_count == 2
        assert context.detail_event_count == 2
        assert context.truncated is False
    finally:
        engine.dispose()


def test_transcript_context_is_deterministic_under_repeated_calls() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="deterministic")
        conversation_id = "conv-deterministic"
        message = _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=1,
            message_type="assistant",
            message="Observed HTTPS service on the target.",
        )
        _add_event(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            chat_message_id=message.id,
            turn_number=1,
            phase_sequence=1,
            kind="tool",
            content="nmap reported tcp/443 open",
        )

        builder = TranscriptContextBuilder(session)
        first_context = builder.build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        second_context = builder.build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert first_context == second_context
    finally:
        engine.dispose()


def test_transcript_context_enforces_message_and_character_bounds() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="bounds")
        conversation_id = "conv-bounds"
        _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=1,
            message_type="user",
            message="older message should be outside max message window",
        )
        _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=2,
            message_type="assistant",
            message="second message with extra words",
        )
        _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=3,
            message_type="assistant",
            message="third message with extra words",
        )

        context = TranscriptContextBuilder(
            session,
            max_messages=2,
            max_characters=40,
            max_item_characters=30,
        ).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert context.message_count <= 2
        assert context.total_characters <= 40
        assert context.truncated is True
        assert "older message" not in " ".join(item.text for item in context.items)
    finally:
        engine.dispose()


def test_transcript_context_returns_empty_context_when_task_has_no_transcript() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="empty")

        context = TranscriptContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert context.task_id == task.id
        assert context.conversation_id is None
        assert context.items == ()
        assert context.total_characters == 0
        assert context.truncated is False
    finally:
        engine.dispose()


def test_transcript_context_excludes_rows_from_other_task_scope() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="target")
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session, label="other"
        )
        _add_message(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            conversation_id="target-conv",
            turn_number=1,
            message_type="user",
            message="target task text",
        )
        _add_message(
            session,
            tenant_id=other_tenant.id,
            task_id=other_task.id,
            conversation_id="other-conv",
            turn_number=10,
            message_type="assistant",
            message="other task text",
        )

        context = TranscriptContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        wrong_scope_context = TranscriptContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=task.id,
        )

        assert [item.text for item in context.items] == ["target task text"]
        assert wrong_scope_context.items == ()
    finally:
        engine.dispose()
