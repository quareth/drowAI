"""Expose read-only engagement report job status endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...routers.tasks.deps import enforce_tenant_action
from ...schemas.reporting import (
    EngagementReportActiveJobResponse,
    EngagementReportJobStatusResponse,
)
from ...services.engagement.access_service import get_owned_engagement_or_404
from ...services.reporting.report_read_service import ReportReadService
from ...services.tenant.authorization import ACTION_REPORT_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context

router = APIRouter()


@router.get(
    "/engagements/{engagement_id}/jobs/active",
    response_model=EngagementReportActiveJobResponse,
)
async def get_active_engagement_report_job(
    engagement_id: int,
    report_type: str = Query(...),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportActiveJobResponse:
    """Return the latest active report job for a user-owned engagement."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    get_owned_engagement_or_404(
        db,
        engagement_id=int(engagement_id),
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    return ReportReadService(db).get_active_job(
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        requested_by_user_id=int(current_user.id),
        engagement_id=int(engagement_id),
        report_type=report_type,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=EngagementReportJobStatusResponse,
)
async def get_report_job_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportJobStatusResponse:
    """Return an existing report job status for the requesting owner."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    job_status = ReportReadService(db).get_job_status_by_id(
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        requested_by_user_id=int(current_user.id),
        job_id=job_id,
    )
    if job_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report job not found",
        )
    return job_status


@router.get(
    "/engagements/{engagement_id}/jobs/{job_id}",
    response_model=EngagementReportJobStatusResponse,
)
async def get_engagement_report_job_status(
    engagement_id: int,
    job_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportJobStatusResponse:
    """Return an existing report job status for a user-owned engagement."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    get_owned_engagement_or_404(
        db,
        engagement_id=int(engagement_id),
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    job_status = ReportReadService(db).get_job_status(
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        requested_by_user_id=int(current_user.id),
        engagement_id=int(engagement_id),
        job_id=job_id,
    )
    if job_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report job not found",
        )
    return job_status


__all__ = ["router"]
