"""Terminal session REST routes for docker-backed task terminals.

Responsibilities:
- Expose terminal session list/create/close REST endpoints.
- Delegate terminal session lifecycle operations to terminal session manager.

Boundary:
- No websocket stream handling.
- No docker logs/status REST endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models.core import Task, User
from backend.services.tenant.authorization import ACTION_TASK_CONTROL
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context
from backend.routers.tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from backend.services.terminal_session_manager import terminal_session_manager

router = APIRouter()


@router.get("/terminal/sessions")
async def get_terminal_sessions(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get active terminal sessions for the current user in the active tenant.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
        user_id = int(current_user.id) if hasattr(current_user, "id") else None
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid user")
        sessions = terminal_session_manager.get_user_sessions(user_id)
        if not sessions:
            return {"sessions": [], "total": 0}

        session_task_ids = {int(session.task_id) for session in sessions}
        tenant_task_ids = set(
            db.execute(
                select(Task.id).where(
                    Task.tenant_id == int(tenant_context.tenant_id),
                    Task.id.in_(session_task_ids),
                )
            )
            .scalars()
            .all()
        )
        tenant_sessions = [session for session in sessions if int(session.task_id) in tenant_task_ids]
        return {
            "sessions": [session.to_dict() for session in tenant_sessions],
            "total": len(tenant_sessions),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get terminal sessions: {str(e)}")


@router.post("/terminal/sessions/{task_id}")
async def create_terminal_session(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Create a new terminal session for the specified task.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        user_id = int(current_user.id) if hasattr(current_user, "id") else None
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid user")
        session = await terminal_session_manager.create_session(task_id, user_id, authorized_task=task)
        if session:
            return {
                "success": True,
                "session": session.to_dict(),
            }
        raise HTTPException(
            status_code=400,
            detail="Failed to create terminal session - container not accessible or session limit reached",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create terminal session: {str(e)}")


@router.delete("/terminal/sessions/{session_id}")
async def close_terminal_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Close a terminal session.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
        session = terminal_session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        get_tenant_task_or_404(db=db, task_id=int(session.task_id), tenant_context=tenant_context)

        success = await terminal_session_manager.close_session(session_id)
        return {"success": success}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to close terminal session: {str(e)}")
