"""Chat-owned retention executor for terminal task transcript rows.

This module deletes task-local chat transcript persistence after the owning
task is terminal and outside the tenant's effective transcript retention
window. It does not delete task records or execution provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import ChatMessage, ChatTurnEvent, ToolCall
from backend.models.core import Task
from backend.models.llm import LLMConversation
from backend.services.retention.contracts import (
    RETENTION_CLASS_TASK_TRANSCRIPT,
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


TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED = (
    "terminal_task_transcript_retention_expired"
)
ACTIVE_TASK_TRANSCRIPT_RETENTION_PROTECTED = "active_task_transcript_retention_protected"
RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED = "recent_task_transcript_retention_protected"


class SupportsChatTranscriptRetentionPolicy(Protocol):
    """Policy fields consumed by the chat transcript retention executor."""

    chat_transcript_retention_days_after_terminal: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class ChatTranscriptRetentionExecutor:
    """Run bounded terminal task transcript retention through the shared contract."""

    db: Session
    name: str = "chat.retention"
    retention_class: str = RETENTION_CLASS_TASK_TRANSCRIPT

    def run(
        self,
        *,
        policy: SupportsChatTranscriptRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally delete tenant-scoped terminal transcripts."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        cutoff = utc_now() - timedelta(
            days=_normalize_positive_int(
                policy.chat_transcript_retention_days_after_terminal,
                field_name="policy.chat_transcript_retention_days_after_terminal",
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
        decisions = [
            _task_transcript_decision(
                task_id=int(task.id),
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=_protected_reason(task=task, terminal_before=cutoff),
            )
            for task in protected_tasks
        ]

        for task in candidates:
            task_id = int(task.id)
            if run_mode == RETENTION_RUN_MODE_DRY_RUN:
                decisions.append(
                    _task_transcript_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_CANDIDATE,
                        reason_code=TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED,
                    )
                )
                continue

            deleted_count = _delete_task_transcript(
                self.db,
                tenant_id=scoped_tenant_id,
                task_id=task_id,
            )
            if deleted_count > 0:
                applied_count += 1
                decisions.append(
                    _task_transcript_decision(
                        task_id=task_id,
                        outcome=RETENTION_DECISION_APPLIED,
                        reason_code=TERMINAL_TASK_TRANSCRIPT_RETENTION_EXPIRED,
                    )
                )

        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_TASK_TRANSCRIPT,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=len(candidates) + len(protected_tasks),
                candidate_count=len(candidates),
                protected_count=len(protected_tasks),
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
            _has_transcript_rows(db, tenant_id=tenant_id),
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
            _has_transcript_rows(db, tenant_id=tenant_id),
        )
        .order_by(touched_at.asc(), Task.id.asc())
        .limit(limit)
        .all()
    )


def _has_transcript_rows(db: Session, *, tenant_id: int) -> object:
    return or_(
        db.query(ChatMessage.id)
        .filter(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.task_id == Task.id,
        )
        .exists(),
        db.query(ChatTurnEvent.id)
        .filter(
            ChatTurnEvent.tenant_id == tenant_id,
            ChatTurnEvent.task_id == Task.id,
        )
        .exists(),
        db.query(LLMConversation.id)
        .filter(
            LLMConversation.tenant_id == tenant_id,
            LLMConversation.task_id == Task.id,
        )
        .exists(),
    )


def _delete_task_transcript(
    db: Session,
    *,
    tenant_id: int,
    task_id: int,
) -> int:
    message_ids = [
        int(row[0])
        for row in (
            db.query(ChatMessage.id)
            .filter(
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.task_id == task_id,
            )
            .all()
        )
    ]
    deleted_count = 0
    deleted_count += int(
        db.query(ChatTurnEvent)
        .filter(
            ChatTurnEvent.tenant_id == tenant_id,
            ChatTurnEvent.task_id == task_id,
        )
        .delete(synchronize_session=False)
    )
    if message_ids:
        (
            db.query(ToolCall)
            .filter(
                ToolCall.tenant_id == tenant_id,
                ToolCall.chat_message_id.in_(message_ids),
            )
            .update({ToolCall.parent_tool_call_id: None}, synchronize_session=False)
        )
        deleted_count += int(
            db.query(ToolCall)
            .filter(
                ToolCall.tenant_id == tenant_id,
                ToolCall.chat_message_id.in_(message_ids),
            )
            .delete(synchronize_session=False)
        )
        (
            db.query(ChatMessage)
            .filter(
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.task_id == task_id,
            )
            .update(
                {
                    ChatMessage.parent_message_id: None,
                    ChatMessage.latest_child_message_id: None,
                },
                synchronize_session=False,
            )
        )
        deleted_count += int(
            db.query(ChatMessage)
            .filter(
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.task_id == task_id,
            )
            .delete(synchronize_session=False)
        )
    deleted_count += int(
        db.query(LLMConversation)
        .filter(
            LLMConversation.tenant_id == tenant_id,
            LLMConversation.task_id == task_id,
        )
        .delete(synchronize_session=False)
    )
    return deleted_count


def _protected_reason(*, task: Task, terminal_before: object) -> str:
    terminal_at = _task_terminal_at(task)
    if (
        task.status in TaskStatus.get_terminal_statuses()
        and terminal_at is not None
        and terminal_at >= terminal_before
    ):
        return RECENT_TASK_TRANSCRIPT_RETENTION_PROTECTED
    return ACTIVE_TASK_TRANSCRIPT_RETENTION_PROTECTED


def _task_terminal_at(task: Task) -> object | None:
    return task.completed_at or task.stopped_at or task.updated_at or task.created_at


def _terminal_at_expression() -> object:
    return func.coalesce(
        Task.completed_at,
        Task.stopped_at,
        Task.updated_at,
        Task.created_at,
    )


def _task_transcript_decision(
    *,
    task_id: int,
    outcome: str,
    reason_code: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_TASK_TRANSCRIPT,
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
    policy: SupportsChatTranscriptRetentionPolicy,
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
