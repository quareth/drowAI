"""Shared task access helpers.

Responsibilities:
- Provide tenant/user-aware task lookup helpers for router and service layers.
- Keep the 404 contract consistent across task-related code paths.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session, joinedload

from ...models import Engagement, Task


def get_task_in_tenant(
    db: Session,
    task_id: int,
    *,
    tenant_id: int,
) -> Task | None:
    """Return any task in a tenant for internal/admin-only callers."""
    result = db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.tenant_id == int(tenant_id),
        )
    )
    return result.scalar_one_or_none()


def get_task_in_tenant_or_404(
    db: Session,
    task_id: int,
    *,
    tenant_id: int,
) -> Task:
    """Return any task in a tenant for internal/admin-only callers or raise 404."""
    task = get_task_in_tenant(
        db=db,
        task_id=task_id,
        tenant_id=tenant_id,
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def get_task_with_engagement_in_tenant(
    db: Session,
    task_id: int,
    *,
    tenant_id: int,
) -> Task | None:
    """Return any tenant task with engagement for internal/admin-only callers."""
    resolved_tenant_id = int(tenant_id)
    result = db.execute(
        select(Task)
        .options(joinedload(Task.engagement))
        .where(
            Task.id == task_id,
            Task.tenant_id == resolved_tenant_id,
            or_(
                Task.engagement_id.is_(None),
                Task.engagement.has(Engagement.tenant_id == resolved_tenant_id),
            ),
        )
    )
    return result.unique().scalars().one_or_none()


def get_task_with_engagement_in_tenant_or_404(
    db: Session,
    task_id: int,
    *,
    tenant_id: int,
) -> Task:
    """Return any tenant task with engagement for internal/admin-only callers or raise 404."""
    task = get_task_with_engagement_in_tenant(
        db=db,
        task_id=task_id,
        tenant_id=tenant_id,
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def list_tenant_tasks_for_user(
    db: Session,
    *,
    user_id: int,
    tenant_id: int,
) -> list[Task]:
    """Return tasks owned by a user inside the resolved tenant."""
    result = db.execute(
        select(Task)
        .options(joinedload(Task.engagement))
        .where(
            Task.user_id == int(user_id),
            Task.tenant_id == int(tenant_id),
        )
        .order_by(desc(Task.created_at))
    )
    return list(result.unique().scalars().all())


def get_tenant_task(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task | None:
    """Return a user-owned task constrained to an explicit tenant."""
    result = db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.user_id == user_id,
            Task.tenant_id == int(tenant_id),
        )
    )
    return result.scalar_one_or_none()


def get_tenant_task_or_404(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task:
    """Return a user-owned tenant task or raise HTTP 404."""
    task = get_tenant_task(
        db=db,
        task_id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def get_tenant_task_with_engagement(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task | None:
    """Return user-owned tenant task with engagement eager-loaded."""
    resolved_tenant_id = int(tenant_id)
    result = db.execute(
        select(Task)
        .options(joinedload(Task.engagement))
        .where(
            Task.id == task_id,
            Task.user_id == user_id,
            Task.tenant_id == resolved_tenant_id,
            or_(
                Task.engagement_id.is_(None),
                Task.engagement.has(Engagement.tenant_id == resolved_tenant_id),
            ),
        )
    )
    return result.unique().scalars().one_or_none()


def get_tenant_task_with_engagement_or_404(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task:
    """Return tenant task with engagement eager-loaded or raise HTTP 404."""
    task = get_tenant_task_with_engagement(
        db=db,
        task_id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def get_owned_task(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task | None:
    """Return a task owned by the user in an explicit tenant."""
    return get_tenant_task(db=db, task_id=task_id, user_id=user_id, tenant_id=tenant_id)


def get_owned_task_or_404(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task:
    """Return a task owned by the user in an explicit tenant or raise HTTP 404."""
    task = get_owned_task(db=db, task_id=task_id, user_id=user_id, tenant_id=tenant_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def get_owned_task_with_engagement(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task | None:
    """Return an owned task with engagement eager-loaded in an explicit tenant."""
    return get_tenant_task_with_engagement(
        db=db,
        task_id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )


def get_owned_task_with_engagement_or_404(
    db: Session,
    task_id: int,
    user_id: int,
    *,
    tenant_id: int,
) -> Task:
    """Return an owned task with engagement eager-loaded in an explicit tenant or raise HTTP 404."""
    task = get_owned_task_with_engagement(
        db=db,
        task_id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task
