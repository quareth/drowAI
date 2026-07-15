"""Authenticated system resource metrics endpoint for the settings UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.auth import get_current_user
from backend.models import User
from backend.routers.tasks.deps import enforce_tenant_action
from backend.schemas.system_metrics import SystemMetricsResponse
from backend.services.platform.system_metrics_service import SystemMetricsService
from backend.services.tenant.authorization import ACTION_TENANT_SETTINGS_MANAGE
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context


router = APIRouter(prefix="/api/settings/system", tags=["settings"])


@router.get("/metrics", response_model=SystemMetricsResponse)
async def get_system_metrics(
    _current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
) -> SystemMetricsResponse:
    """Return a fresh management-host resource snapshot."""

    enforce_tenant_action(
        tenant_context=tenant_context,
        action=ACTION_TENANT_SETTINGS_MANAGE,
    )
    return SystemMetricsService().collect()


__all__ = ["router"]
