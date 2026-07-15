"""Engagement management service for archive/restore write workflows.

Scope:
- Own engagement state transitions for archive/restore operations.
- Enforce preconditions for engagement archive behavior.

Boundaries:
- No HTTP routing concerns; routers delegate write orchestration here.
- Read/query endpoints remain in engagement knowledge/query services.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task
from .access_service import get_engagement_in_tenant_or_404, get_owned_engagement_or_404


class EngagementManagementService:
    """Manage engagement archive/restore transitions with tenant-scope checks."""

    _RUNTIME_ACTIVE_ARCHIVE_BLOCK_STATUSES = (
        *TaskStatus.engagement_archive_block_statuses(),
        "waiting_for_human",
    )

    def __init__(self, db: Session):
        self.db = db

    def _engagement_has_runtime_active_tasks(self, *, engagement_id: int) -> bool:
        row = self.db.execute(
            select(Task.id)
            .where(Task.engagement_id == int(engagement_id))
            .where(Task.status.in_(self._RUNTIME_ACTIVE_ARCHIVE_BLOCK_STATUSES))
            .limit(1)
        ).scalar_one_or_none()
        return row is not None

    def archive_engagement(self, *, engagement_id: int, tenant_id: int, user_id: int | None = None) -> Engagement:
        """Archive an engagement when it has no runtime-active tasks."""
        if user_id is None:
            engagement = get_engagement_in_tenant_or_404(
                db=self.db,
                engagement_id=int(engagement_id),
                tenant_id=int(tenant_id),
                lock_for_update=True,
            )
        else:
            engagement = get_owned_engagement_or_404(
                db=self.db,
                engagement_id=int(engagement_id),
                user_id=int(user_id),
                tenant_id=int(tenant_id),
                lock_for_update=True,
            )
        if self._engagement_has_runtime_active_tasks(engagement_id=int(engagement.id)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot archive engagement while runtime-active tasks exist. "
                    "Stop/retire active tasks before archiving."
                ),
            )
        engagement.status = "archived"
        self.db.commit()
        self.db.refresh(engagement)
        return engagement

    def restore_engagement(self, *, engagement_id: int, tenant_id: int, user_id: int | None = None) -> Engagement:
        """Restore an engagement to active status."""
        if user_id is None:
            engagement = get_engagement_in_tenant_or_404(
                db=self.db,
                engagement_id=int(engagement_id),
                tenant_id=int(tenant_id),
            )
        else:
            engagement = get_owned_engagement_or_404(
                db=self.db,
                engagement_id=int(engagement_id),
                user_id=int(user_id),
                tenant_id=int(tenant_id),
            )
        engagement.status = "active"
        self.db.commit()
        self.db.refresh(engagement)
        return engagement
