"""Task-owned retention executor for terminal task records.

This module evaluates tenant-scoped task row retention and performs only the
task-record delete step after durable knowledge delete-safety checks pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Report, Task, TaskHistory
from backend.services.langgraph_chat.checkpoint.retention_service import (
    protected_resume_state_reason,
)
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.retention.contracts import (
    RETENTION_CLASS_TASK_RECORD,
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


TERMINAL_TASK_RETENTION_EXPIRED = "terminal_task_retention_expired"
ACTIVE_TASK_RETENTION_PROTECTED = "active_task_retention_protected"
DURABLE_KNOWLEDGE_DELETE_PREFLIGHT_BLOCKED = (
    "durable_knowledge_delete_preflight_blocked"
)


class SupportsTaskRetentionPolicy(Protocol):
    """Policy fields consumed by the task retention executor."""

    task_retention_days_after_terminal: int
    retention_batch_size_per_tenant: int


class SupportsTaskDeleteSafetyPreflight(Protocol):
    """Delete-safety preflight shape reused by task retention apply mode."""

    def ensure_task_delete_safe(
        self,
        *,
        task_id: int,
        engagement_id: int | None,
    ) -> dict[str, object]:
        """Return whether task deletion is safe for durable knowledge."""


@dataclass(frozen=True, slots=True)
class TaskRetentionExecutor:
    """Run bounded terminal task record retention through the shared contract."""

    db: Session
    delete_safety_preflight: SupportsTaskDeleteSafetyPreflight | None = None
    name: str = "task.retention"
    retention_class: str = RETENTION_CLASS_TASK_RECORD

    def run(
        self,
        *,
        policy: SupportsTaskRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally delete tenant-scoped terminal task rows."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        cutoff = utc_now() - timedelta(
            days=_normalize_positive_int(
                policy.task_retention_days_after_terminal,
                field_name="policy.task_retention_days_after_terminal",
            )
        )

        candidates = _load_terminal_candidates(
            self.db,
            tenant_id=scoped_tenant_id,
            terminal_before=cutoff,
            limit=effective_limit,
        )
        protected_active_tasks = _load_active_protected_tasks(
            self.db,
            tenant_id=scoped_tenant_id,
            terminal_before=cutoff,
            limit=effective_limit,
        )

        applied_count = 0
        protected_count = len(protected_active_tasks)
        decisions: list[RetentionDecision] = [
            _task_decision(
                task_id=int(task.id),
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=ACTIVE_TASK_RETENTION_PROTECTED,
            )
            for task in protected_active_tasks
        ]

        for task in candidates:
            task_id = int(task.id)
            protected_reason = protected_resume_state_reason(
                self.db,
                tenant_id=scoped_tenant_id,
                task_id=task_id,
            )
            if protected_reason is not None:
                protected_count += 1
                decisions.append(
                    _task_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_PROTECTED,
                        reason_code=protected_reason,
                    )
                )
                continue

            if run_mode == RETENTION_RUN_MODE_DRY_RUN:
                decisions.append(
                    _task_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_CANDIDATE,
                        reason_code=TERMINAL_TASK_RETENTION_EXPIRED,
                    )
                )
                continue

            if not self._is_delete_safe(task):
                protected_count += 1
                decisions.append(
                    _task_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_PROTECTED,
                        reason_code=DURABLE_KNOWLEDGE_DELETE_PREFLIGHT_BLOCKED,
                    )
                )
                continue

            _delete_task_record_dependencies(
                self.db,
                task_id=task_id,
                tenant_id=scoped_tenant_id,
            )
            deleted_count = (
                self.db.query(Task)
                .filter(
                    Task.id == task_id,
                    Task.tenant_id == scoped_tenant_id,
                    Task.status.in_(tuple(TaskStatus.get_terminal_statuses())),
                )
                .delete(synchronize_session=False)
            )
            if int(deleted_count) > 0:
                applied_count += int(deleted_count)
                decisions.append(
                    _task_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_APPLIED,
                        reason_code=TERMINAL_TASK_RETENTION_EXPIRED,
                    )
                )

        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_TASK_RECORD,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=len(candidates) + len(protected_active_tasks),
                candidate_count=len(candidates),
                protected_count=protected_count,
                applied_count=applied_count,
                batch_count=len(candidates),
                batch_limit=effective_limit,
            ),
            reason_counts=_reason_counts(decisions),
            decisions=tuple(decisions),
        )

    def _is_delete_safe(self, task: Task) -> bool:
        preflight = self.delete_safety_preflight or KnowledgeIngestionService(self.db)
        decision = preflight.ensure_task_delete_safe(
            task_id=int(task.id),
            engagement_id=(
                int(task.engagement_id) if task.engagement_id is not None else None
            ),
        )
        return bool(decision.get("safe"))


def _load_terminal_candidates(
    db: Session,
    *,
    tenant_id: int,
    terminal_before: object,
    limit: int,
) -> list[Task]:
    terminal_at = func.coalesce(
        Task.completed_at,
        Task.stopped_at,
        Task.updated_at,
        Task.created_at,
    )
    return (
        db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            Task.status.in_(tuple(TaskStatus.get_terminal_statuses())),
            terminal_at < terminal_before,
        )
        .order_by(terminal_at.asc(), Task.id.asc())
        .limit(limit)
        .all()
    )


def _load_active_protected_tasks(
    db: Session,
    *,
    tenant_id: int,
    terminal_before: object,
    limit: int,
) -> list[Task]:
    touched_at = func.coalesce(Task.updated_at, Task.created_at)
    return (
        db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            Task.status.in_(tuple(TaskStatus.active_task_statuses())),
            touched_at < terminal_before,
        )
        .order_by(touched_at.asc(), Task.id.asc())
        .limit(limit)
        .all()
    )


def _delete_task_record_dependencies(
    db: Session,
    *,
    task_id: int,
    tenant_id: int,
) -> None:
    terminal_task_ids = select(Task.id).where(
        Task.id == task_id,
        Task.tenant_id == tenant_id,
        Task.status.in_(tuple(TaskStatus.get_terminal_statuses())),
    )
    db.query(Report).filter(
        Report.task_id.in_(terminal_task_ids),
        Report.tenant_id == tenant_id,
    ).delete(synchronize_session=False)
    db.query(TaskHistory).filter(
        TaskHistory.task_id.in_(terminal_task_ids),
        TaskHistory.tenant_id == tenant_id,
    ).delete(synchronize_session=False)


def _task_decision(
    *,
    task_id: int,
    outcome: str,
    reason_code: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_TASK_RECORD,
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
    policy: SupportsTaskRetentionPolicy,
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
