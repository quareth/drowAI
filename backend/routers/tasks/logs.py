"""Task system log retrieval routes.

Responsibilities:
- Expose ordered system log retrieval endpoint for a task.
"""

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import SystemLog, User
from ...services.tenant.authorization import ACTION_TASK_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from .deps import enforce_tenant_action, get_tenant_task_or_404

router = APIRouter()


@router.get("/{task_id}/logs", response_model=List[dict])
def get_task_logs(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get system reasoning logs for a specific task."""
    _ = current_user
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )

    logs_result = db.execute(
        select(SystemLog)
        .where(SystemLog.task_id == task_id)
        .order_by(SystemLog.sequence.asc())
    )
    logs = logs_result.scalars().all()

    return [
        {
            "id": log.id,
            "task_id": log.task_id,
            "sequence": log.sequence,
            "type": log.type,
            "content": log.content,
            "metadata": log.log_metadata,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        }
        for log in logs
    ]
