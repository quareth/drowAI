"""Tenant-admin retention dry-run and apply API router.

This router authorizes active-tenant data-management administrators and
delegates retention execution to the central orchestrator.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models import User
from backend.routers.tasks.deps import enforce_tenant_action
from backend.schemas.retention import (
    RetentionApplyRequest,
    RetentionDryRunRequest,
    RetentionRunResponse,
)
from backend.services.retention.contracts import (
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_TENANT,
    RetentionRunRequest,
)
from backend.services.retention.orchestrator import RetentionOrchestrator
from backend.services.tenant.authorization import ACTION_TENANT_SETTINGS_MANAGE
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context


router = APIRouter(prefix="/api/retention", tags=["retention"])


@router.post("/dry-run", response_model=RetentionRunResponse)
async def dry_run_retention(
    payload: RetentionDryRunRequest | None = None,
    _current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> RetentionRunResponse:
    """Run a count-only retention dry-run for the active tenant."""

    request_payload = payload or RetentionDryRunRequest()
    return _run_retention_for_active_tenant(
        db=db,
        tenant_context=tenant_context,
        mode=RETENTION_RUN_MODE_DRY_RUN,
        retention_classes=request_payload.retention_classes,
        limit_per_tenant=request_payload.limit_per_tenant,
    )


@router.post("/apply", response_model=RetentionRunResponse)
async def apply_retention(
    payload: RetentionApplyRequest,
    _current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> RetentionRunResponse:
    """Run bounded retention apply for the active tenant after confirmation."""

    if payload.confirm is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Retention apply requires confirm=true.",
        )
    return _run_retention_for_active_tenant(
        db=db,
        tenant_context=tenant_context,
        mode=RETENTION_RUN_MODE_APPLY,
        retention_classes=payload.retention_classes,
        limit_per_tenant=payload.limit_per_tenant,
    )


def _run_retention_for_active_tenant(
    *,
    db: Session,
    tenant_context: TenantRequestContext,
    mode: str,
    retention_classes: tuple[str, ...],
    limit_per_tenant: int | None,
) -> RetentionRunResponse:
    enforce_tenant_action(
        tenant_context=tenant_context,
        action=ACTION_TENANT_SETTINGS_MANAGE,
    )
    request = RetentionRunRequest(
        mode=mode,
        scope=RETENTION_SCOPE_TENANT,
        tenant_id=int(tenant_context.tenant_id),
        retention_classes=retention_classes,
        limit_per_tenant=limit_per_tenant,
    )
    try:
        result = RetentionOrchestrator(db).run(request)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return RetentionRunResponse.from_run_result(result)


__all__ = ["router"]
