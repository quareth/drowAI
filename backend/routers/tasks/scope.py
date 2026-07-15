"""Task scope parsing and inspection routes.

Responsibilities:
- Expose scope parsing endpoint for a task workspace.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...services.tenant.authorization import ACTION_TASK_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ...services.workspace.runtime_workspace_query_service import TaskWorkspaceQueryService
from .deps import enforce_tenant_action, get_tenant_task_or_404

from agent.planner import ScopeParser

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{task_id}/scope", response_model=dict)
async def get_task_scope(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get parsed scope data for a task."""
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
        task = get_tenant_task_or_404(
            db=db,
            task_id=task_id,
            tenant_context=tenant_context,
        )
        query_service = TaskWorkspaceQueryService(db)
        try:
            scope_markdown = await query_service.read_scope_markdown(
                task=task,
                user_id=current_user.id,
            )
        except HTTPException as provider_error:
            return {
                "success": False,
                "error": f"Scope file not available | {provider_error.detail}",
                "task_id": task_id,
                "task_name": task.name,
                "raw_scope": task.scope,
            }

        if not scope_markdown:
            return {
                "success": False,
                "error": "Scope file not found",
                "task_id": task_id,
                "task_name": task.name,
                "raw_scope": task.scope,
            }

        try:
            scope_parser = ScopeParser()
            parsed_scope = scope_parser.parse_markdown_content(scope_markdown)
            return {
                "success": True,
                "task_id": task_id,
                "task_name": task.name,
                "parsed_scope": parsed_scope.to_dict(),
                "validation_errors": scope_parser.get_validation_errors(),
                "warnings": scope_parser.get_warnings(),
                "has_errors": scope_parser.has_errors(),
            }
        except Exception as parse_error:
            logger.error("Error parsing scope file for task %s: %s", task_id, parse_error)
            return {
                "success": False,
                "error": f"Failed to parse scope file: {str(parse_error)}",
                "task_id": task_id,
                "task_name": task.name,
                "raw_scope": task.scope,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting scope for task {task_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve scope data",
        )
