"""Shared engagement access helpers.

Responsibilities:
- Provide tenant-aware engagement lookup helpers for router and service layers.
- Keep the 404 contract consistent across engagement-related code paths.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Engagement


def get_engagement_in_tenant(
    db: Session,
    engagement_id: int,
    *,
    tenant_id: int,
    lock_for_update: bool = False,
) -> Engagement | None:
    """Return any engagement in a tenant for internal/admin-only callers."""

    stmt = select(Engagement).where(
        Engagement.id == int(engagement_id),
        Engagement.tenant_id == int(tenant_id),
    )
    if lock_for_update:
        stmt = stmt.with_for_update()
    result = db.execute(stmt)
    return result.scalar_one_or_none()


def get_engagement_in_tenant_or_404(
    db: Session,
    engagement_id: int,
    *,
    tenant_id: int,
    lock_for_update: bool = False,
) -> Engagement:
    """Return any engagement in a tenant for internal/admin-only callers or raise 404."""

    engagement = get_engagement_in_tenant(
        db=db,
        engagement_id=engagement_id,
        tenant_id=tenant_id,
        lock_for_update=lock_for_update,
    )
    if not engagement:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Engagement not found")
    return engagement


def get_owned_engagement(
    db: Session,
    engagement_id: int,
    user_id: int,
    *,
    tenant_id: int,
    lock_for_update: bool = False,
) -> Engagement | None:
    """Return a user-owned engagement constrained to an explicit tenant."""

    stmt = select(Engagement).where(
        Engagement.id == int(engagement_id),
        Engagement.user_id == int(user_id),
        Engagement.tenant_id == int(tenant_id),
    )
    if lock_for_update:
        stmt = stmt.with_for_update()
    result = db.execute(stmt)
    return result.scalar_one_or_none()


def get_owned_engagement_or_404(
    db: Session,
    engagement_id: int,
    user_id: int,
    *,
    tenant_id: int,
    lock_for_update: bool = False,
) -> Engagement:
    """Return a user-owned engagement in an explicit tenant or raise 404."""

    engagement = get_owned_engagement(
        db=db,
        engagement_id=engagement_id,
        user_id=user_id,
        tenant_id=tenant_id,
        lock_for_update=lock_for_update,
    )
    if not engagement:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Engagement not found")
    return engagement
