"""Tests for tenant-scoped chat transcript retention executor behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time_utils import utc_now
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import ChatMessage, ChatTurnEvent, ToolCall
from backend.models.core import Engagement, Task, User
from backend.models.llm import LLMConversation
from backend.models.tenant import Tenant
from backend.services.chat.retention_service import (
    ACTIVE_TASK_TRANSCRIPT_RETENTION_PROTECTED,
    ChatTranscriptRetentionExecutor,
    RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED,
    TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    chat_transcript_retention_days_after_terminal: int = 30
    retention_batch_size_per_tenant: int = 100


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def test_chat_transcript_retention_dry_run_is_tenant_scoped_and_preserves_continuity() -> None:
    db = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="dry-run")
        other_tenant, other_user, other_engagement = _seed_scope(db, label="other")
        old_terminal = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        recent_terminal = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=5,
        )
        active_task = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.RUNNING.value,
            age_days=45,
        )
        foreign_task = _seed_task(
            db,
            tenant=other_tenant,
            user=other_user,
            engagement=other_engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        for task in (old_terminal, recent_terminal, active_task, foreign_task):
            _seed_transcript(db, task=task, user=user if task.tenant_id == tenant.id else other_user)

        result = ChatTranscriptRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.retention_class == RETENTION_CLASS_TASK_TRANSCRIPT
        assert result.counts.candidate_count == 1
        assert result.counts.protected_count == 2
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            ACTIVE_TASK_TRANSCRIPT_RETENTION_PROTECTED: 1,
            RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED: 1,
            TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED: 1,
        }
        assert {(decision.outcome, decision.resource_id) for decision in result.decisions} == {
            (RETENTION_DECISION_CANDIDATE, f"task:{old_terminal.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{recent_terminal.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{active_task.id}"),
        }
        assert _count_transcript_rows(db, task_id=old_terminal.id) == 5
        assert _count_transcript_rows(db, task_id=recent_terminal.id) == 5
        assert _count_transcript_rows(db, task_id=active_task.id) == 5
        assert _count_transcript_rows(db, task_id=foreign_task.id) == 5
    finally:
        db.close()


def test_chat_transcript_retention_apply_is_batched_and_idempotent() -> None:
    db = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="apply")
        other_tenant, other_user, other_engagement = _seed_scope(db, label="apply-other")
        oldest_terminal = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=60,
        )
        next_terminal = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.FAILED.value,
            age_days=45,
        )
        recent_terminal = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=5,
        )
        foreign_terminal = _seed_task(
            db,
            tenant=other_tenant,
            user=other_user,
            engagement=other_engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=60,
        )
        for task in (oldest_terminal, next_terminal, recent_terminal):
            _seed_transcript(db, task=task, user=user)
        _seed_transcript(db, task=foreign_terminal, user=other_user)
        executor = ChatTranscriptRetentionExecutor(db)
        policy = _Policy(retention_batch_size_per_tenant=1)

        first = executor.run(
            policy=policy,
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert first.counts.candidate_count == 1
        assert first.counts.applied_count == 1
        assert first.reason_counts == {
            RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED: 1,
            TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED: 1,
        }
        assert {(decision.outcome, decision.resource_id) for decision in first.decisions} == {
            (RETENTION_DECISION_APPLIED, f"task:{oldest_terminal.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{recent_terminal.id}"),
        }
        assert _task_exists(db, oldest_terminal.id)
        assert _count_transcript_rows(db, task_id=oldest_terminal.id) == 0
        assert _count_transcript_rows(db, task_id=next_terminal.id) == 5
        assert _count_transcript_rows(db, task_id=recent_terminal.id) == 5
        assert _count_transcript_rows(db, task_id=foreign_terminal.id) == 5

        second = executor.run(
            policy=policy,
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert second.counts.candidate_count == 1
        assert second.counts.applied_count == 1
        assert second.reason_counts == {
            RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED: 1,
            TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED: 1,
        }
        assert {(decision.outcome, decision.resource_id) for decision in second.decisions} == {
            (RETENTION_DECISION_APPLIED, f"task:{next_terminal.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{recent_terminal.id}"),
        }
        assert _count_transcript_rows(db, task_id=next_terminal.id) == 0
        assert _count_transcript_rows(db, task_id=recent_terminal.id) == 5
        assert _count_transcript_rows(db, task_id=foreign_terminal.id) == 5

        third = executor.run(
            policy=policy,
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert third.counts.candidate_count == 0
        assert third.counts.applied_count == 0
        assert third.reason_counts == {
            RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED: 1,
        }
    finally:
        db.close()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(slug=f"tenant-{label}-{uuid_lib.uuid4().hex[:8]}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid_lib.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()
    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"engagement-{label}",
    )
    db.add(engagement)
    db.flush()
    return tenant, user, engagement


def _seed_task(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    engagement: Engagement,
    status: str,
    age_days: int,
) -> Task:
    timestamp = utc_now() - timedelta(days=age_days)
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"task-{uuid_lib.uuid4().hex[:8]}",
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
        completed_at=timestamp if status in TaskStatus.get_terminal_statuses() else None,
        stopped_at=timestamp if status == TaskStatus.STOPPED.value else None,
    )
    db.add(task)
    db.flush()
    return task


def _seed_transcript(db: Session, *, task: Task, user: User) -> None:
    first_message = ChatMessage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        conversation_id=f"conversation-{task.id}",
        turn_number=1,
        parent_message_id=None,
        latest_child_message_id=None,
        message_type="user",
        message="hello",
    )
    db.add(first_message)
    db.flush()
    second_message = ChatMessage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        conversation_id=f"conversation-{task.id}",
        turn_number=2,
        parent_message_id=first_message.id,
        latest_child_message_id=None,
        message_type="assistant",
        message="world",
    )
    db.add(second_message)
    db.flush()
    first_message.latest_child_message_id = second_message.id
    db.add(
        ToolCall(
            tenant_id=task.tenant_id,
            chat_message_id=second_message.id,
            tool_call_id=f"tool-{task.id}",
            tool_name="test_tool",
            tool_arguments={},
            tool_result="ok",
            turn_index=0,
        )
    )
    db.add(
        ChatTurnEvent(
            tenant_id=task.tenant_id,
            task_id=task.id,
            conversation_id=f"conversation-{task.id}",
            chat_message_id=second_message.id,
            turn_number=2,
            phase_sequence=0,
            kind="tool",
            content="safe test content",
        )
    )
    db.add(
        LLMConversation(
            tenant_id=task.tenant_id,
            task_id=task.id,
            user_id=user.id,
            provider="openai",
            model="gpt-5.2",
            conversation_id=f"remote-{task.id}",
            status="active",
            is_active=True,
        )
    )
    db.flush()


def _count_transcript_rows(db: Session, *, task_id: int) -> int:
    message_ids = [row[0] for row in db.query(ChatMessage.id).filter(ChatMessage.task_id == task_id).all()]
    tool_count = (
        db.query(ToolCall).filter(ToolCall.chat_message_id.in_(message_ids)).count()
        if message_ids
        else 0
    )
    return (
        db.query(ChatMessage).filter(ChatMessage.task_id == task_id).count()
        + db.query(ChatTurnEvent).filter(ChatTurnEvent.task_id == task_id).count()
        + tool_count
        + db.query(LLMConversation).filter(LLMConversation.task_id == task_id).count()
    )


def _task_exists(db: Session, task_id: int) -> bool:
    return db.query(Task.id).filter(Task.id == int(task_id)).first() is not None
