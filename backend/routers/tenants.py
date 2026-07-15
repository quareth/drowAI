"""Tenant membership and active-context API router for Tenant Isolation.

Responsibilities:
- Expose authenticated tenant membership listing for the current user.
- Expose active-tenant context read/switch APIs with effective permissions.
- Expose owner/admin tenant membership management operations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from backend.auth import get_current_user, security
from backend.database import get_db
from backend.models import User
from backend.schemas import (
    ActiveTenantContextResponse,
    EffectivePermissionsResponse,
    TenantContextResponse,
    TenantManagedMembershipResponse,
    TenantMembershipSummaryResponse,
    TenantMembershipUpdateRequest,
    TenantSwitchRequest,
)
from backend.services.tenant.context import TenantContextResolutionError
from backend.services.tenant.context import TenantContextService
from backend.services.tenant.dependencies import (
    ACTIVE_TENANT_HEADER,
    get_tenant_request_context,
    map_tenant_context_error,
    resolve_tenant_context_for_request,
)
from backend.services.tenant.membership_service import (
    TenantMembershipService,
    TenantMembershipServiceError,
)

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


def _map_membership_service_error(exc: TenantMembershipServiceError) -> HTTPException:
    code_to_status = {
        "TENANT_CONTEXT_MISMATCH": status.HTTP_403_FORBIDDEN,
        "TENANT_MEMBERSHIP_FORBIDDEN": status.HTTP_403_FORBIDDEN,
        "TENANT_MEMBERSHIP_NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "TENANT_OWNER_REQUIRED": status.HTTP_409_CONFLICT,
        "TENANT_INVALID_ROLE": status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(status_code=code_to_status.get(exc.error_code, status.HTTP_400_BAD_REQUEST), detail=str(exc))


def _build_context_response(
    *,
    service: TenantMembershipService,
    context,
    memberships,
) -> TenantContextResponse:
    permissions = service.build_effective_permissions(context)
    active_tenant = None
    if context is not None:
        active_tenant = ActiveTenantContextResponse(
            tenant_id=int(context.tenant_id),
            membership_id=int(context.membership_id),
            role=str(context.role),
            is_default_tenant=bool(context.is_default_tenant),
            source=str(context.source),
        )

    effective_permissions = None
    if permissions is not None:
        effective_permissions = EffectivePermissionsResponse(
            actions=list(permissions.actions),
            role=str(permissions.role),
            tenant_id=int(permissions.tenant_id),
            policy_version=str(permissions.policy_version),
        )

    return TenantContextResponse(
        active_tenant=active_tenant,
        membership_summaries=[
            TenantMembershipSummaryResponse(
                membership_id=int(membership.membership_id),
                tenant_id=int(membership.tenant_id),
                tenant_slug=str(membership.tenant_slug),
                tenant_name=str(membership.tenant_name),
                role=str(membership.role),
                membership_status=str(membership.membership_status),
                tenant_status=str(membership.tenant_status),
                is_default_tenant=bool(membership.is_default_tenant),
            )
            for membership in memberships
        ],
        effective_permissions=effective_permissions,
    )


@router.get("/memberships", response_model=list[TenantMembershipSummaryResponse])
def list_current_user_memberships(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TenantMembershipSummaryResponse]:
    """List memberships for the authenticated user only."""

    service = TenantMembershipService(db)
    memberships = service.list_membership_summaries_for_user(user_id=int(current_user.id))
    return [
        TenantMembershipSummaryResponse(
            membership_id=int(membership.membership_id),
            tenant_id=int(membership.tenant_id),
            tenant_slug=str(membership.tenant_slug),
            tenant_name=str(membership.tenant_name),
            role=str(membership.role),
            membership_status=str(membership.membership_status),
            tenant_status=str(membership.tenant_status),
            is_default_tenant=bool(membership.is_default_tenant),
        )
        for membership in memberships
    ]


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_tenant_header: str | None = Header(default=None, alias=ACTIVE_TENANT_HEADER),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> TenantContextResponse:
    """Read active tenant context and effective permissions for the user."""

    service = TenantMembershipService(db)
    context_service = TenantContextService(db)
    try:
        context = resolve_tenant_context_for_request(
            tenant_context_service=context_service,
            current_user=current_user,
            header_tenant_id=active_tenant_header,
            credentials=credentials,
            allow_ambiguous=True,
        )
    except TenantContextResolutionError as exc:
        raise map_tenant_context_error(exc) from exc

    memberships = service.list_membership_summaries_for_user(user_id=int(current_user.id))
    return _build_context_response(service=service, context=context, memberships=memberships)


@router.post("/context/switch", response_model=TenantContextResponse)
def switch_tenant_context(
    payload: TenantSwitchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantContextResponse:
    """Validate and switch active tenant context for the authenticated user."""

    service = TenantMembershipService(db)
    try:
        context = service.resolve_active_context(
            user_id=int(current_user.id),
            requested_tenant_id=int(payload.tenant_id),
            requested_source="switch_api",
            allow_ambiguous=False,
        )
    except TenantContextResolutionError as exc:
        raise map_tenant_context_error(exc) from exc

    memberships = service.list_membership_summaries_for_user(user_id=int(current_user.id))
    return _build_context_response(service=service, context=context, memberships=memberships)


@router.get("/{tenant_id}/memberships", response_model=list[TenantManagedMembershipResponse])
def list_tenant_memberships(
    tenant_id: int,
    db: Session = Depends(get_db),
    tenant_context=Depends(get_tenant_request_context),
) -> list[TenantManagedMembershipResponse]:
    """List memberships for a tenant when owner/admin authorization passes."""

    service = TenantMembershipService(db)
    try:
        memberships = service.list_tenant_memberships(actor_context=tenant_context, tenant_id=int(tenant_id))
    except TenantMembershipServiceError as exc:
        raise _map_membership_service_error(exc) from exc

    return [
        TenantManagedMembershipResponse(
            membership_id=int(item.membership_id),
            tenant_id=int(item.tenant_id),
            user_id=int(item.user_id),
            role=str(item.role),
            status=str(item.status),
            deactivated_at=item.deactivated_at,
            deactivated_by_user_id=item.deactivated_by_user_id,
        )
        for item in memberships
    ]


@router.patch("/{tenant_id}/memberships/{membership_id}", response_model=TenantManagedMembershipResponse)
def update_tenant_membership(
    tenant_id: int,
    membership_id: int,
    payload: TenantMembershipUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    tenant_context=Depends(get_tenant_request_context),
) -> TenantManagedMembershipResponse:
    """Change role or deactivate one tenant membership with owner-preservation."""

    service = TenantMembershipService(db)
    try:
        if payload.deactivate:
            updated = service.deactivate_membership(
                actor_context=tenant_context,
                tenant_id=int(tenant_id),
                membership_id=int(membership_id),
                deactivated_by_user_id=int(current_user.id),
            )
        else:
            updated = service.change_membership_role(
                actor_context=tenant_context,
                tenant_id=int(tenant_id),
                membership_id=int(membership_id),
                new_role=str(payload.role),
            )
    except TenantMembershipServiceError as exc:
        raise _map_membership_service_error(exc) from exc

    return TenantManagedMembershipResponse(
        membership_id=int(updated.membership_id),
        tenant_id=int(updated.tenant_id),
        user_id=int(updated.user_id),
        role=str(updated.role),
        status=str(updated.status),
        deactivated_at=updated.deactivated_at,
        deactivated_by_user_id=updated.deactivated_by_user_id,
    )
