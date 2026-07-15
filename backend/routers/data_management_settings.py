"""Tenant data management settings API router.

This router exposes tenant-scoped lifecycle policy settings and delegates all
validation and persistence to the settings service.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.config.retention import RETENTION_POLICY_DEFAULTS
from backend.database import get_db
from backend.models import User
from backend.routers.tasks.deps import enforce_tenant_action
from backend.schemas.data_management import (
    TenantDataManagementSettingsResponse,
    TenantDataManagementSettingsUpdateRequest,
)
from backend.services.data_management_settings_service import (
    DataManagementSettingsService,
    DataManagementSettingsValidationError,
)
from backend.services.tenant.authorization import ACTION_TENANT_SETTINGS_MANAGE
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context

_RETENTION_SETTING_FIELDS = frozenset(RETENTION_POLICY_DEFAULTS)
_RANGE_VALIDATION_ERROR_TYPES = {"greater_than_equal", "less_than_equal"}


class _DataManagementSettingsRoute(APIRoute):
    """Translate retention range validation failures to the settings API contract."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                if _is_retention_range_validation_error(exc):
                    return JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content={
                            "detail": "Retention setting values are out of range."
                        },
                    )
                raise

        return custom_route_handler


def _is_retention_range_validation_error(exc: RequestValidationError) -> bool:
    errors = exc.errors()
    if not errors:
        return False

    for error in errors:
        location = error.get("loc", ())
        field_name = location[-1] if location else None
        if (
            error.get("type") not in _RANGE_VALIDATION_ERROR_TYPES
            or field_name not in _RETENTION_SETTING_FIELDS
        ):
            return False

    return True


router = APIRouter(
    prefix="/api/settings/data-management",
    tags=["settings"],
    route_class=_DataManagementSettingsRoute,
)


@router.get("", response_model=TenantDataManagementSettingsResponse)
async def get_data_management_settings(
    _current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TenantDataManagementSettingsResponse:
    """Return data management settings for the active tenant."""

    enforce_tenant_action(
        tenant_context=tenant_context,
        action=ACTION_TENANT_SETTINGS_MANAGE,
    )
    return DataManagementSettingsService(db).get_settings_response(
        tenant_id=int(tenant_context.tenant_id)
    )


@router.put("", response_model=TenantDataManagementSettingsResponse)
async def update_data_management_settings(
    payload: TenantDataManagementSettingsUpdateRequest,
    _current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TenantDataManagementSettingsResponse:
    """Update data management settings for the active tenant."""

    enforce_tenant_action(
        tenant_context=tenant_context,
        action=ACTION_TENANT_SETTINGS_MANAGE,
    )
    try:
        return DataManagementSettingsService(db).update_settings(
            tenant_id=int(tenant_context.tenant_id),
            payload=payload,
        )
    except DataManagementSettingsValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


__all__ = ["router"]
