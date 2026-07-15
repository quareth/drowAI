"""Tests for task-local memo preparation context composition."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import MappingProxyType

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import AgentLog, ChatMessage, ChatTurnEvent
from backend.models.core import Engagement, Task, TaskHistory, User
from backend.models.knowledge import (
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.reporting import TaskClosureMemo
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerControlMessage,
    RuntimeJob,
)
from backend.models.streaming import StreamEvent, SystemLog
from backend.models.tenant import Tenant
from backend.services.reporting.task_memo_context_builder import (
    TaskMemoContextBuilder,
)


TASK_MEMO_CONTEXT_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskHistory.__table__,
    ChatMessage.__table__,
    ChatTurnEvent.__table__,
    AgentLog.__table__,
    SystemLog.__table__,
    StreamEvent.__table__,
    ExecutionSite.__table__,
    Runner.__table__,
    RuntimeJob.__table__,
    RunnerControlMessage.__table__,
    ToolExecution.__table__,
    ExecutionArtifact.__table__,
    KnowledgeEvidenceArchive.__table__,
    KnowledgeObservation.__table__,
    KnowledgeAsset.__table__,
    KnowledgeService.__table__,
    KnowledgeFinding.__table__,
    KnowledgeRelationship.__table__,
    KnowledgeWebPath.__table__,
    KnowledgeEntityProvenance.__table__,
    TaskClosureMemo.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=TASK_MEMO_CONTEXT_TABLES)
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
        description="Task description",
        scope="Task scope",
        status=TaskStatus.STOPPED.value,
        stopped_at=datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc),
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def _add_transcript_message(session, *, tenant_id: int, task_id: int) -> ChatMessage:
    message = ChatMessage(
        tenant_id=tenant_id,
        task_id=task_id,
        conversation_id=f"conv-{task_id}",
        turn_number=1,
        message_type="assistant",
        message="Completed the task and recorded notes.",
    )
    session.add(message)
    session.flush()
    return message


def _add_useful_runtime_signal(
    session,
    *,
    tenant_id: int,
    user_id: int,
    task_id: int,
) -> TaskHistory:
    row = TaskHistory(
        tenant_id=tenant_id,
        task_id=task_id,
        user_id=user_id,
        old_status=TaskStatus.STARTING.value,
        new_status=TaskStatus.RUNNING.value,
        transition_reason="runtime started",
        timestamp=datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _add_evidence(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
) -> KnowledgeEvidenceArchive:
    evidence = KnowledgeEvidenceArchive(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        source_execution_id=uuid.uuid4(),
        storage_mode="inline_excerpt",
        inline_excerpt="Open TCP 443 was observed.",
        lineage_snapshot={"target": "10.0.0.5:443"},
        archive_metadata={"type": "service"},
        created_at=datetime(2026, 6, 9, 11, 0, tzinfo=timezone.utc),
    )
    session.add(evidence)
    session.flush()
    return evidence


def _add_current_memo(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    version: int = 1,
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        version=version,
        is_current=True,
        status="ready",
        memo_mode="supported",
        source_watermark={"schema_version": 1},
        memo={
            "task_name": "Previous task",
            "summary": "Previous memo summary",
            "actions_performed": [{"text": "Previous action", "source": "transcript"}],
            "reportable_observations": [],
            "possible_findings": [],
            "limitations": [],
            "unsupported_notes": [],
            "evidence_refs": [],
            "knowledge_refs": [],
        },
        generated_at=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
    )
    session.add(memo)
    session.flush()
    return memo


def test_supported_context_includes_packets_and_allowed_ref_sets() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="supported")
        _add_transcript_message(session, tenant_id=tenant.id, task_id=task.id)
        evidence = _add_evidence(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        session.commit()

        context = TaskMemoContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert context.memo_mode == "supported"
        assert context.is_preparable is True
        assert context.not_preparable_reason is None
        assert context.task.task_id == task.id
        assert context.task.name == task.name
        assert context.transcript.message_count == 1
        assert context.evidence.item_count == 1
        assert context.allowed_evidence_refs == {f"evidence_archive:{evidence.id}"}
        assert context.allowed_knowledge_refs == frozenset()
        assert context.has_reportable_source_refs is True
        assert context.source_watermark["empty"] is False
        assert isinstance(context.source_watermark, MappingProxyType)
        assert context.previous_memo is None
    finally:
        session.close()
        engine.dispose()


def test_limited_context_requires_useful_runtime_without_reportable_refs() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="limited")
        _add_transcript_message(session, tenant_id=tenant.id, task_id=task.id)
        _add_useful_runtime_signal(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
        )
        session.commit()

        context = TaskMemoContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert context.memo_mode == "limited"
        assert context.is_preparable is True
        assert context.not_preparable_reason is None
        assert context.runtime_readiness.useful_runtime_execution is True
        assert context.allowed_evidence_refs == frozenset()
        assert context.allowed_knowledge_refs == frozenset()
        assert context.has_reportable_source_refs is False
        assert context.evidence.items == ()
        assert context.knowledge.items == ()
        assert context.transcript.message_count == 1
    finally:
        session.close()
        engine.dispose()


def test_previous_memo_is_included_only_for_scoped_regeneration() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="previous")
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session,
            label="other-previous",
        )
        _add_evidence(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        current = _add_current_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        _add_current_memo(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
        )
        session.commit()

        first_context = TaskMemoContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            regenerate=False,
        )
        regenerate_context = TaskMemoContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            regenerate=True,
        )

        assert first_context.previous_memo is None
        assert regenerate_context.previous_memo is not None
        assert regenerate_context.previous_memo.memo_id == str(current.id)
        assert regenerate_context.previous_memo.version == 1
        assert regenerate_context.previous_memo.summary == "Previous memo summary"
        assert regenerate_context.previous_memo.body["task_name"] == "Previous task"
    finally:
        session.close()
        engine.dispose()


def test_empty_context_without_useful_runtime_is_not_preparable() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="empty")
        session.commit()

        context = TaskMemoContextBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert context.memo_mode is None
        assert context.is_preparable is False
        assert context.not_preparable_reason == "no_useful_runtime_execution"
        assert context.allowed_evidence_refs == frozenset()
        assert context.allowed_knowledge_refs == frozenset()
        assert context.transcript.items == ()
        assert context.evidence.items == ()
        assert context.knowledge.items == ()
    finally:
        session.close()
        engine.dispose()
