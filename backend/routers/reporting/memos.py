"""Expose task closure memo preparation and read endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...routers.tasks.deps import enforce_tenant_action
from ...schemas.reporting import (
    TaskClosureMemoHistoryResponse,
    TaskClosureMemoPrepareRequest,
    TaskClosureMemoPrepareResponse,
    TaskClosureMemoReadResponse,
)
from ...services.reporting.contracts import (
    TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND,
    TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    TASK_MEMO_ERROR_NO_USEFUL_RUNTIME_EXECUTION,
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
    TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
    TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    TASK_MEMO_ERROR_TASK_NOT_FOUND,
    TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
    TASK_MEMO_ERROR_TASK_NOT_STOPPED,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
)
from ...services.reporting.task_memo_service import (
    TaskMemoService,
    TaskMemoServiceError,
)
from ...services.tenant.authorization import ACTION_REPORT_READ, ACTION_REPORT_WRITE
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context

router = APIRouter()

_NOT_FOUND_REASONS = {
    TASK_MEMO_ERROR_TASK_NOT_FOUND,
    TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND,
}
_CONFLICT_REASONS = {
    TASK_MEMO_ERROR_TASK_NOT_STOPPED,
    TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    TASK_MEMO_ERROR_NO_USEFUL_RUNTIME_EXECUTION,
    TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
    TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
}
_UNPROCESSABLE_REASONS = {
    TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
}


@router.post(
    "/tasks/{task_id}/memo/prepare",
    response_model=TaskClosureMemoPrepareResponse,
)
async def prepare_task_closure_memo(
    task_id: int,
    request: TaskClosureMemoPrepareRequest | None = None,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TaskClosureMemoPrepareResponse:
    """Prepare a task closure memo for a user-owned task."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_WRITE)
    prepare_request = request or TaskClosureMemoPrepareRequest()
    try:
        memo = await TaskMemoService(db).prepare_task_memo(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            task_id=int(task_id),
            regenerate=prepare_request.regenerate,
        )
    except TaskMemoServiceError as exc:
        _raise_memo_service_error(exc)
    return TaskClosureMemoPrepareResponse(
        task_id=int(task_id),
        memo=_memo_response(memo),
    )


@router.get(
    "/tasks/{task_id}/memo/current",
    response_model=TaskClosureMemoReadResponse,
)
async def get_current_task_closure_memo(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TaskClosureMemoReadResponse:
    """Return the current ready task closure memo for a user-owned task."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    try:
        memo = TaskMemoService(db).get_current_task_memo(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            task_id=int(task_id),
        )
    except TaskMemoServiceError as exc:
        _raise_memo_service_error(exc)
    if memo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task closure memo not found",
        )
    return _memo_response(memo)


@router.get(
    "/tasks/{task_id}/memo/history",
    response_model=TaskClosureMemoHistoryResponse,
)
async def list_task_closure_memo_history(
    task_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TaskClosureMemoHistoryResponse:
    """Return task closure memo attempts for a user-owned task."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    try:
        items = TaskMemoService(db).list_task_memo_history(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            task_id=int(task_id),
            limit=int(limit),
            offset=int(offset),
        )
    except TaskMemoServiceError as exc:
        _raise_memo_service_error(exc)
    return TaskClosureMemoHistoryResponse(
        task_id=int(task_id),
        items=[_memo_response(item) for item in items],
    )


def _memo_response(memo: object) -> TaskClosureMemoReadResponse:
    return TaskClosureMemoReadResponse.model_validate(memo)


def _raise_memo_service_error(exc: TaskMemoServiceError) -> None:
    if exc.reason in _NOT_FOUND_REASONS:
        status_code = status.HTTP_404_NOT_FOUND
        detail = "Task closure memo source not found"
    elif exc.reason == TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS:
        status_code = status.HTTP_409_CONFLICT
        detail = "Task memo preparation is already in progress."
    elif exc.reason in _CONFLICT_REASONS:
        status_code = status.HTTP_409_CONFLICT
        detail = "Task is not eligible for memo preparation"
    elif exc.reason in _UNPROCESSABLE_REASONS:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        detail = "Task closure memo request could not be processed"
    else:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = "Task closure memo preparation failed"
    raise HTTPException(status_code=status_code, detail=detail) from exc


__all__ = ["router"]
