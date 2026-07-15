"""Task interrupt inspection and graph-resume routes.

Responsibilities:
- Expose interrupt state lookup endpoint.
- Expose graph resume endpoint and delegate orchestration to service layer.
- Expose checkpoint retry endpoint for retryable failed graph turns.
"""

import asyncio
import time
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...services.langgraph_chat.checkpoint.hitl_schemas import HITLResumeResponse
from ...services.langgraph_chat.checkpoint.interrupt_state_service import get_interrupt_state_service
from ...services.task.graph_retry_service import TaskGraphRetryService
from ...services.task.interrupt_service import TaskInterruptService
from ...services.tenant.authorization import ACTION_CHAT_READ, ACTION_CHAT_RETRY
from ...services.tenant.context import TenantContextService, TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from .deps import enforce_tenant_action

router = APIRouter()


class ResumeRequest(BaseModel):
    """Request body for resuming interrupted graph execution."""

    interrupt_id: str = Field(
        ...,
        description="Canonical interrupt ID from the interrupt snapshot (GET /interrupt or graph_interrupt event).",
    )
    interrupt_type: Literal["tool_approval", "plan_review", "clarify_request"]
    graph_name: Optional[str] = Field(
        default=None,
        description="Graph that was interrupted. If not provided, retrieved from stored metadata.",
    )
    response: HITLResumeResponse


class TaskInterruptSnapshotResponse(BaseModel):
    """Snapshot response for task interrupt hydration."""

    has_interrupt: bool
    task_id: int
    task_missing: Optional[bool] = None
    thread_id: Optional[str] = None
    graph_name: Optional[str] = None
    interrupt_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    interrupt_type: Optional[
        Literal["tool_approval", "plan_review", "clarify_request"]
    ] = None
    payload: Optional[Dict[str, Any]] = None
    resumable: Optional[bool] = None


class RetryRequest(BaseModel):
    """Request body for retrying a failed graph turn from checkpoint."""

    turn_id: str = Field(
        ...,
        description="Stable turn identifier for the failed assistant turn.",
    )
    retry_mode: Literal["checkpoint"] = Field(
        default="checkpoint",
        description="Retry mode. MVP supports checkpoint only.",
    )
    graph_name: Optional[str] = Field(
        default=None,
        description="Optional graph hint. Canonical graph is resolved from workflow metadata.",
    )


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


@router.get("/{task_id}/interrupt", response_model=TaskInterruptSnapshotResponse)
async def get_task_interrupt(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get pending interrupt state for a task from ticket authority."""
    resolved_tenant_context = _resolve_tenant_context(
        tenant_context=tenant_context,
        db=db,
        current_user=current_user,
    )
    enforce_tenant_action(tenant_context=resolved_tenant_context, action=ACTION_CHAT_READ)
    interrupt_service = TaskInterruptService(db)
    return await interrupt_service.get_task_interrupt(
        task_id=task_id,
        user_id=current_user.id,
        interrupt_service=get_interrupt_state_service(),
        tenant_id=resolved_tenant_context.tenant_id,
    )


@router.post("/{task_id}/graph/resume")
async def resume_graph_execution(
    task_id: int,
    request: ResumeRequest,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Resume a graph that is waiting at an interrupt point."""
    from backend.services.langgraph_chat.execution.turn_service import run_resume_generation
    resolved_tenant_context = _resolve_tenant_context(
        tenant_context=tenant_context,
        db=db,
        current_user=current_user,
    )
    enforce_tenant_action(tenant_context=resolved_tenant_context, action=ACTION_CHAT_RETRY)

    if request.interrupt_type == "clarify_request":
        if request.response.action != "answer":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="clarify_request resumes must use action='answer'.",
            )
        answers = request.response.answers
        has_valid_answer = isinstance(answers, dict) and any(
            str(key).strip() and str(value).strip()
            for key, value in answers.items()
        )
        if not has_valid_answer:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="clarify_request resumes require non-empty answers.",
            )

    approval_received_at = time.perf_counter()
    interrupt_service = TaskInterruptService(db)
    return await interrupt_service.resume_graph_execution(
        task_id=task_id,
        user_id=current_user.id,
        interrupt_id=request.interrupt_id,
        graph_name=request.graph_name,
        response_payload=request.response.model_dump(),
        create_task_fn=asyncio.create_task,
        run_resume_generation=run_resume_generation,
        approval_received_at=approval_received_at,
        tenant_id=resolved_tenant_context.tenant_id,
    )


@router.post("/{task_id}/graph/retry")
async def retry_graph_execution(
    task_id: int,
    request: RetryRequest,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Retry a failed retryable graph turn from the latest stable checkpoint."""
    from backend.services.langgraph_chat.execution.turn_service import (
        run_checkpoint_retry_generation,
    )

    resolved_tenant_context = _resolve_tenant_context(
        tenant_context=tenant_context,
        db=db,
        current_user=current_user,
    )
    enforce_tenant_action(tenant_context=resolved_tenant_context, action=ACTION_CHAT_RETRY)
    retry_service = TaskGraphRetryService(db)
    return await retry_service.retry_graph_execution(
        task_id=task_id,
        user_id=current_user.id,
        turn_id=request.turn_id,
        retry_mode=request.retry_mode,
        graph_name=request.graph_name,
        create_task_fn=asyncio.create_task,
        run_checkpoint_retry_generation=run_checkpoint_retry_generation,
        tenant_id=resolved_tenant_context.tenant_id,
    )
