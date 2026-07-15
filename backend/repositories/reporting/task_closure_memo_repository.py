"""Task-closure memo persistence for scoped reporting data.

This module owns only tenant/user/engagement/task-scoped memo and supporting
task queries; report artifacts, jobs, worker queues, and retention are excluded.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func

from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task
from backend.models.reporting import TaskClosureMemo
from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.services.reporting.contracts import (
    MEMO_MODE_SUPPORTED,
    MEMO_STATUS_FAILED,
    MEMO_STATUS_PREPARING,
    MEMO_STATUS_READY,
    TASK_CLOSURE_MEMO_SCHEMA_VERSION,
)


class TaskClosureMemoRepository(ReportingRepositoryBase):
    """Persist scoped task-closure memos and their supporting task reads."""

    def get_current_ready_memo(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> TaskClosureMemo | None:
        """Return the current ready memo for a tenant/user-owned task."""

        return (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
                TaskClosureMemo.status == MEMO_STATUS_READY,
                TaskClosureMemo.is_current.is_(True),
            )
            .order_by(TaskClosureMemo.version.desc(), TaskClosureMemo.created_at.desc())
            .one_or_none()
        )

    def list_selected_current_ready_memos(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        selected_task_memo_ids: Sequence[str | uuid.UUID],
    ) -> list[TaskClosureMemo]:
        """Return selected current ready memos in first-requested order."""

        normalized_memo_ids, _ = self.normalize_selected_memo_ids(
            selected_task_memo_ids
        )
        if not normalized_memo_ids:
            return []

        rows = (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.id.in_(normalized_memo_ids),
                TaskClosureMemo.status == MEMO_STATUS_READY,
                TaskClosureMemo.is_current.is_(True),
            )
            .all()
        )
        rows_by_id = {row.id: row for row in rows}
        return [
            rows_by_id[memo_id]
            for memo_id in normalized_memo_ids
            if memo_id in rows_by_id
        ]

    def get_selected_memo_tasks(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        selected_task_memo_ids: Sequence[str | uuid.UUID],
    ) -> list[tuple[uuid.UUID, Task]]:
        """Return stopped tasks joined from selected current ready memo IDs."""

        normalized_memo_ids, _ = self.normalize_selected_memo_ids(
            selected_task_memo_ids
        )
        if not normalized_memo_ids:
            return []

        rows = (
            self.db.query(TaskClosureMemo.id, Task)
            .join(Task, TaskClosureMemo.task_id == Task.id)
            .join(Engagement, Task.engagement_id == Engagement.id)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.id.in_(normalized_memo_ids),
                TaskClosureMemo.status == MEMO_STATUS_READY,
                TaskClosureMemo.is_current.is_(True),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
                Task.status == TaskStatus.STOPPED.value,
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Engagement.id == int(engagement_id),
            )
            .all()
        )
        tasks_by_memo_id = {memo_id: task for memo_id, task in rows}
        return [
            (memo_id, tasks_by_memo_id[memo_id])
            for memo_id in normalized_memo_ids
            if memo_id in tasks_by_memo_id
        ]

    def get_task_for_memo_preparation(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> Task | None:
        """Return a task only when it belongs to the scoped engagement owner."""

        return (
            self.db.query(Task)
            .join(Engagement, Task.engagement_id == Engagement.id)
            .filter(
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
                Task.id == int(task_id),
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Engagement.id == int(engagement_id),
            )
            .one_or_none()
        )

    def get_memo_by_id(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        memo_id: str | uuid.UUID,
    ) -> TaskClosureMemo | None:
        """Return one memo constrained by tenant/user/engagement/task identity."""

        parsed_memo_id = self._parse_uuid(memo_id)
        if parsed_memo_id is None:
            return None

        return (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
                TaskClosureMemo.id == parsed_memo_id,
            )
            .one_or_none()
        )

    def get_latest_memo_attempt(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> TaskClosureMemo | None:
        """Return the latest memo attempt for a tenant/user-owned task."""

        return (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
            )
            .order_by(TaskClosureMemo.version.desc(), TaskClosureMemo.created_at.desc())
            .first()
        )

    def get_preparing_memo_attempt(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> TaskClosureMemo | None:
        """Return the newest scoped in-flight memo attempt for one task."""

        return (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
                TaskClosureMemo.status == MEMO_STATUS_PREPARING,
            )
            .order_by(TaskClosureMemo.created_at.desc(), TaskClosureMemo.version.desc())
            .first()
        )

    def mark_stale_preparing_memos_failed(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        stale_before: datetime,
        error_message: str,
    ) -> int:
        """Fail scoped preparing memo attempts older than the supplied cutoff."""

        updated = (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
                TaskClosureMemo.status == MEMO_STATUS_PREPARING,
                TaskClosureMemo.created_at < stale_before,
            )
            .update(
                {
                    TaskClosureMemo.status: MEMO_STATUS_FAILED,
                    TaskClosureMemo.is_current: False,
                    TaskClosureMemo.error_message: str(error_message),
                    TaskClosureMemo.updated_at: datetime.now(UTC),
                },
                synchronize_session="fetch",
            )
        )
        self.db.flush()
        return int(updated)

    def list_memo_history_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskClosureMemo]:
        """Return memo attempts for one tenant/user-owned task."""

        return (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
            )
            .order_by(TaskClosureMemo.version.desc(), TaskClosureMemo.created_at.desc())
            .offset(max(0, int(offset)))
            .limit(max(1, int(limit)))
            .all()
        )

    def next_memo_version(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        """Return the next memo version for one scoped task."""

        latest_version = (
            self.db.query(func.max(TaskClosureMemo.version))
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
            )
            .scalar()
        )
        return int(latest_version or 0) + 1

    def create_memo_attempt(
        self,
        *,
        tenant_id: int,
        user_id: int,
        created_by_user_id: int,
        engagement_id: int,
        task_id: int,
        version: int,
        memo_mode: str = MEMO_MODE_SUPPORTED,
        source_watermark: dict[str, Any] | None = None,
        memo: dict[str, Any] | None = None,
        generation_metadata: dict[str, Any] | None = None,
        status: str = MEMO_STATUS_PREPARING,
        error_message: str | None = None,
    ) -> TaskClosureMemo:
        """Insert a scoped memo attempt and return the created row."""

        row = TaskClosureMemo(
            schema_version=TASK_CLOSURE_MEMO_SCHEMA_VERSION,
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            created_by_user_id=int(created_by_user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
            version=int(version),
            is_current=False,
            status=str(status),
            memo_mode=str(memo_mode),
            source_watermark=dict(source_watermark or {}),
            memo=dict(memo or {}),
            generation_metadata=dict(generation_metadata or {}),
            error_message=error_message,
        )
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        return row

    def mark_memo_ready(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        memo_id: str | uuid.UUID,
        memo: dict[str, Any],
        source_watermark: dict[str, Any],
        generation_metadata: dict[str, Any] | None = None,
        generated_at: datetime | None = None,
        memo_mode: str | None = None,
    ) -> TaskClosureMemo | None:
        """Mark one scoped memo attempt ready without changing other rows."""

        row = self.get_memo_by_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            memo_id=memo_id,
        )
        if row is None:
            return None

        row.status = MEMO_STATUS_READY
        row.is_current = True
        if memo_mode is not None:
            row.memo_mode = str(memo_mode)
        row.memo = dict(memo)
        row.source_watermark = dict(source_watermark)
        row.generation_metadata = {
            **dict(row.generation_metadata or {}),
            **dict(generation_metadata or {}),
        }
        row.error_message = None
        row.generated_at = generated_at
        self.db.flush()
        self.db.refresh(row)
        return row

    def mark_memo_failed(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        memo_id: str | uuid.UUID,
        error_message: str,
        generation_metadata: dict[str, Any] | None = None,
        source_watermark: dict[str, Any] | None = None,
    ) -> TaskClosureMemo | None:
        """Mark one scoped memo attempt failed without changing ready memos."""

        row = self.get_memo_by_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            memo_id=memo_id,
        )
        if row is None:
            return None

        row.status = MEMO_STATUS_FAILED
        row.is_current = False
        row.generation_metadata = dict(generation_metadata or {})
        if source_watermark is not None:
            row.source_watermark = dict(source_watermark)
        row.error_message = str(error_message)
        self.db.flush()
        self.db.refresh(row)
        return row

    def clear_current_ready_memos_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        """Clear current pointers only on ready memos for one scoped task."""

        updated = (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.user_id == int(user_id),
                TaskClosureMemo.engagement_id == int(engagement_id),
                TaskClosureMemo.task_id == int(task_id),
                TaskClosureMemo.status == MEMO_STATUS_READY,
                TaskClosureMemo.is_current.is_(True),
            )
            .update({TaskClosureMemo.is_current: False}, synchronize_session="fetch")
        )
        self.db.flush()
        return int(updated)

    def list_memos_for_tasks(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_ids: Sequence[int],
        current_ready_only: bool = False,
    ) -> list[TaskClosureMemo]:
        """Return memos for tenant/user-owned task IDs within one engagement."""

        normalized_task_ids = [int(task_id) for task_id in task_ids]
        if not normalized_task_ids:
            return []

        query = self.db.query(TaskClosureMemo).filter(
            TaskClosureMemo.tenant_id == int(tenant_id),
            TaskClosureMemo.user_id == int(user_id),
            TaskClosureMemo.engagement_id == int(engagement_id),
            TaskClosureMemo.task_id.in_(normalized_task_ids),
        )
        if current_ready_only:
            query = query.filter(
                TaskClosureMemo.status == MEMO_STATUS_READY,
                TaskClosureMemo.is_current.is_(True),
            )

        return query.order_by(
            TaskClosureMemo.task_id.asc(), TaskClosureMemo.version.desc()
        ).all()

