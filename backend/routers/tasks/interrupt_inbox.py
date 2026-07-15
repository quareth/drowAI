"""Task interrupt inbox routes for cross-task pending approvals."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...services.task.interrupt_service import TaskInterruptService
from ...services.tenant.authorization import ACTION_CHAT_RETRY
from ...services.tenant.context import TenantContextService, TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from .deps import enforce_tenant_action

router = APIRouter()


class InterruptInboxItem(BaseModel):
    task_id: int
    task_name: Optional[str] = None
    interrupt_id: str
    interrupt_type: str
    graph_name: str
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    turn_sequence: Optional[int] = None
    checkpoint_id: Optional[str] = None
    updated_at: Optional[str] = None
    created_at: Optional[str] = None


class InterruptInboxResponse(BaseModel):
    items: List[InterruptInboxItem]
    count: int


def _resolve_tenant_context(
    *,
    tenant_context: object,
    db: Session,
    current_user: User,
) -> TenantRequestContext:
    """Return resolved tenant context for FastAPI and direct function-call tests."""
    if isinstance(tenant_context, TenantRequestContext):
        return tenant_context
    resolved = TenantContextService(db).resolve_for_user(user_id=int(current_user.id))
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Explicit tenant selection is required for this user.",
        )
    return resolved


@router.get("/interrupts/inbox", response_model=InterruptInboxResponse)
async def list_interrupt_inbox(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """List pending interrupts across tasks scoped to the active tenant context."""
    resolved_tenant_context = _resolve_tenant_context(
        tenant_context=tenant_context,
        db=db,
        current_user=current_user,
    )
    enforce_tenant_action(tenant_context=resolved_tenant_context, action=ACTION_CHAT_RETRY)
    svc = TaskInterruptService(db)
    items = svc.list_pending_interrupts_for_user(
        current_user.id,
        tenant_id=resolved_tenant_context.tenant_id,
    )
    return InterruptInboxResponse(items=items, count=len(items))
