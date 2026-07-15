"""Tests for task-local reporting input inventory source helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Engagement, Task, TaskHistory, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.reporting import TaskClosureMemo
from backend.models.tenant import Tenant
from backend.services.reporting.contracts import (
    REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    REASON_TASK_NOT_STOPPED,
)
from backend.services.reporting.input_inventory_service import (
    InputInventoryService,
    ReportingInputInventoryNotFoundError,
)
from backend.services.reporting.source_watermark_service import SourceWatermarkService


def _build_session() -> Session:
    assert backend_models.__all__
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _seed_scope(
    db: Session,
    *,
    label: str,
    tenant_status: str = "active",
    user_active: bool = True,
    task_status: str = TaskStatus.CREATED.value,
    runtime_placement_mode: str = "local",
) -> tuple[Tenant, User, Engagement, Task]:
    tenant = Tenant(
        slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}",
        name=f"Tenant {label}",
        status=tenant_status,
    )
    user = User(
        username=f"user-{label}-{uuid.uuid4().hex[:8]}",
        password="hashed",
        is_active=user_active,
    )
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
        status=task_status,
        runtime_placement_mode=runtime_placement_mode,
    )
    db.add(task)
    db.flush()
    return tenant, user, engagement, task


def _add_ingestion_run(db: Session, *, task: Task, task_lineage: bool = True) -> KnowledgeIngestionRun:
    run = KnowledgeIngestionRun(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        engagement_id=task.engagement_id,
        task_id=task.id if task_lineage else None,
        source_execution_id=uuid.uuid4(),
        extractor_family="test",
        extractor_version="1",
        status="succeeded",
    )
    db.add(run)
    db.flush()
    return run


def _add_evidence(db: Session, *, task: Task, task_lineage: bool = True) -> KnowledgeEvidenceArchive:
    evidence = KnowledgeEvidenceArchive(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        engagement_id=task.engagement_id,
        task_id=task.id if task_lineage else None,
        source_execution_id=uuid.uuid4(),
        storage_mode="inline",
        inline_excerpt="evidence",
        lineage_snapshot={"task_id": task.id if task_lineage else None},
    )
    db.add(evidence)
    db.flush()
    return evidence


def _add_finding(
    db: Session,
    *,
    task: Task,
    finding_key: str,
    assertion_level: str,
    status: str = "open",
    metadata: dict[str, object] | None = None,
    task_lineage: bool = True,
) -> KnowledgeFinding:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    finding = KnowledgeFinding(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        engagement_id=task.engagement_id,
        finding_key=finding_key,
        finding_type="vulnerability",
        subject_type="finding.vulnerability",
        subject_key=finding_key,
        title=finding_key,
        severity="medium",
        status=status,
        assertion_level=assertion_level,
        first_seen_at=now,
        last_seen_at=now,
        finding_metadata=metadata or {},
    )
    db.add(finding)
    db.flush()
    db.add(
        KnowledgeEntityProvenance(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            engagement_id=task.engagement_id,
            task_id=task.id if task_lineage else None,
            entity_type="finding",
            entity_id=finding.id,
            execution_id=uuid.uuid4(),
            observed_at=now,
        )
    )
    db.flush()
    return finding


def _add_observation(
    db: Session,
    *,
    task: Task,
    subject_key: str,
    assertion_level: str,
    task_lineage: bool = True,
) -> KnowledgeObservation:
    run = _add_ingestion_run(db, task=task, task_lineage=task_lineage)
    observation = KnowledgeObservation(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        engagement_id=task.engagement_id,
        task_id=task.id if task_lineage else None,
        ingestion_run_id=run.id,
        source_execution_id=run.source_execution_id,
        observation_type="finding.vulnerability_detected",
        subject_type="finding.vulnerability",
        subject_key=subject_key,
        assertion_level=assertion_level,
        dedupe_key=uuid.uuid4().hex,
        payload={"title": subject_key},
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(observation)
    db.flush()
    return observation


def _add_chat_message(db: Session, *, task: Task, turn_number: int, created_at: datetime) -> ChatMessage:
    message = ChatMessage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        conversation_id=f"conversation-{task.id}",
        turn_number=turn_number,
        message_type="assistant",
        message="message",
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(message)
    db.flush()
    return message


def _add_memo(
    db: Session,
    *,
    task: Task,
    version: int,
    status: str,
    is_current: bool,
    source_watermark: dict[str, object],
    error_message: str | None = None,
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        created_by_user_id=task.user_id,
        engagement_id=task.engagement_id,
        task_id=task.id,
        version=version,
        is_current=is_current,
        status=status,
        memo_mode="supported",
        source_watermark=source_watermark,
        memo={"summary": f"memo-{version}"},
        error_message=error_message,
    )
    db.add(memo)
    db.flush()
    return memo


def test_source_counts_use_active_owner_engagement_task_scope_and_task_lineage() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="owner")
    _other_tenant, _other_user, _other_engagement, other_task = _seed_scope(db, label="other")
    inactive_tenant, inactive_user, inactive_engagement, inactive_task = _seed_scope(
        db,
        label="inactive",
        tenant_status="disabled",
    )

    _add_evidence(db, task=task)
    _add_evidence(db, task=task, task_lineage=False)
    _add_evidence(db, task=other_task)
    _add_evidence(db, task=inactive_task)
    _add_finding(db, task=task, finding_key="finding:canonical", assertion_level="confirmed")
    _add_finding(
        db,
        task=task,
        finding_key="finding:candidate-metadata",
        assertion_level="observed",
        status="candidate",
        metadata={"authority": {"candidate_only": True, "source_kind": "llm_candidate"}},
    )
    _add_finding(
        db,
        task=task,
        finding_key="finding:no-task-lineage",
        assertion_level="confirmed",
        task_lineage=False,
    )
    _add_observation(
        db,
        task=task,
        subject_key="finding:candidate-observation",
        assertion_level="candidate",
    )
    _add_observation(
        db,
        task=task,
        subject_key="finding:candidate-no-task-lineage",
        assertion_level="candidate",
        task_lineage=False,
    )
    db.commit()

    service = InputInventoryService(db)
    counts = service._source_counts(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    inactive_counts = service._source_counts(
        tenant_id=inactive_tenant.id,
        user_id=inactive_user.id,
        engagement_id=inactive_engagement.id,
        task_id=inactive_task.id,
    )

    assert counts.evidence_count == 1
    assert counts.canonical_finding_count == 1
    assert counts.candidate_finding_count == 2
    assert counts.has_default_reportable_sources is True
    assert inactive_counts.evidence_count == 0
    assert inactive_counts.canonical_finding_count == 0
    assert inactive_counts.candidate_finding_count == 0


def test_source_counts_exclude_same_owner_finding_from_other_engagement() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="finding-engagement-scope",
        task_status=TaskStatus.STOPPED.value,
    )
    other_engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Other Finding Engagement",
    )
    db.add(other_engagement)
    db.flush()
    other_task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=other_engagement.id,
        name="Other Finding Task",
        status=TaskStatus.STOPPED.value,
    )
    db.add(other_task)
    db.flush()
    other_finding = _add_finding(
        db,
        task=other_task,
        finding_key="finding:other-engagement",
        assertion_level="confirmed",
    )
    db.add(
        KnowledgeEntityProvenance(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            entity_type="finding",
            entity_id=other_finding.id,
            execution_id=uuid.uuid4(),
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.commit()

    service = InputInventoryService(db)
    counts = service._source_counts(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    row = service._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert counts.canonical_finding_count == 0
    assert counts.candidate_finding_count == 0
    assert counts.has_default_reportable_sources is False
    assert row.counts.canonical_finding_count == 0
    assert row.is_reportable is False
    assert row.memo_mode is None
    assert row.not_preparable_reason == REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL


def test_source_counts_include_task_local_finding_with_null_engagement() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="null-finding-engagement")
    finding = _add_finding(
        db,
        task=task,
        finding_key="finding:null-engagement",
        assertion_level="confirmed",
    )
    finding.engagement_id = None
    db.commit()

    counts = InputInventoryService(db)._source_counts(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )

    assert counts.canonical_finding_count == 1
    assert counts.has_default_reportable_sources is True


def test_source_counts_deduplicate_duplicate_finding_provenance_rows() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="duplicate-finding-provenance")
    finding = _add_finding(
        db,
        task=task,
        finding_key="finding:duplicate-provenance",
        assertion_level="confirmed",
        metadata={"evidence_refs": ["evidence:1"]},
    )
    db.add(
        KnowledgeEntityProvenance(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            entity_type="finding",
            entity_id=finding.id,
            execution_id=uuid.uuid4(),
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.commit()

    counts = InputInventoryService(db)._source_counts(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )

    assert counts.canonical_finding_count == 1
    assert counts.candidate_finding_count == 0


def test_candidate_only_sources_are_counted_without_default_reportable_material() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="candidate-only")
    _add_finding(
        db,
        task=task,
        finding_key="finding:candidate-only",
        assertion_level="candidate",
        status="candidate",
        metadata={"authority": {"source_kind": "llm_candidate", "candidate_only": True}},
    )
    _add_observation(
        db,
        task=task,
        subject_key="finding:candidate-only",
        assertion_level="candidate",
    )
    db.commit()

    counts = InputInventoryService(db)._source_counts(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )

    assert counts.evidence_count == 0
    assert counts.canonical_finding_count == 0
    assert counts.candidate_finding_count == 1
    assert counts.has_default_reportable_sources is False


def test_useful_execution_markers_use_durable_task_local_rows() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="execution")
    _other_tenant, _other_user, _other_engagement, other_task = _seed_scope(db, label="other-execution")
    started_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

    db.add(
        TaskHistory(
            tenant_id=task.tenant_id,
            user_id=None,
            task_id=task.id,
            old_status=TaskStatus.STARTING.value,
            new_status=TaskStatus.RUNNING.value,
            change_source="system",
            timestamp=started_at,
        )
    )
    db.add(
        TaskHistory(
            tenant_id=other_task.tenant_id,
            user_id=other_task.user_id,
            task_id=other_task.id,
            old_status=TaskStatus.STARTING.value,
            new_status=TaskStatus.RUNNING.value,
            change_source="system",
            timestamp=started_at,
        )
    )
    execution = ToolExecution(
        tenant_id=task.tenant_id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo ok"},
        agent_path="langgraph",
        status="succeeded",
        started_at=started_at + timedelta(minutes=1),
        created_at=started_at + timedelta(minutes=1),
    )
    other_execution = ToolExecution(
        tenant_id=other_task.tenant_id,
        task_id=other_task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo ignored"},
        agent_path="langgraph",
        status="succeeded",
        started_at=started_at + timedelta(minutes=1),
        created_at=started_at + timedelta(minutes=1),
    )
    db.add_all([execution, other_execution])
    db.flush()
    db.add_all(
        [
            ExecutionArtifact(
                tenant_id=task.tenant_id,
                task_id=task.id,
                execution_id=execution.id,
                artifact_kind="stdout",
                content_text="ok",
                created_at=started_at + timedelta(minutes=2),
            ),
            ExecutionArtifact(
                tenant_id=other_task.tenant_id,
                task_id=other_task.id,
                execution_id=other_execution.id,
                artifact_kind="stdout",
                content_text="ignored",
                created_at=started_at + timedelta(minutes=2),
            ),
        ]
    )
    before_message = _add_chat_message(db, task=task, turn_number=1, created_at=started_at - timedelta(minutes=2))
    after_message = _add_chat_message(db, task=task, turn_number=2, created_at=started_at + timedelta(minutes=3))
    other_message = _add_chat_message(
        db,
        task=other_task,
        turn_number=1,
        created_at=started_at + timedelta(minutes=3),
    )
    db.add_all(
        [
            ChatTurnEvent(
                tenant_id=task.tenant_id,
                task_id=task.id,
                conversation_id=before_message.conversation_id,
                chat_message_id=before_message.id,
                turn_number=1,
                phase_sequence=1,
                kind="tool",
                created_at=started_at - timedelta(minutes=1),
            ),
            ChatTurnEvent(
                tenant_id=task.tenant_id,
                task_id=task.id,
                conversation_id=after_message.conversation_id,
                chat_message_id=after_message.id,
                turn_number=2,
                phase_sequence=1,
                kind="tool",
                event_metadata={"runtime_phase": "running"},
                created_at=started_at + timedelta(minutes=4),
            ),
            ChatTurnEvent(
                tenant_id=other_task.tenant_id,
                task_id=other_task.id,
                conversation_id=other_message.conversation_id,
                chat_message_id=other_message.id,
                turn_number=1,
                phase_sequence=1,
                kind="tool",
                event_metadata={"runtime_phase": "running"},
                created_at=started_at + timedelta(minutes=4),
            ),
        ]
    )
    db.commit()

    markers = InputInventoryService(db)._useful_execution_markers(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )

    assert markers.task_history_count == 1
    assert markers.tool_execution_count == 1
    assert markers.execution_artifact_count == 1
    assert markers.post_start_chat_turn_event_count == 1
    assert markers.has_useful_runtime_execution is True


def test_supported_projection_with_evidence_is_preparable_and_reportable() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="supported-projection",
        task_status=TaskStatus.STOPPED.value,
    )
    _add_evidence(db, task=task)
    db.commit()

    rows = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == task.id
    assert row.runtime_retired is True
    assert row.is_preparable is True
    assert row.memo_mode == "supported"
    assert row.is_reportable is True
    assert row.not_preparable_reason is None
    assert row.counts.evidence_count == 1


def test_limited_projection_with_useful_execution_is_preparable_but_not_reportable() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="limited-projection",
        task_status=TaskStatus.STOPPED.value,
    )
    db.add(
        TaskHistory(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_id=task.id,
            old_status=TaskStatus.STARTING.value,
            new_status=TaskStatus.RUNNING.value,
            change_source="system",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.runtime_retired is True
    assert row.is_preparable is True
    assert row.memo_mode == "limited"
    assert row.is_reportable is False
    assert row.not_preparable_reason is None
    assert row.counts.has_default_reportable_sources is False


def test_stopped_runner_without_retirement_signal_is_not_preparable() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="runner-no-retire",
        task_status=TaskStatus.STOPPED.value,
        runtime_placement_mode="runner",
    )
    db.add(
        TaskHistory(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_id=task.id,
            old_status=TaskStatus.STARTING.value,
            new_status=TaskStatus.RUNNING.value,
            change_source="system",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.runtime_retired is False
    assert row.memo_mode == "limited"
    assert row.is_preparable is False
    assert row.is_reportable is False
    assert row.not_preparable_reason == REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED


def test_stopped_runner_with_evidence_without_retirement_signal_is_not_reportable() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="runner-evidence-no-retire",
        task_status=TaskStatus.STOPPED.value,
        runtime_placement_mode="runner",
    )
    db.add(
        TaskHistory(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_id=task.id,
            old_status=TaskStatus.STARTING.value,
            new_status=TaskStatus.RUNNING.value,
            change_source="system",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    _add_evidence(db, task=task)
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.runtime_retired is False
    assert row.memo_mode == "supported"
    assert row.counts.evidence_count == 1
    assert row.counts.has_default_reportable_sources is True
    assert row.is_preparable is False
    assert row.is_reportable is False
    assert row.not_preparable_reason == REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED


def test_active_runtime_tasks_are_visible_but_not_preparable() -> None:
    db = _build_session()
    tenant, user, engagement, _task = _seed_scope(
        db,
        label="running-visible",
        task_status=TaskStatus.RUNNING.value,
    )
    for status in (TaskStatus.QUEUED, TaskStatus.STARTING, TaskStatus.STOPPING):
        task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name=f"Task {status.value}",
            status=status.value,
        )
        db.add(task)
    db.commit()

    rows = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )

    assert {row.task_status for row in rows} == {
        TaskStatus.RUNNING.value,
        TaskStatus.QUEUED.value,
        TaskStatus.STARTING.value,
        TaskStatus.STOPPING.value,
    }
    assert all(row.is_preparable is False for row in rows)
    assert all(row.not_preparable_reason == REASON_TASK_NOT_STOPPED for row in rows)


def test_running_task_with_evidence_is_visible_but_not_reportable() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="running-evidence-visible",
        task_status=TaskStatus.RUNNING.value,
    )
    _add_evidence(db, task=task)
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.runtime_retired is False
    assert row.memo_mode == "supported"
    assert row.counts.evidence_count == 1
    assert row.counts.has_default_reportable_sources is True
    assert row.is_preparable is False
    assert row.is_reportable is False
    assert row.not_preparable_reason == REASON_TASK_NOT_STOPPED


def test_projection_enforces_selected_engagement_membership() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="engagement-owner",
        task_status=TaskStatus.STOPPED.value,
    )
    other_engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Other Engagement",
    )
    db.add(other_engagement)
    db.flush()
    other_task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=other_engagement.id,
        name="Other Task",
        status=TaskStatus.STOPPED.value,
    )
    db.add(other_task)
    db.flush()
    _add_evidence(db, task=task)
    _add_evidence(db, task=other_task)
    db.commit()

    rows = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )

    assert [row.task_id for row in rows] == [task.id]
    assert rows[0].counts.evidence_count == 1


def test_candidate_only_projection_requires_explicit_inclusion_without_reportability() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="candidate-projection",
        task_status=TaskStatus.STOPPED.value,
    )
    _add_finding(
        db,
        task=task,
        finding_key="finding:candidate-only-projection",
        assertion_level="candidate",
        status="candidate",
        metadata={"authority": {"candidate_only": True}},
    )
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.counts.candidate_finding_count == 1
    assert row.candidate_findings_require_explicit_inclusion is True
    assert row.is_reportable is False
    assert row.memo_mode is None
    assert row.not_preparable_reason == REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL


def test_projection_includes_current_memo_and_stale_input_state() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="stale-projection",
        task_status=TaskStatus.STOPPED.value,
    )
    _add_evidence(db, task=task)
    ready_memo = _add_memo(
        db,
        task=task,
        version=1,
        status="ready",
        is_current=True,
        source_watermark={"schema_version": 1, "empty": True, "sources": {}},
    )
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.input_state == "stale"
    assert row.current_memo is not None
    assert row.current_memo.id == ready_memo.id
    assert row.latest_memo_attempt is not None
    assert row.latest_memo_attempt.id == ready_memo.id
    assert row.source_watermark["empty"] is False


def test_projection_surfaces_failed_first_prepare_without_current_memo() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="failed-first-projection",
        task_status=TaskStatus.STOPPED.value,
    )
    failed_attempt = _add_memo(
        db,
        task=task,
        version=1,
        status="failed",
        is_current=False,
        source_watermark={"schema_version": 1, "empty": True, "sources": {}},
        error_message="Safe failure summary.",
    )
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.input_state == "failed"
    assert row.current_memo is None
    assert row.latest_memo_attempt is not None
    assert row.latest_memo_attempt.id == failed_attempt.id
    assert row.latest_memo_attempt.status == "failed"


def test_projection_surfaces_preparing_attempt_without_current_memo() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="preparing-projection",
        task_status=TaskStatus.STOPPED.value,
    )
    preparing_attempt = _add_memo(
        db,
        task=task,
        version=1,
        status="preparing",
        is_current=False,
        source_watermark={"schema_version": 1, "empty": True, "sources": {}},
    )
    db.commit()

    row = InputInventoryService(db)._project_engagement_task_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )[0]

    assert row.input_state == "preparing"
    assert row.current_memo is None
    assert row.latest_memo_attempt is not None
    assert row.latest_memo_attempt.id == preparing_attempt.id
    assert row.latest_memo_attempt.status == "preparing"


def test_list_engagement_inputs_surfaces_ready_current_and_latest_memo_attempt() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="ready-public-memo",
        task_status=TaskStatus.STOPPED.value,
    )
    source_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    ready_memo = _add_memo(
        db,
        task=task,
        version=1,
        status="ready",
        is_current=True,
        source_watermark=source_watermark,
    )
    db.commit()

    row = InputInventoryService(db).list_engagement_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    ).tasks[0]

    assert row.input_state == "ready"
    assert row.current_memo is not None
    assert row.current_memo.id == ready_memo.id
    assert row.latest_memo_attempt is not None
    assert row.latest_memo_attempt.id == ready_memo.id
    assert row.latest_memo_attempt.status == "ready"


def test_list_engagement_inputs_surfaces_failed_regeneration_with_previous_current_memo() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="failed-regeneration-public",
        task_status=TaskStatus.STOPPED.value,
    )
    source_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    ready_memo = _add_memo(
        db,
        task=task,
        version=1,
        status="ready",
        is_current=True,
        source_watermark=source_watermark,
    )
    failed_attempt = _add_memo(
        db,
        task=task,
        version=2,
        status="failed",
        is_current=False,
        source_watermark=source_watermark,
        error_message="Safe failure summary.",
    )
    db.commit()

    row = InputInventoryService(db).list_engagement_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    ).tasks[0]

    assert row.input_state == "ready"
    assert row.current_memo is not None
    assert row.current_memo.id == ready_memo.id
    assert row.latest_memo_attempt is not None
    assert row.latest_memo_attempt.id == failed_attempt.id
    assert row.latest_memo_attempt.status == "failed"
    assert row.latest_memo_attempt.error_message == "Safe failure summary."


def test_list_engagement_inputs_returns_typed_contract_in_deterministic_order() -> None:
    db = _build_session()
    tenant, user, engagement, first_task = _seed_scope(
        db,
        label="public-contract-first",
        task_status=TaskStatus.STOPPED.value,
    )
    first_task.created_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    second_task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name="Task public-contract-second",
        status=TaskStatus.RUNNING.value,
        created_at=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
    )
    db.add(second_task)
    _add_evidence(db, task=first_task)
    db.commit()

    response = InputInventoryService(db).list_engagement_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )

    assert response.engagement_id == engagement.id
    assert [row.task_id for row in response.tasks] == [first_task.id, second_task.id]
    assert response.tasks[0].memo_mode == "supported"
    assert response.tasks[0].is_preparable is True
    assert response.tasks[0].is_reportable is True
    assert response.tasks[0].counts.evidence == 1
    assert response.tasks[1].task_status == TaskStatus.RUNNING.value
    assert response.tasks[1].is_preparable is False
    assert response.tasks[1].not_preparable_reason == REASON_TASK_NOT_STOPPED


def test_list_engagement_inputs_excludes_foreign_tasks_and_validates_owned_engagement() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="public-owned",
        task_status=TaskStatus.STOPPED.value,
    )
    _other_tenant, _other_user, _other_engagement, other_task = _seed_scope(
        db,
        label="public-foreign",
        task_status=TaskStatus.STOPPED.value,
    )
    same_tenant_other_engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Same Tenant Other Engagement",
    )
    db.add(same_tenant_other_engagement)
    db.flush()
    same_tenant_other_task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=same_tenant_other_engagement.id,
        name="Same Tenant Other Task",
        status=TaskStatus.STOPPED.value,
    )
    db.add(same_tenant_other_task)
    _add_evidence(db, task=task)
    _add_evidence(db, task=other_task)
    _add_evidence(db, task=same_tenant_other_task)
    db.commit()

    response = InputInventoryService(db).list_engagement_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )

    assert [row.task_id for row in response.tasks] == [task.id]
    assert response.tasks[0].counts.evidence == 1

    with pytest.raises(ReportingInputInventoryNotFoundError):
        InputInventoryService(db).list_engagement_inputs(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=other_task.engagement_id,
        )


def test_list_engagement_inputs_surfaces_candidate_counts_and_current_memo_summary() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="public-candidate-memo",
        task_status=TaskStatus.STOPPED.value,
    )
    _add_evidence(db, task=task)
    _add_finding(
        db,
        task=task,
        finding_key="finding:public-candidate",
        assertion_level="candidate",
        status="candidate",
        metadata={"authority": {"candidate_only": True}},
    )
    ready_memo = _add_memo(
        db,
        task=task,
        version=1,
        status="ready",
        is_current=True,
        source_watermark={"schema_version": 1, "empty": True, "sources": {}},
    )
    db.commit()

    row = InputInventoryService(db).list_engagement_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    ).tasks[0]

    assert row.counts.candidate_findings == 1
    assert row.candidate_findings_require_explicit_inclusion is True
    assert row.current_memo is not None
    assert row.current_memo.id == ready_memo.id
    assert row.input_state == "stale"
    assert row.source_watermark.latest_evidence_created_at is not None


def test_list_engagement_inputs_does_not_mutate_reporting_or_lifecycle_rows() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db,
        label="public-readonly",
        task_status=TaskStatus.STOPPED.value,
    )
    _add_evidence(db, task=task)
    _add_memo(
        db,
        task=task,
        version=1,
        status="ready",
        is_current=True,
        source_watermark={"schema_version": 1, "empty": True, "sources": {}},
    )
    db.commit()

    before = (
        db.query(Task).count(),
        db.query(TaskHistory).count(),
        db.query(TaskClosureMemo).count(),
        len(db.dirty),
        len(db.new),
        len(db.deleted),
    )

    InputInventoryService(db).list_engagement_inputs(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )

    after = (
        db.query(Task).count(),
        db.query(TaskHistory).count(),
        db.query(TaskClosureMemo).count(),
        len(db.dirty),
        len(db.new),
        len(db.deleted),
    )
    assert after == before
