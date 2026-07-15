"""Tenant context dependency helpers for HTTP request handlers.

Responsibilities:
- Parse tenant hints from validated request sources.
- Resolve tenant context through TenantContextService.
- Translate tenant context failures into deterministic HTTP errors.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from backend.auth import (
    extract_active_tenant_hint,
    get_current_user,
    security,
    verify_token_with_error,
)
from backend.database import get_db
from backend.models import User
from backend.services.tenant.context import (
    TenantContextResolutionError,
    TenantContextService,
    TenantRequestContext,
)

ACTIVE_TENANT_HEADER = "X-Active-Tenant-Id"


def _resolve_request_actor_type(*, role: str | None) -> str:
    normalized_role = str(role or "").strip().lower()
    if normalized_role == "owner":
        return "tenant_owner"
    if normalized_role == "admin":
        return "tenant_admin"
    if normalized_role == "operator":
        return "tenant_operator"
    if normalized_role == "viewer":
        return "tenant_viewer"
    return "user"


def parse_requested_tenant_id(header_value: str | None) -> int | None:
    """Parse and validate active-tenant header hint."""

    if header_value is None:
        return None
    trimmed = header_value.strip()
    if not trimmed:
        return None
    try:
        parsed = int(trimmed)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{ACTIVE_TENANT_HEADER} must be a positive integer.",
        ) from exc
    if parsed <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{ACTIVE_TENANT_HEADER} must be a positive integer.",
        )
    return parsed


def resolve_token_payload(credentials: HTTPAuthorizationCredentials | None) -> Mapping[str, object] | None:
    """Decode token payload for optional tenant-hint extraction."""

    if credentials is None:
        return None
    payload, error_code = verify_token_with_error(credentials.credentials)
    if payload is None or error_code is not None:
        return None
    return payload


def resolve_tenant_context_for_request(
    *,
    tenant_context_service: TenantContextService,
    current_user: User,
    header_tenant_id: str | None,
    credentials: HTTPAuthorizationCredentials | None,
    allow_ambiguous: bool,
) -> TenantRequestContext | None:
    """Resolve tenant context from user identity and validated tenant hints."""

    requested_tenant_id = parse_requested_tenant_id(header_tenant_id)
    token_payload = resolve_token_payload(credentials)
    preferred_tenant_id = None if requested_tenant_id is not None else extract_active_tenant_hint(token_payload)

    return tenant_context_service.resolve_for_user(
        user_id=int(current_user.id),
        requested_tenant_id=requested_tenant_id,
        requested_source="header" if requested_tenant_id is not None else "token_hint",
        preferred_tenant_id=preferred_tenant_id,
        allow_ambiguous=allow_ambiguous,
    )


def map_tenant_context_error(exc: TenantContextResolutionError) -> HTTPException:
    code_to_status = {
        "explicit_tenant_required": status.HTTP_409_CONFLICT,
        "tenant_membership_required": status.HTTP_403_FORBIDDEN,
        "inactive_tenant_membership": status.HTTP_403_FORBIDDEN,
        "no_active_membership": status.HTTP_403_FORBIDDEN,
    }
    mapped_status = code_to_status.get(exc.code, status.HTTP_403_FORBIDDEN)
    return HTTPException(status_code=mapped_status, detail=str(exc))


def get_tenant_request_context(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    header_tenant_id: str | None = Header(default=None, alias=ACTIVE_TENANT_HEADER),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> TenantRequestContext:
    """Resolve strict tenant context for tenant-owned HTTP endpoints."""

    service = TenantContextService(db)
    try:
        resolved = resolve_tenant_context_for_request(
            tenant_context_service=service,
            current_user=current_user,
            header_tenant_id=header_tenant_id,
            credentials=credentials,
            allow_ambiguous=False,
        )
    except TenantContextResolutionError as exc:
        raise map_tenant_context_error(exc) from exc

    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Explicit tenant selection is required for this user.",
        )
    from backend.services.tenant.rls import set_tenant_rls_context

    set_tenant_rls_context(
        db,
        tenant_id=int(resolved.tenant_id),
        user_id=int(current_user.id),
        actor_type=_resolve_request_actor_type(role=resolved.role),
    )
    return resolved
