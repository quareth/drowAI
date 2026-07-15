"""Tests for tenant-scoped checkpoint/HITL retention executor behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import uuid as uuid_lib

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time_utils import utc_now
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, TaskTurnCounter, User
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.models.tenant import Tenant
from backend.services.langgraph_chat.checkpoint.retention_service import (
    CheckpointRetentionExecutor,
    ACTIVE_TURN_WORKFLOW_RETENTION_PROTECTED,
    PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED,
    RESUMING_INTERRUPT_TICKET_RETENTION_PROTECTED,
    RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED,
    TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED,
)
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    owned_checkpoint_thread_ids,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowState,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)
from backend.services.task.graph_state_cleanup_service import TaskGraphStateCleanupService


@dataclass(frozen=True, slots=True)
class _Policy:
    checkpoint_retention_days_after_terminal: int = 30
    retention_batch_size_per_tenant: int = 100


class _FakeCheckpointerService:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    async def invalidate_task(self, task_id: int) -> None:
        self.invalidated.append(int(task_id))


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    _create_checkpoint_tables(db)
    return db


def test_checkpoint_retention_dry_run_is_tenant_scoped_and_does_not_mutate() -> None:
    db = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="dry-run")
        other_tenant, other_user, other_engagement = _seed_scope(db, label="other")
        candidate = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        protected_pending = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        protected_retryable = _seed_task(
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
        _seed_resume_state(db, task=candidate, workflow_state=TurnWorkflowState.COMPLETED.value)
        _seed_resume_state(
            db,
            task=protected_pending,
            ticket_state=InterruptTicketState.PENDING,
        )
        _seed_resume_state(
            db,
            task=protected_retryable,
            workflow_state=TurnWorkflowState.FAILED.value,
            workflow_metadata={
                "retryable": True,
                "retry_attempt_count": 0,
                "retry_max_attempts": 2,
            },
        )
        _seed_resume_state(db, task=foreign_task, workflow_state=TurnWorkflowState.COMPLETED.value)

        result = CheckpointRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.retention_class == RETENTION_CLASS_RUNTIME_RESUME_STATE
        assert result.counts.candidate_count == 3
        assert result.counts.protected_count == 2
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED: 1,
            PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED: 1,
            RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id, decision.reason_code)
            for decision in result.decisions
        } == {
            (
                RETENTION_DECISION_CANDIDATE,
                f"task:{candidate.id}",
                TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED,
            ),
            (
                RETENTION_DECISION_PROTECTED,
                f"task:{protected_pending.id}",
                PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED,
            ),
            (
                RETENTION_DECISION_PROTECTED,
                f"task:{protected_retryable.id}",
                RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED,
            ),
        }
        assert _count_resume_rows(db, task=candidate) == 9
        assert _count_resume_rows(db, task=protected_pending) == 9
        assert _count_resume_rows(db, task=protected_retryable) == 9
        assert _count_resume_rows(db, task=foreign_task) == 9
    finally:
        db.close()


def test_checkpoint_retention_apply_deletes_only_unprotected_owned_state() -> None:
    db = _build_session()
    fake_checkpointer = _FakeCheckpointerService()
    try:
        tenant, user, engagement = _seed_scope(db, label="apply")
        other_tenant, other_user, other_engagement = _seed_scope(db, label="apply-other")
        candidate = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        protected_pending = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        protected_retryable = _seed_task(
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
        _seed_resume_state(db, task=candidate, workflow_state=TurnWorkflowState.COMPLETED.value)
        _seed_resume_state(
            db,
            task=protected_pending,
            ticket_state=InterruptTicketState.PENDING,
        )
        _seed_resume_state(
            db,
            task=protected_retryable,
            workflow_state=TurnWorkflowState.FAILED.value,
            workflow_metadata={
                "retryable": True,
                "retry_attempt_count": 1,
                "retry_max_attempts": 2,
            },
        )
        _seed_resume_state(db, task=foreign_task, workflow_state=TurnWorkflowState.COMPLETED.value)
        executor = CheckpointRetentionExecutor(
            db,
            graph_cleanup_service=TaskGraphStateCleanupService(
                db,
                checkpointer_service=fake_checkpointer,  # type: ignore[arg-type]
            ),
        )

        result = executor.run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 3
        assert result.counts.protected_count == 2
        assert result.counts.applied_count == 1
        assert result.reason_counts == {
            TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED: 1,
            PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED: 1,
            RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id)
            for decision in result.decisions
        } == {
            (RETENTION_DECISION_APPLIED, f"task:{candidate.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{protected_pending.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{protected_retryable.id}"),
        }
        assert _count_resume_rows(db, task=candidate) == 0
        assert _count_resume_rows(db, task=protected_pending) == 9
        assert _count_resume_rows(db, task=protected_retryable) == 9
        assert _count_resume_rows(db, task=foreign_task) == 9
        assert fake_checkpointer.invalidated == [candidate.id]

        second = executor.run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert second.counts.candidate_count == 2
        assert second.counts.applied_count == 0
        assert _count_resume_rows(db, task=protected_pending) == 9
        assert _count_resume_rows(db, task=protected_retryable) == 9
        assert _count_resume_rows(db, task=foreign_task) == 9
        assert fake_checkpointer.invalidated == [candidate.id]
    finally:
        db.close()


def test_checkpoint_retention_protects_resumable_ticket_and_workflow_states() -> None:
    db = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="protected")
        resuming_ticket = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        running_workflow = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        waiting_workflow = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        retrying_workflow = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=45,
        )
        _seed_resume_state(
            db,
            task=resuming_ticket,
            ticket_state=InterruptTicketState.RESUMING,
        )
        _seed_resume_state(
            db,
            task=running_workflow,
            workflow_state=TurnWorkflowState.RUNNING.value,
        )
        _seed_resume_state(
            db,
            task=waiting_workflow,
            workflow_state=TurnWorkflowState.WAITING_FOR_HUMAN.value,
        )
        _seed_resume_state(
            db,
            task=retrying_workflow,
            workflow_state=TurnWorkflowState.RETRYING.value,
        )

        result = CheckpointRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.counts.candidate_count == 4
        assert result.counts.protected_count == 4
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            RESUMING_INTERRUPT_TICKET_RETENTION_PROTECTED: 1,
            ACTIVE_TURN_WORKFLOW_RETENTION_PROTECTED: 3,
        }
        assert {
            (decision.outcome, decision.resource_id)
            for decision in result.decisions
        } == {
            (RETENTION_DECISION_PROTECTED, f"task:{resuming_ticket.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{running_workflow.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{waiting_workflow.id}"),
            (RETENTION_DECISION_PROTECTED, f"task:{retrying_workflow.id}"),
        }
    finally:
        db.close()


def test_checkpoint_retention_apply_is_batched() -> None:
    db = _build_session()
    fake_checkpointer = _FakeCheckpointerService()
    try:
        tenant, user, engagement = _seed_scope(db, label="batch")
        oldest = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.COMPLETED.value,
            age_days=60,
        )
        next_oldest = _seed_task(
            db,
            tenant=tenant,
            user=user,
            engagement=engagement,
            status=TaskStatus.FAILED.value,
            age_days=45,
        )
        _seed_resume_state(db, task=oldest, workflow_state=TurnWorkflowState.COMPLETED.value)
        _seed_resume_state(
            db,
            task=next_oldest,
            workflow_state=TurnWorkflowState.COMPLETED.value,
        )
        executor = CheckpointRetentionExecutor(
            db,
            graph_cleanup_service=TaskGraphStateCleanupService(
                db,
                checkpointer_service=fake_checkpointer,  # type: ignore[arg-type]
            ),
        )

        first = executor.run(
            policy=_Policy(retention_batch_size_per_tenant=1),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert first.counts.candidate_count == 1
        assert first.counts.applied_count == 1
        assert _count_resume_rows(db, task=oldest) == 0
        assert _count_resume_rows(db, task=next_oldest) == 9

        second = executor.run(
            policy=_Policy(retention_batch_size_per_tenant=1),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert second.counts.candidate_count == 1
        assert second.counts.applied_count == 1
        assert _count_resume_rows(db, task=next_oldest) == 0
        assert fake_checkpointer.invalidated == [oldest.id, next_oldest.id]
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


def _seed_resume_state(
    db: Session,
    *,
    task: Task,
    workflow_state: str = TurnWorkflowState.COMPLETED.value,
    ticket_state: InterruptTicketState = InterruptTicketState.COMPLETED,
    workflow_metadata: dict[str, object] | None = None,
) -> None:
    checkpoint_id = f"ckpt-{task.id}"
    db.add(
        TurnWorkflow(
            task_id=task.id,
            tenant_id=task.tenant_id,
            conversation_id=f"conversation-{task.id}",
            turn_id=f"turn-{task.id}",
            turn_sequence=1,
            state=workflow_state,
            graph_name="test_graph",
            checkpoint_id=checkpoint_id,
            workflow_metadata=workflow_metadata or {},
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
            state=ticket_state,
        )
    )
    db.merge(TaskTurnCounter(task_id=task.id, next_turn=2))
    for thread_id in owned_checkpoint_thread_ids(
        task_id=task.id,
        graph_thread_id=task.graph_thread_id,
    ):
        for table_name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            db.execute(
                text(
                    f"INSERT INTO {table_name} (thread_id, payload) "
                    "VALUES (:thread_id, :payload)"
                ),
                {"thread_id": thread_id, "payload": f"payload-{task.id}"},
            )
    db.flush()


def _create_checkpoint_tables(db: Session) -> None:
    for table_name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        db.execute(
            text(
                f"CREATE TABLE {table_name} "
                "(thread_id TEXT NOT NULL, payload TEXT)"
            )
        )
    db.flush()


def _count_resume_rows(db: Session, *, task: Task) -> int:
    thread_ids = owned_checkpoint_thread_ids(
        task_id=task.id,
        graph_thread_id=task.graph_thread_id,
    )
    checkpoint_count = 0
    for table_name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        checkpoint_count += int(
            db.execute(
                text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    "WHERE thread_id IN :thread_ids"
                ).bindparams(bindparam("thread_ids", expanding=True)),
                {"thread_ids": list(thread_ids)},
            ).scalar_one()
        )
    return (
        db.query(TurnWorkflow).filter(TurnWorkflow.task_id == task.id).count()
        + db.query(InterruptTicket).filter(InterruptTicket.task_id == task.id).count()
        + db.query(TaskTurnCounter).filter(TaskTurnCounter.task_id == task.id).count()
        + checkpoint_count
    )
