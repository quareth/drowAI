"""Shared dependencies/helpers for task route modules.

Responsibilities:
- Resolve user-owned tenant tasks with consistent 404 contracts.
- Enforce centralized tenant action authorization for task routes.
- Centralize file browser exception-to-HTTP mapping.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspacePathError,
)

from ...models import Task
from ...services.task.access_service import (
    get_task_in_tenant_or_404 as resolve_tenant_task_or_404,
    get_owned_task_or_404 as resolve_owned_task_or_404,
    get_tenant_task_with_engagement_or_404 as resolve_owned_task_with_engagement_or_404,
)
from ...services.tenant.authorization import decide_action
from ...services.tenant.context import TenantRequestContext


def enforce_tenant_action(
    *,
    tenant_context: TenantRequestContext,
    action: str,
    detail: str | None = None,
) -> None:
    """Fail closed when tenant role is not allowed to execute an action."""

    decision = decide_action(role=tenant_context.role, action=action)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail or f"Tenant policy denied action '{action}'.",
    )


def get_tenant_task_or_404(
    *,
    db: Session,
    task_id: int,
    tenant_context: TenantRequestContext,
) -> Task:
    """Fetch a user-owned task in the active tenant or raise HTTP 404."""

    return resolve_owned_task_or_404(
        db=db,
        task_id=task_id,
        user_id=int(tenant_context.user_id),
        tenant_id=int(tenant_context.tenant_id),
    )


def get_tenant_task_with_engagement_or_404(
    *,
    db: Session,
    task_id: int,
    tenant_context: TenantRequestContext,
) -> Task:
    """Fetch a user-owned task with engagement in the active tenant or raise 404."""

    return resolve_owned_task_with_engagement_or_404(
        db=db,
        task_id=task_id,
        user_id=int(tenant_context.user_id),
        tenant_id=int(tenant_context.tenant_id),
    )


def get_any_task_in_tenant_or_404(
    *,
    db: Session,
    task_id: int,
    tenant_context: TenantRequestContext,
) -> Task:
    """Fetch a tenant-scoped task for explicitly tenant-wide/admin surfaces only."""

    return resolve_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_id=int(tenant_context.tenant_id),
    )


def map_file_browser_exception(exc: Exception) -> HTTPException:
    """Normalize file browser exceptions to stable HTTP error responses."""
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (PermissionError, WorkspacePathError, WorkspaceEntryUnsafeError)):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="File operation failed",
    )


def map_admission_exception(exc: HTTPException) -> HTTPException:
    """Normalize admission rejections to a stable HTTP 409 detail payload."""

    if exc.status_code != status.HTTP_409_CONFLICT:
        return exc

    detail = exc.detail
    if not isinstance(detail, Mapping):
        return exc

    reason_code = detail.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code.strip():
        return exc

    message = detail.get("message")
    normalized_message = (
        message.strip()
        if isinstance(message, str) and message.strip()
        else "Task admission rejected."
    )
    reason_codes = detail.get("reason_codes")
    normalized_reason_codes = (
        [str(reason).strip() for reason in reason_codes if str(reason or "").strip()]
        if isinstance(reason_codes, list | tuple)
        else [reason_code.strip()]
    )
    if not normalized_reason_codes:
        normalized_reason_codes = [reason_code.strip()]
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "reason_code": reason_code.strip(),
            "reason_codes": normalized_reason_codes,
            "message": normalized_message,
        },
        headers=exc.headers,
    )
