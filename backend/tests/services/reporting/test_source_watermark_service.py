"""Tests for deterministic task-local source watermark computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeIngestionRun,
    KnowledgeObservation,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant
from backend.services.reporting.source_watermark_service import SourceWatermarkService


def _build_session() -> Session:
    assert backend_models.__all__
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement, Task]:
    tenant = Tenant(slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(tenant_id=tenant.id, user_id=user.id, name=f"Engagement {label}")
    db.add(engagement)
    db.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
    )
    db.add(task)
    db.flush()
    return tenant, user, engagement, task


def _add_chat_message(
    db: Session,
    *,
    task: Task,
    turn_number: int,
    created_at: datetime,
) -> ChatMessage:
    message = ChatMessage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        conversation_id=f"conversation-{task.id}",
        turn_number=turn_number,
        message_type="assistant",
        message=f"message-{turn_number}",
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(message)
    db.flush()
    return message


def _add_chat_turn_event(
    db: Session,
    *,
    message: ChatMessage,
    turn_number: int,
    phase_sequence: int,
    created_at: datetime,
) -> ChatTurnEvent:
    event = ChatTurnEvent(
        tenant_id=message.tenant_id,
        task_id=message.task_id,
        conversation_id=message.conversation_id,
        chat_message_id=message.id,
        turn_number=turn_number,
        phase_sequence=phase_sequence,
        kind="tool",
        content="event",
        created_at=created_at,
    )
    db.add(event)
    db.flush()
    return event


def _add_tool_execution(
    db: Session,
    *,
    task: Task,
    created_at: datetime,
    finished_at: datetime | None,
) -> ToolExecution:
    execution = ToolExecution(
        tenant_id=task.tenant_id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo test"},
        agent_path="langgraph",
        status="succeeded",
        started_at=created_at,
        finished_at=finished_at,
        created_at=created_at,
    )
    db.add(execution)
    db.flush()
    return execution


def _add_artifact(
    db: Session,
    *,
    execution: ToolExecution,
    created_at: datetime,
) -> ExecutionArtifact:
    artifact = ExecutionArtifact(
        tenant_id=execution.tenant_id,
        task_id=execution.task_id,
        execution_id=execution.id,
        artifact_kind="stdout",
        content_text="artifact",
        created_at=created_at,
    )
    db.add(artifact)
    db.flush()
    return artifact


def _add_knowledge_rows(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    observed_at: datetime,
    with_task_lineage: bool = True,
) -> None:
    source_execution_id = uuid.uuid4()
    task_id = task.id if with_task_lineage else None
    run = KnowledgeIngestionRun(
        tenant_id=task.tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        source_execution_id=source_execution_id,
        extractor_family="test",
        extractor_version="1",
        status="succeeded",
    )
    db.add(run)
    db.flush()

    db.add_all(
        [
            KnowledgeEvidenceArchive(
                tenant_id=task.tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                source_execution_id=source_execution_id,
                storage_mode="inline",
                inline_excerpt="evidence",
                lineage_snapshot={"task_id": task_id},
                created_at=observed_at + timedelta(minutes=1),
            ),
            KnowledgeObservation(
                tenant_id=task.tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                ingestion_run_id=run.id,
                source_execution_id=source_execution_id,
                observation_type="finding",
                subject_type="host",
                subject_key=f"host-{uuid.uuid4().hex[:8]}",
                assertion_level="confirmed",
                dedupe_key=uuid.uuid4().hex,
                payload={"value": "observed"},
                observed_at=observed_at + timedelta(minutes=2),
            ),
            KnowledgeEntityProvenance(
                tenant_id=task.tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                entity_type="finding",
                entity_id=uuid.uuid4(),
                execution_id=source_execution_id,
                observed_at=observed_at + timedelta(minutes=3),
            ),
        ]
    )
    db.flush()


def test_empty_task_source_set_returns_stable_json_watermark() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="empty")
    db.commit()

    service = SourceWatermarkService(db)
    first = service.compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    second = service.compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )

    assert first == second
    assert first["empty"] is True
    assert json.loads(json.dumps(first, sort_keys=True)) == first
    for marker in first["sources"].values():
        assert set(marker.values()) == {None}


def test_watermark_uses_latest_durable_task_local_source_rows_only() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="owner")
    _other_tenant, other_user, other_engagement, other_task = _seed_scope(db, label="other")
    base_time = datetime(2026, 1, 1, tzinfo=UTC)

    _add_chat_message(db, task=task, turn_number=1, created_at=base_time)
    _add_chat_message(db, task=task, turn_number=5, created_at=base_time + timedelta(minutes=1))
    latest_event_message = _add_chat_message(db, task=task, turn_number=6, created_at=base_time + timedelta(minutes=2))
    _add_chat_turn_event(
        db,
        message=latest_event_message,
        turn_number=6,
        phase_sequence=2,
        created_at=base_time + timedelta(minutes=3),
    )
    _add_chat_turn_event(
        db,
        message=latest_event_message,
        turn_number=6,
        phase_sequence=7,
        created_at=base_time + timedelta(minutes=4),
    )
    _add_tool_execution(db, task=task, created_at=base_time, finished_at=base_time + timedelta(minutes=2))
    latest_execution = _add_tool_execution(
        db,
        task=task,
        created_at=base_time + timedelta(minutes=3),
        finished_at=base_time + timedelta(minutes=5),
    )
    _add_artifact(db, execution=latest_execution, created_at=base_time + timedelta(minutes=6))
    _add_knowledge_rows(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        observed_at=base_time + timedelta(minutes=7),
    )

    other_message = _add_chat_message(
        db,
        task=other_task,
        turn_number=99,
        created_at=base_time + timedelta(days=1),
    )
    _add_chat_turn_event(
        db,
        message=other_message,
        turn_number=99,
        phase_sequence=99,
        created_at=base_time + timedelta(days=1),
    )
    other_execution = _add_tool_execution(
        db,
        task=other_task,
        created_at=base_time + timedelta(days=1),
        finished_at=base_time + timedelta(days=1, minutes=1),
    )
    _add_artifact(db, execution=other_execution, created_at=base_time + timedelta(days=1, minutes=2))
    _add_knowledge_rows(
        db,
        task=other_task,
        user_id=other_user.id,
        engagement_id=other_engagement.id,
        observed_at=base_time + timedelta(days=1),
    )
    _add_knowledge_rows(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        observed_at=base_time + timedelta(days=2),
        with_task_lineage=False,
    )
    db.commit()

    watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    sources = watermark["sources"]

    assert watermark["empty"] is False
    assert sources["chat_messages"] == {
        "latest_id": latest_event_message.id,
        "latest_turn_number": 6,
    }
    assert sources["chat_turn_events"] == {
        "latest_turn_number": 6,
        "latest_phase_sequence": 7,
    }
    assert sources["tool_executions"]["latest_id"] == str(latest_execution.id)
    assert sources["tool_executions"]["latest_created_at"].startswith("2026-01-01T00:03:00")
    assert sources["tool_executions"]["latest_finished_at"].startswith("2026-01-01T00:05:00")
    assert sources["execution_artifacts"]["latest_created_at"].startswith("2026-01-01T00:06:00")
    assert sources["knowledge_evidence_archives"]["latest_created_at"].startswith("2026-01-01T00:08:00")
    assert sources["knowledge_observations"]["latest_observed_at"].startswith("2026-01-01T00:09:00")
    assert sources["knowledge_entity_provenance"]["latest_observed_at"].startswith("2026-01-01T00:10:00")
    assert json.loads(json.dumps(watermark, sort_keys=True)) == watermark


def test_direct_user_and_engagement_filters_apply_when_source_rows_have_columns() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="direct-scope")
    other_user = User(username=f"user-cross-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add(other_user)
    db.flush()
    base_time = datetime(2026, 1, 2, tzinfo=UTC)

    _add_knowledge_rows(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        observed_at=base_time,
    )
    _add_knowledge_rows(
        db,
        task=task,
        user_id=other_user.id,
        engagement_id=engagement.id,
        observed_at=base_time + timedelta(days=1),
    )
    db.commit()

    sources = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )["sources"]

    assert sources["knowledge_evidence_archives"]["latest_created_at"].startswith("2026-01-02T00:01:00")
    assert sources["knowledge_observations"]["latest_observed_at"].startswith("2026-01-02T00:02:00")
    assert sources["knowledge_entity_provenance"]["latest_observed_at"].startswith("2026-01-02T00:03:00")
