"""Resolve engagement identity for task lifecycle operations.

Scope:
- Decide which engagement a new task should attach to.

Responsibilities:
- Validate explicit engagement references within the task tenant boundary.
- Create a default engagement when task creation omits one.

Boundary:
- This service only resolves/creates engagement identity for task flows.
- It does not expose engagement management APIs.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Engagement


class EngagementService:
    """Resolve or create an engagement for task creation in one authority boundary."""

    def __init__(self, db: Session):
        self.db = db

    def resolve_for_task_creation(
        self,
        *,
        user_id: int,
        task_name: str,
        task_description: str | None,
        requested_engagement_id: int | None,
        expected_tenant_id: int | None = None,
    ) -> Engagement:
        """Return the authoritative engagement to attach to a newly created task."""
        if requested_engagement_id is not None:
            engagement = self.db.execute(
                select(Engagement)
                .where(Engagement.id == requested_engagement_id)
                .with_for_update()
            ).scalar_one_or_none()
            if engagement is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Engagement not found",
                )
            if expected_tenant_id is None and int(engagement.user_id) != int(user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Engagement does not belong to the current user",
                )
            if str(getattr(engagement, "status", "active")) == "archived":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot create task in archived engagement. "
                        "Restore the engagement first."
                    ),
                )
            if expected_tenant_id is not None and int(engagement.tenant_id) != int(expected_tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Engagement tenant does not match task tenant boundary. "
                        "Cross-tenant task attachment is not allowed."
                    ),
                )
            return engagement

        default_name = (task_name or "").strip() or "Untitled engagement"
        default_description = (task_description or "").strip() or None
        engagement = Engagement(
            user_id=user_id,
            tenant_id=expected_tenant_id,
            name=default_name,
            description=default_description,
            status="active",
        )
        self.db.add(engagement)
        return engagement
