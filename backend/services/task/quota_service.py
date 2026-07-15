"""Task concurrency quota counting service.

Responsibilities:
- Provide tenant-scoped and user-scoped active-task counts from PostgreSQL/SQLAlchemy.
- Centralize quota counting queries behind one service for admission logic reuse.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models import Task


class TaskQuotaService:
    """Count active tasks per tenant/user from the tasks table."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def count_active_for_tenant(self, tenant_id: int) -> int:
        """Return active task count for one tenant."""
        return self._count_active_tasks(tenant_id=int(tenant_id))

    def count_active_for_user(self, tenant_id: int, user_id: int) -> int:
        """Return active task count for one user within one tenant."""
        return self._count_active_tasks(tenant_id=int(tenant_id), user_id=int(user_id))

    def count_active_global(self) -> int:
        """Return active task count across all tenants (deployment-wide)."""
        return self._count_active_tasks()

    def _count_active_tasks(self, *, tenant_id: int | None = None, user_id: int | None = None) -> int:
        statuses = tuple(TaskStatus.active_task_statuses())
        stmt = select(func.count(Task.id)).where(Task.status.in_(statuses))
        if tenant_id is not None:
            stmt = stmt.where(Task.tenant_id == tenant_id)
        if user_id is not None:
            stmt = stmt.where(Task.user_id == user_id)
        return int(self._db.execute(stmt).scalar_one())

