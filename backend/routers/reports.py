"""User-owned report CRUD routes within the active tenant.

This module exposes report create/read/list/delete endpoints and enforces
tenant action policy plus user-owned task/report filtering.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Report, ReportCreate, ReportResponse, User
from ..routers.tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from ..services.tenant.authorization import (
    ACTION_REPORT_DELETE,
    ACTION_REPORT_READ,
    ACTION_REPORT_WRITE,
)
from ..services.tenant.context import TenantRequestContext
from ..services.tenant.dependencies import get_tenant_request_context

router = APIRouter()


@router.get("/", response_model=List[ReportResponse])
@router.get("", response_model=List[ReportResponse])
async def get_reports(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get all reports visible in the active tenant."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    result = db.execute(
        select(Report)
        .where(
            Report.tenant_id == int(tenant_context.tenant_id),
            Report.user_id == int(current_user.id),
        )
        .order_by(Report.created_at.desc())
    )
    reports = result.scalars().all()
    return [ReportResponse.model_validate(report) for report in reports]


@router.post("/", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    report_data: ReportCreate,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Create a new report."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_WRITE)
    task = get_tenant_task_or_404(
        db=db,
        task_id=int(report_data.task_id),
        tenant_context=tenant_context,
    )
    db_report = Report(
        task_id=report_data.task_id,
        tenant_id=task.tenant_id,
        user_id=current_user.id,
        title=report_data.title,
        content=report_data.content,
        findings=report_data.findings,
        severity=report_data.severity
    )

    db.add(db_report)
    db.commit()
    db.refresh(db_report)

    return ReportResponse.model_validate(db_report)


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get a specific report by ID."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    result = db.execute(
        select(Report)
        .where(
            Report.id == int(report_id),
            Report.tenant_id == int(tenant_context.tenant_id),
            Report.user_id == int(current_user.id),
        )
    )
    report = result.scalar_one_or_none()

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found"
        )

    return ReportResponse.model_validate(report)


@router.get("/task/{task_id}", response_model=List[ReportResponse])
async def get_task_reports(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get all reports for a specific task."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    get_tenant_task_or_404(
        db=db,
        task_id=int(task_id),
        tenant_context=tenant_context,
    )
    result = db.execute(
        select(Report)
        .where(
            Report.task_id == int(task_id),
            Report.tenant_id == int(tenant_context.tenant_id),
            Report.user_id == int(current_user.id),
        )
        .order_by(Report.created_at.desc())
    )
    reports = result.scalars().all()

    return [ReportResponse.model_validate(report) for report in reports]


@router.delete("/{report_id}")
async def delete_report(
    report_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Delete a specific report."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_DELETE)
    result = db.execute(
        select(Report)
        .where(
            Report.id == int(report_id),
            Report.tenant_id == int(tenant_context.tenant_id),
            Report.user_id == int(current_user.id),
        )
    )
    report = result.scalar_one_or_none()

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found"
        )
    db.delete(report)
    db.commit()

    return {"message": "Report deleted successfully"}
