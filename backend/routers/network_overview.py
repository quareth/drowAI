"""Authenticated read-only network overview endpoint for Settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models import User
from backend.routers.tasks.deps import enforce_tenant_action
from backend.schemas.network_overview import NetworkOverviewResponse
from backend.services.platform.network_overview_service import NetworkOverviewService
from backend.services.tenant.authorization import ACTION_TENANT_SETTINGS_MANAGE
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context


router = APIRouter(prefix="/api/settings/network", tags=["settings"])


@router.get("/overview", response_model=NetworkOverviewResponse)
async def get_network_overview(
    request: Request,
    _current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> NetworkOverviewResponse:
    """Return Management routing and tenant Runner connection information."""

    enforce_tenant_action(
        tenant_context=tenant_context,
        action=ACTION_TENANT_SETTINGS_MANAGE,
    )
    return NetworkOverviewService(db).collect(
        tenant_id=int(tenant_context.tenant_id),
        request=request,
    )


__all__ = ["router"]
