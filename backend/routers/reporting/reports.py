"""Expose engagement report generation and read endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...routers.tasks.deps import enforce_tenant_action
from ...schemas.reporting import (
    CurrentEngagementReportResponse,
    EngagementReportDeleteResponse,
    EngagementReportGenerationRequest,
    EngagementReportGenerationResponse,
    EngagementReportHistoryResponse,
    EngagementReportReadResponse,
    EngagementReportUndoDeleteResponse,
    ReportLibraryResponse,
)
from ...services.engagement.access_service import get_owned_engagement_or_404
from ...services.platform.background_services import start_background_services
from ...services.reporting.contracts import (
    REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
    REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
    ReportType,
    validate_report_type,
)
from ...services.reporting.report_generation_service import (
    ReportGenerationRequestError,
    ReportGenerationService,
)
from ...services.reporting.report_deletion_service import (
    ReportDeletionError,
    ReportDeletionService,
)
from ...services.reporting.report_read_service import ReportReadService
from ...services.tenant.authorization import (
    ACTION_REPORT_DELETE,
    ACTION_REPORT_READ,
    ACTION_REPORT_WRITE,
)
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context

router = APIRouter()

_REPORT_GENERATION_CONFLICT_REASONS = {
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
}
_REPORT_GENERATION_UNPROCESSABLE_REASONS = {
    REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
    REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
}


def _validated_report_type(report_type: str) -> ReportType:
    try:
        return validate_report_type(report_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/engagements/{engagement_id}/reports",
    response_model=EngagementReportGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_engagement_report(
    engagement_id: int,
    request: EngagementReportGenerationRequest,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportGenerationResponse:
    """Accept a report generation request for a user-owned engagement."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_WRITE)
    engagement = get_owned_engagement_or_404(
        db,
        engagement_id=int(engagement_id),
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    if str(getattr(engagement, "status", "active")).strip().lower() == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot generate reports for archived engagements. Restore the engagement first.",
        )
    try:
        scheduler_running = await start_background_services()
    except Exception:
        scheduler_running = False
    if not scheduler_running:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Report generation is temporarily unavailable.",
        )
    try:
        result = ReportGenerationService(db).request_generation(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            requested_by_user_id=int(current_user.id),
            engagement_id=int(engagement_id),
            report_type=request.report_type,
            selected_task_memo_ids=request.selected_task_memo_ids,
            include_candidate_findings=request.include_candidate_findings,
            force_regenerate=request.force_regenerate,
            engagement_is_owned=True,
        )
    except ReportGenerationRequestError as exc:
        _raise_report_generation_error(exc)
    if result.job_id is not None:
        db.commit()
    return EngagementReportGenerationResponse(
        job_id=result.job_id,
        report_id=result.report_id,
        status=result.status,
    )


@router.get(
    "/engagements/{engagement_id}/reports/current",
    response_model=CurrentEngagementReportResponse,
)
async def get_current_engagement_report(
    engagement_id: int,
    report_type: str = Query(...),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> CurrentEngagementReportResponse:
    """Return the current ready report for a user-owned engagement."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    validated_report_type = _validated_report_type(report_type)
    get_owned_engagement_or_404(
        db,
        engagement_id=int(engagement_id),
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    return ReportReadService(db).get_current_report(
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        engagement_id=int(engagement_id),
        report_type=validated_report_type,
    )


@router.get(
    "/engagements/{engagement_id}/reports",
    response_model=EngagementReportHistoryResponse,
)
async def list_engagement_reports(
    engagement_id: int,
    report_type: str = Query(...),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportHistoryResponse:
    """Return compact report versions for a user-owned engagement."""

    return await _list_engagement_report_history(
        engagement_id=engagement_id,
        report_type=report_type,
        current_user=current_user,
        tenant_context=tenant_context,
        db=db,
    )


@router.get(
    "/engagements/{engagement_id}/reports/history",
    response_model=EngagementReportHistoryResponse,
)
async def list_engagement_report_history(
    engagement_id: int,
    report_type: str = Query(...),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportHistoryResponse:
    """Return persisted report versions for a user-owned engagement."""

    return await _list_engagement_report_history(
        engagement_id=engagement_id,
        report_type=report_type,
        current_user=current_user,
        tenant_context=tenant_context,
        db=db,
    )


@router.get(
    "/reports",
    response_model=ReportLibraryResponse,
)
async def list_report_library(
    report_type: str | None = Query(default=None),
    engagement_id: int | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> ReportLibraryResponse:
    """Return tenant/user-owned generated reports without requiring live engagements."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    try:
        return ReportReadService(db).list_report_library(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            report_type=report_type,
            engagement_id=engagement_id,
            query=query,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get(
    "/reports/{report_id}",
    response_model=EngagementReportReadResponse,
)
async def get_engagement_report(
    report_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportReadResponse:
    """Return one report version with full section content."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    report = ReportReadService(db).get_report(
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        report_id=report_id,
    )
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    return report


@router.delete(
    "/reports/{report_id}",
    response_model=EngagementReportDeleteResponse,
)
async def delete_engagement_report(
    report_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportDeleteResponse:
    """Schedule one report for deletion with an undo window."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_DELETE)
    try:
        result = ReportDeletionService(db).schedule_delete(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            requested_by_user_id=int(current_user.id),
            report_id=report_id,
        )
    except ReportDeletionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    return result


@router.post(
    "/reports/{report_id}/undo-delete",
    response_model=EngagementReportUndoDeleteResponse,
)
async def undo_delete_engagement_report(
    report_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportUndoDeleteResponse:
    """Cancel pending report deletion before finalization."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_DELETE)
    try:
        result = ReportDeletionService(db).undo_delete(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            report_id=report_id,
        )
    except ReportDeletionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    return result


async def _list_engagement_report_history(
    *,
    engagement_id: int,
    report_type: str,
    current_user: User,
    tenant_context: TenantRequestContext,
    db: Session,
) -> EngagementReportHistoryResponse:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    validated_report_type = _validated_report_type(report_type)
    get_owned_engagement_or_404(
        db,
        engagement_id=int(engagement_id),
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    return ReportReadService(db).list_report_history(
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        engagement_id=int(engagement_id),
        report_type=validated_report_type,
    )


def _raise_report_generation_error(exc: ReportGenerationRequestError) -> None:
    if exc.reason in _REPORT_GENERATION_CONFLICT_REASONS:
        status_code = status.HTTP_409_CONFLICT
    elif exc.reason in _REPORT_GENERATION_UNPROCESSABLE_REASONS:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    raise HTTPException(status_code=status_code, detail=exc.safe_message) from exc


__all__ = ["router"]
