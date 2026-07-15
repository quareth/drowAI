"""Checkpoint-owned retention executor for terminal task resume state.

This module evaluates tenant-scoped LangGraph checkpoint/HITL resume-state
retention and delegates destructive graph-state deletion to the task graph
cleanup service only after protected runtime checks pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import String, cast, column, func, inspect, literal, or_, table
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, TaskTurnCounter
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS,
    TurnWorkflowState,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_DRY_RUN,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunMode,
    TenantId,
    validate_run_mode,
)
from backend.services.task.graph_state_cleanup_service import TaskGraphStateCleanupService


TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED = (
    "terminal_task_checkpoint_retention_expired"
)
ACTIVE_TASK_CHECKPOINT_RETENTION_PROTECTED = (
    "active_task_checkpoint_retention_protected"
)
RECENT_TASK_CHECKPOINT_RETENTION_PROTECTED = (
    "recent_task_checkpoint_retention_protected"
)
PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED = (
    "pending_interrupt_ticket_retention_protected"
)
RESUMING_INTERRUPT_TICKET_RETENTION_PROTECTED = (
    "resuming_interrupt_ticket_retention_protected"
)
ACTIVE_TURN_WORKFLOW_RETENTION_PROTECTED = "active_turn_workflow_retention_protected"
RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED = (
    "retryable_turn_workflow_retention_protected"
)
TASK_OWNERSHIP_RETENTION_PROTECTED = "task_ownership_retention_protected"

_CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
)
_ACTIVE_WORKFLOW_STATES = frozenset(
    {
        TurnWorkflowState.RUNNING.value,
        TurnWorkflowState.WAITING_FOR_HUMAN.value,
        TurnWorkflowState.RETRYING.value,
    }
)
_PROTECTED_INTERRUPT_STATES = frozenset(
    {
        InterruptTicketState.PENDING.value,
        InterruptTicketState.RESUMING.value,
    }
)


class SupportsCheckpointRetentionPolicy(Protocol):
    """Policy fields consumed by the checkpoint retention executor."""

    checkpoint_retention_days_after_terminal: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class CheckpointRetentionExecutor:
    """Run bounded terminal checkpoint/HITL retention through the shared contract."""

    db: Session
    graph_cleanup_service: TaskGraphStateCleanupService | None = None
    name: str = "checkpoint.retention"
    retention_class: str = RETENTION_CLASS_RUNTIME_RESUME_STATE

    def run(
        self,
        *,
        policy: SupportsCheckpointRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally delete tenant-scoped runtime resume state."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        cutoff = utc_now() - timedelta(
            days=_normalize_positive_int(
                policy.checkpoint_retention_days_after_terminal,
                field_name="policy.checkpoint_retention_days_after_terminal",
            )
        )

        candidates = _load_terminal_candidates(
            self.db,
            tenant_id=scoped_tenant_id,
            terminal_before=cutoff,
            limit=effective_limit,
        )
        protected_tasks = _load_protected_tasks(
            self.db,
            tenant_id=scoped_tenant_id,
            terminal_before=cutoff,
            limit=effective_limit,
        )

        applied_count = 0
        protected_count = len(protected_tasks)
        decisions = [
            _checkpoint_decision(
                task_id=int(task.id),
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=_protected_task_reason(task=task, terminal_before=cutoff),
            )
            for task in protected_tasks
        ]

        cleanup_service = self.graph_cleanup_service or TaskGraphStateCleanupService(
            self.db
        )
        for task in candidates:
            task_id = int(task.id)
            protected_reason = _protected_resume_state_reason(
                self.db,
                tenant_id=scoped_tenant_id,
                task_id=task_id,
            )
            if protected_reason is not None:
                protected_count += 1
                decisions.append(
                    _checkpoint_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_PROTECTED,
                        reason_code=protected_reason,
                    )
                )
                continue

            if run_mode == RETENTION_RUN_MODE_DRY_RUN:
                decisions.append(
                    _checkpoint_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_CANDIDATE,
                        reason_code=TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED,
                    )
                )
                continue

            if not _task_belongs_to_tenant(
                self.db,
                tenant_id=scoped_tenant_id,
                task_id=task_id,
            ):
                protected_count += 1
                decisions.append(
                    _checkpoint_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_PROTECTED,
                        reason_code=TASK_OWNERSHIP_RETENTION_PROTECTED,
                    )
                )
                continue

            cleanup_service.cleanup_task_graph_state_sync(
                task_id=task_id,
                graph_thread_id=str(task.graph_thread_id or ""),
            )
            applied_count += 1
            decisions.append(
                _checkpoint_decision(
                    task_id=task_id,
                    outcome=RETENTION_DECISION_APPLIED,
                    reason_code=TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED,
                )
            )

        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_RUNTIME_RESUME_STATE,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=len(candidates) + len(protected_tasks),
                candidate_count=len(candidates),
                protected_count=protected_count,
                applied_count=applied_count,
                batch_count=len(candidates),
                batch_limit=effective_limit,
            ),
            reason_counts=_reason_counts(decisions),
            decisions=tuple(decisions),
        )


def _load_terminal_candidates(
    db: Session,
    *,
    tenant_id: int,
    terminal_before: object,
    limit: int,
) -> list[Task]:
    terminal_at = _terminal_at_expression()
    return (
        db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            Task.status.in_(tuple(TaskStatus.get_terminal_statuses())),
            terminal_at < terminal_before,
            _has_resume_state_rows(db, tenant_id=tenant_id),
        )
        .order_by(terminal_at.asc(), Task.id.asc())
        .limit(limit)
        .all()
    )


def _load_protected_tasks(
    db: Session,
    *,
    tenant_id: int,
    terminal_before: object,
    limit: int,
) -> list[Task]:
    terminal_at = _terminal_at_expression()
    touched_at = func.coalesce(Task.updated_at, Task.created_at)
    return (
        db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            or_(
                Task.status.in_(tuple(TaskStatus.active_task_statuses())),
                (
                    Task.status.in_(tuple(TaskStatus.get_terminal_statuses()))
                    & (terminal_at >= terminal_before)
                ),
            ),
            _has_resume_state_rows(db, tenant_id=tenant_id),
        )
        .order_by(touched_at.asc(), Task.id.asc())
        .limit(limit)
        .all()
    )


def _has_resume_state_rows(db: Session, *, tenant_id: int) -> object:
    predicates: list[object] = [
        db.query(TurnWorkflow.id)
        .filter(
            TurnWorkflow.tenant_id == tenant_id,
            TurnWorkflow.task_id == Task.id,
        )
        .exists(),
        db.query(InterruptTicket.id)
        .filter(
            InterruptTicket.tenant_id == tenant_id,
            InterruptTicket.task_id == Task.id,
        )
        .exists(),
        db.query(TaskTurnCounter.task_id)
        .filter(TaskTurnCounter.task_id == Task.id)
        .exists(),
    ]
    predicates.extend(_checkpoint_table_exists_predicates(db))
    return or_(*predicates)


def _checkpoint_table_exists_predicates(db: Session) -> list[object]:
    inspector = inspect(db.connection())
    predicates: list[object] = []
    current_thread_id = literal("graph-") + Task.graph_thread_id
    legacy_thread_id = literal("task-") + cast(Task.id, String)
    for table_name in _CHECKPOINT_TABLES:
        if not inspector.has_table(table_name):
            continue
        checkpoint_table = table(table_name, column("thread_id"))
        predicates.append(
            db.query(literal(1))
            .select_from(checkpoint_table)
            .filter(
                checkpoint_table.c.thread_id.in_(
                    (current_thread_id, legacy_thread_id)
                )
            )
            .exists()
        )
    return predicates


def _protected_resume_state_reason(
    db: Session,
    *,
    tenant_id: int,
    task_id: int,
) -> str | None:
    ticket = (
        db.query(InterruptTicket)
        .filter(
            InterruptTicket.tenant_id == tenant_id,
            InterruptTicket.task_id == task_id,
            InterruptTicket.state.in_(tuple(_PROTECTED_INTERRUPT_STATES)),
        )
        .order_by(InterruptTicket.updated_at.desc(), InterruptTicket.id.desc())
        .first()
    )
    if ticket is not None:
        ticket_state = (
            ticket.state.value
            if isinstance(ticket.state, InterruptTicketState)
            else str(ticket.state)
        )
        if ticket_state == InterruptTicketState.RESUMING.value:
            return RESUMING_INTERRUPT_TICKET_RETENTION_PROTECTED
        return PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED

    active_workflow = (
        db.query(TurnWorkflow.id)
        .filter(
            TurnWorkflow.tenant_id == tenant_id,
            TurnWorkflow.task_id == task_id,
            TurnWorkflow.state.in_(tuple(_ACTIVE_WORKFLOW_STATES)),
        )
        .first()
    )
    if active_workflow is not None:
        return ACTIVE_TURN_WORKFLOW_RETENTION_PROTECTED

    retryable_failed_workflows = (
        db.query(TurnWorkflow)
        .filter(
            TurnWorkflow.tenant_id == tenant_id,
            TurnWorkflow.task_id == task_id,
            TurnWorkflow.state == TurnWorkflowState.FAILED.value,
            TurnWorkflow.checkpoint_id.is_not(None),
        )
        .all()
    )
    if any(_is_retryable_failed_workflow(workflow) for workflow in retryable_failed_workflows):
        return RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED
    return None


def protected_resume_state_reason(
    db: Session,
    *,
    tenant_id: int,
    task_id: int,
) -> str | None:
    """Return the checkpoint-owned protected resume-state reason for a task."""

    return _protected_resume_state_reason(
        db,
        tenant_id=tenant_id,
        task_id=task_id,
    )


def _is_retryable_failed_workflow(workflow: TurnWorkflow) -> bool:
    metadata = workflow.workflow_metadata if isinstance(workflow.workflow_metadata, dict) else {}
    if not bool(metadata.get("retryable")):
        return False
    checkpoint_id = getattr(workflow, "checkpoint_id", None)
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        return False
    retry_attempt_count = _coerce_int(metadata.get("retry_attempt_count"), default=0)
    retry_max_attempts = _coerce_positive_int(
        metadata.get("retry_max_attempts"),
        default=DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS,
    )
    return retry_attempt_count < retry_max_attempts


def _task_belongs_to_tenant(
    db: Session,
    *,
    tenant_id: int,
    task_id: int,
) -> bool:
    return (
        db.query(Task.id)
        .filter(
            Task.id == int(task_id),
            Task.tenant_id == int(tenant_id),
        )
        .first()
        is not None
    )


def _protected_task_reason(*, task: Task, terminal_before: object) -> str:
    terminal_at = _task_terminal_at(task)
    if (
        task.status in TaskStatus.get_terminal_statuses()
        and terminal_at is not None
        and terminal_at >= terminal_before
    ):
        return RECENT_TASK_CHECKPOINT_RETENTION_PROTECTED
    return ACTIVE_TASK_CHECKPOINT_RETENTION_PROTECTED


def _task_terminal_at(task: Task) -> object | None:
    return task.completed_at or task.stopped_at or task.updated_at or task.created_at


def _terminal_at_expression() -> object:
    return func.coalesce(
        Task.completed_at,
        Task.stopped_at,
        Task.updated_at,
        Task.created_at,
    )


def _checkpoint_decision(
    *,
    task_id: int,
    outcome: str,
    reason_code: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_RUNTIME_RESUME_STATE,
        outcome=outcome,
        reason_code=reason_code,
        resource_id=f"task:{task_id}",
    )


def _reason_counts(decisions: list[RetentionDecision]) -> dict[str, int]:
    reason_counts: dict[str, int] = {}
    for decision in decisions:
        reason_counts[decision.reason_code] = (
            reason_counts.get(decision.reason_code, 0) + int(decision.count)
        )
    return reason_counts


def _effective_limit(
    *,
    policy: SupportsCheckpointRetentionPolicy,
    limit: int,
) -> int:
    return min(
        _normalize_positive_int(limit, field_name="limit"),
        _normalize_positive_int(
            policy.retention_batch_size_per_tenant,
            field_name="policy.retention_batch_size_per_tenant",
        ),
    )


def _normalize_positive_int(value: object, *, field_name: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if normalized < 1:
        raise ValueError(f"{field_name} must be positive")
    return normalized


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_positive_int(value: Any, *, default: int) -> int:
    coerced = _coerce_int(value, default=default)
    return coerced if coerced > 0 else default


__all__ = [
    "ACTIVE_TASK_CHECKPOINT_RETENTION_PROTECTED",
    "ACTIVE_TURN_WORKFLOW_RETENTION_PROTECTED",
    "CheckpointRetentionExecutor",
    "PENDING_INTERRUPT_TICKET_RETENTION_PROTECTED",
    "RECENT_TASK_CHECKPOINT_RETENTION_PROTECTED",
    "RESUMING_INTERRUPT_TICKET_RETENTION_PROTECTED",
    "RETRYABLE_TURN_WORKFLOW_RETENTION_PROTECTED",
    "TERMINAL_TASK_CHECKPOINT_RETENTION_EXPIRED",
    "protected_resume_state_reason",
]
