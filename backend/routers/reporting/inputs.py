"""Expose engagement reporting input inventory read endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import User
from ...routers.tasks.deps import enforce_tenant_action
from ...schemas.reporting import EngagementReportingInputsResponse
from ...services.reporting.input_inventory_service import (
    InputInventoryService,
    ReportingInputInventoryNotFoundError,
)
from ...services.tenant.authorization import ACTION_REPORT_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context

router = APIRouter()


@router.get(
    "/engagements/{engagement_id}/inputs",
    response_model=EngagementReportingInputsResponse,
)
async def get_engagement_reporting_inputs(
    engagement_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementReportingInputsResponse:
    """Return reporting input inventory for a user-owned engagement."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_REPORT_READ)
    try:
        return InputInventoryService(db).list_engagement_inputs(
            tenant_id=int(tenant_context.tenant_id),
            user_id=int(current_user.id),
            engagement_id=int(engagement_id),
        )
    except ReportingInputInventoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Engagement not found",
        ) from exc


__all__ = ["router"]
