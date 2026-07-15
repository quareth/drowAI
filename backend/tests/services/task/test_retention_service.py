"""Tests for tenant-scoped task record retention executor behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time_utils import utc_now
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Report, Task, TaskHistory, User
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.models.knowledge import KnowledgeEvidenceArchive
from backend.models.reporting import EngagementReport
from backend.models.tenant import Tenant
from backend.services.langgraph_chat.checkpoint.retention_service import (
    CheckpointRetentionExecutor,
    PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowState,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)
from backend.services.task.retention_service import (
    ACTIVE_TASK_RETENTION_PROTECTED,
    DURABLE_KNOWLEDGE_DELETE_PREFLIGHT_BLOCKED,
    TERMINAL_TASK_RETENTION_EXPIRED,
    TaskRetentionExecutor,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    task_retention_days_after_terminal: int = 30
    checkpoint_retention_days_after_terminal: int = 30
    retention_batch_size_per_tenant: int = 100


class _DeleteSafetyPreflight:
    def __init__(self, safe_by_task_id: dict[int, bool] | None = None) -> None:
        self.safe_by_task_id = safe_by_task_id or {}
        self.calls: list[tuple[int, int | None]] = []

    def ensure_task_delete_safe(
        self,
        *,
        task_id: int,
        engagement_id: int | None,
    ) -> dict[str, object]:
        self.calls.append((int(task_id), engagement_id))
        return {
            "safe": self.safe_by_task_id.get(int(task_id), True),
            "catchup_attempted": False,
            "unsafe_execution_ids": [],
            "reason": "test preflight",
        }


def _build_session(*, enforce_foreign_keys: bool = False) -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    if enforce_foreign_keys:
        db.execute(text("PRAGMA foreign_keys=ON"))
    return db


def test_task_retention_dry_run_is_tenant_scoped_and_does_not_mutate() -> None:
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
        preflight = _DeleteSafetyPreflight()

        result = TaskRetentionExecutor(
            db,
            delete_safety_preflight=preflight,
        ).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.retention_class == RETENTION_CLASS_TASK_RECORD
        assert result.counts.candidate_count == 1
        assert result.counts.protected_count == 1
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            ACTIVE_TASK_RETENTION_PROTECTED: 1,
            TERMINAL_TASK_RETENTION_EXPIRED: 1,
        }
        assert {(decision.outcome, decision.resource_id) for decision in result.decisions} == {
            (RETENTION_DECISION_CANDIDATE, f"task:{old_terminal.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{active_task.id}"),
        }
        assert preflight.calls == []
        assert _task_exists(db, old_terminal.id)
        assert _task_exists(db, recent_terminal.id)
        assert _task_exists(db, active_task.id)
        assert _task_exists(db, foreign_task.id)
    finally:
        db.close()


def test_task_retention_apply_deletes_task_record_dependencies_only() -> None:
    db = _build_session(enforce_foreign_keys=True)
    try:
        tenant, user, engagement = _seed_scope(db, label="apply")
        other_tenant, other_user, other_engagement = _seed_scope(db, label="apply-other")
        safe_task = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        unsafe_task = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.FAILED.value,
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
        safe_history = _seed_task_history(db, task=safe_task, tenant=tenant, user=user)
        legacy_report = _seed_legacy_task_report(db, task=safe_task, tenant=tenant, user=user)
        report = _seed_engagement_report(db, tenant=tenant, user=user, engagement=engagement)
        evidence = _seed_evidence(db, tenant=tenant, user=user, engagement=engagement, task=safe_task)
        preflight = _DeleteSafetyPreflight(
            {
                safe_task.id: True,
                unsafe_task.id: False,
            }
        )

        result = TaskRetentionExecutor(
            db,
            delete_safety_preflight=preflight,
        ).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 2
        assert result.counts.protected_count == 1
        assert result.counts.applied_count == 1
        assert result.reason_counts == {
            TERMINAL_TASK_RETENTION_EXPIRED: 1,
            DURABLE_KNOWLEDGE_DELETE_PREFLIGHT_BLOCKED: 1,
        }
        assert {(decision.outcome, decision.resource_id) for decision in result.decisions} == {
            (RETENTION_DECISION_APPLIED, f"task:{safe_task.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{unsafe_task.id}"),
        }
        assert preflight.calls == [
            (safe_task.id, engagement.id),
            (unsafe_task.id, engagement.id),
        ]
        assert not _task_exists(db, safe_task.id)
        assert not _task_history_exists(db, safe_history.id)
        assert not _legacy_report_exists(db, legacy_report.id)
        assert _task_exists(db, unsafe_task.id)
        assert _task_exists(db, foreign_task.id)
        assert db.get(EngagementReport, report.id) is not None
        assert db.get(KnowledgeEvidenceArchive, evidence.id) is not None
    finally:
        db.close()


def test_task_retention_preserves_checkpoint_protected_resume_state() -> None:
    db = _build_session(enforce_foreign_keys=True)
    try:
        tenant, user, engagement = _seed_scope(db, label="checkpoint-protected")
        protected_task = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        _seed_protected_resume_state(db, task=protected_task)
        preflight = _DeleteSafetyPreflight()

        checkpoint_result = CheckpointRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )
        task_result = TaskRetentionExecutor(
            db,
            delete_safety_preflight=preflight,
        ).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert checkpoint_result.counts.candidate_count == 1
        assert checkpoint_result.counts.protected_count == 1
        assert checkpoint_result.counts.applied_count == 0
        assert checkpoint_result.reason_counts == {
            PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED: 1,
        }
        assert task_result.counts.candidate_count == 1
        assert task_result.counts.protected_count == 1
        assert task_result.counts.applied_count == 0
        assert task_result.reason_counts == {
            PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id, decision.reason_code)
            for decision in task_result.decisions
        } == {
            (
                RETENTION_DECISION_PROTECTED,
                f"task:{protected_task.id}",
                PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED,
            ),
        }
        assert preflight.calls == []
        assert _task_exists(db, protected_task.id)
        assert _protected_resume_row_count(db, task=protected_task) == 2
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


def _seed_task_history(
    db: Session,
    *,
    task: Task,
    tenant: Tenant,
    user: User,
) -> TaskHistory:
    history = TaskHistory(
        task_id=task.id,
        tenant_id=tenant.id,
        user_id=user.id,
        old_status=TaskStatus.RUNNING.value,
        new_status=task.status,
        transition_reason="test terminal transition",
        change_source="test",
    )
    db.add(history)
    db.flush()
    return history


def _seed_legacy_task_report(
    db: Session,
    *,
    task: Task,
    tenant: Tenant,
    user: User,
) -> Report:
    report = Report(
        task_id=task.id,
        tenant_id=tenant.id,
        user_id=user.id,
        title="Legacy Task Report",
        content="legacy report content",
        findings=[],
    )
    db.add(report)
    db.flush()
    return report


def _seed_engagement_report(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    engagement: Engagement,
) -> EngagementReport:
    report = EngagementReport(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="executive",
        version=1,
        status="ready",
        is_current=True,
        title="Current Report",
        sections=[],
        source_task_memo_ids=[],
        source_knowledge_refs=[],
        source_evidence_refs=[],
    )
    db.add(report)
    db.flush()
    return report


def _seed_evidence(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    engagement: Engagement,
    task: Task,
) -> KnowledgeEvidenceArchive:
    evidence = KnowledgeEvidenceArchive(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=uuid_lib.uuid4(),
        storage_mode="inline_excerpt",
        inline_excerpt="safe excerpt",
        lineage_snapshot={},
    )
    db.add(evidence)
    db.flush()
    return evidence


def _seed_protected_resume_state(db: Session, *, task: Task) -> None:
    checkpoint_id = f"ckpt-{task.id}"
    db.add(
        TurnWorkflow(
            task_id=task.id,
            tenant_id=task.tenant_id,
            conversation_id=f"conversation-{task.id}",
            turn_id=f"turn-{task.id}",
            turn_sequence=1,
            state=TurnWorkflowState.WAITING_FOR_HUMAN.value,
            graph_name="test_graph",
            checkpoint_id=checkpoint_id,
            workflow_metadata={},
        )
    )
    db.add(
        InterruptTicket(
            interrupt_id=f"interrupt-{task.id}-{uuid_lib.uuid4().hex[:8]}",
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="test_graph",
            interrupt_type="tool",
            checkpoint_id=checkpoint_id,
            thread_id=f"graph-{task.graph_thread_id}",
            turn_id=f"turn-{task.id}",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
        )
    )
    db.flush()


def _task_exists(db: Session, task_id: int) -> bool:
    return db.query(Task.id).filter(Task.id == int(task_id)).first() is not None


def _protected_resume_row_count(db: Session, *, task: Task) -> int:
    return (
        db.query(TurnWorkflow).filter(TurnWorkflow.task_id == task.id).count()
        + db.query(InterruptTicket).filter(InterruptTicket.task_id == task.id).count()
    )


def _task_history_exists(db: Session, history_id: int) -> bool:
    return db.query(TaskHistory.id).filter(TaskHistory.id == int(history_id)).first() is not None


def _legacy_report_exists(db: Session, report_id: int) -> bool:
    return db.query(Report.id).filter(Report.id == int(report_id)).first() is not None
