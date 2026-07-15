"""Tenant membership management service for Tenant Isolation tenant APIs.

Responsibilities:
- Enforce centralized tenant membership management authorization.
- Provide tenant-scoped membership listing and mutation operations.
- Preserve tenant owner presence during role downgrade and deactivation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import TenantMembership
from backend.services.tenant.authorization import (
    ACTION_TENANT_MEMBERSHIP_MANAGE,
    ROLE_ACTIONS,
    ROLE_OWNER,
    is_action_allowed,
)
from backend.services.tenant.context import TenantContextService, TenantRequestContext

ACTIVE_STATUS = "active"
INACTIVE_STATUS = "inactive"


class TenantMembershipServiceError(RuntimeError):
    """Stable error shape for tenant membership API failures."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = str(error_code)


@dataclass(frozen=True, slots=True)
class TenantManagedMembership:
    """Tenant-scoped membership projection returned by management APIs."""

    membership_id: int
    tenant_id: int
    user_id: int
    role: str
    status: str
    deactivated_at: datetime | None
    deactivated_by_user_id: int | None


class TenantMembershipService:
    """Service for tenant membership read/write APIs."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._context_service = TenantContextService(db)

    def list_membership_summaries_for_user(self, *, user_id: int):
        """Return the authenticated user's membership summaries."""

        return self._context_service.list_membership_summaries_for_user(user_id=int(user_id))

    def list_tenant_memberships(
        self,
        *,
        actor_context: TenantRequestContext,
        tenant_id: int,
    ) -> list[TenantManagedMembership]:
        """List memberships in one tenant after management authorization."""

        self._authorize_manage_memberships(actor_context=actor_context, tenant_id=tenant_id)
        rows = self._db.execute(
            select(TenantMembership)
            .where(TenantMembership.tenant_id == int(tenant_id))
            .order_by(TenantMembership.id.asc())
        ).scalars()
        return [self._to_managed_membership(membership=row) for row in rows]

    def change_membership_role(
        self,
        *,
        actor_context: TenantRequestContext,
        tenant_id: int,
        membership_id: int,
        new_role: str,
    ) -> TenantManagedMembership:
        """Change membership role within a tenant with owner-preservation checks."""

        self._authorize_manage_memberships(actor_context=actor_context, tenant_id=tenant_id)
        membership = self._get_membership_or_error(tenant_id=tenant_id, membership_id=membership_id)

        normalized_role = str(new_role or "").strip().lower()
        if normalized_role not in ROLE_ACTIONS:
            raise TenantMembershipServiceError(
                error_code="TENANT_INVALID_ROLE",
                message="Membership role must be one of owner, admin, operator, or viewer.",
            )

        current_role = str(membership.role or "").strip().lower()
        if current_role == normalized_role:
            return self._to_managed_membership(membership=membership)

        if self._membership_is_active(membership) and current_role == ROLE_OWNER and normalized_role != ROLE_OWNER:
            self._ensure_tenant_has_another_owner(tenant_id=tenant_id, excluding_membership_id=membership.id)

        membership.role = normalized_role
        self._db.flush()
        self._db.commit()
        self._db.refresh(membership)
        return self._to_managed_membership(membership=membership)

    def deactivate_membership(
        self,
        *,
        actor_context: TenantRequestContext,
        tenant_id: int,
        membership_id: int,
        deactivated_by_user_id: int,
    ) -> TenantManagedMembership:
        """Deactivate one tenant membership while preserving at least one owner."""

        self._authorize_manage_memberships(actor_context=actor_context, tenant_id=tenant_id)
        membership = self._get_membership_or_error(tenant_id=tenant_id, membership_id=membership_id)

        current_role = str(membership.role or "").strip().lower()
        if self._membership_is_active(membership) and current_role == ROLE_OWNER:
            self._ensure_tenant_has_another_owner(tenant_id=tenant_id, excluding_membership_id=membership.id)

        membership.status = INACTIVE_STATUS
        membership.deactivated_at = datetime.now(tz=UTC)
        membership.deactivated_by_user_id = int(deactivated_by_user_id)
        self._db.flush()
        self._db.commit()
        self._db.refresh(membership)
        return self._to_managed_membership(membership=membership)

    @staticmethod
    def build_effective_permissions(context: TenantRequestContext | None):
        """Build effective permissions from the active context role."""

        return TenantContextService.build_effective_permissions(context)

    def resolve_active_context(
        self,
        *,
        user_id: int,
        requested_tenant_id: int | None = None,
        requested_source: str = "api",
        allow_ambiguous: bool = True,
    ) -> TenantRequestContext | None:
        """Resolve active tenant context for API reads and explicit tenant switches."""

        return self._context_service.resolve_for_user(
            user_id=int(user_id),
            requested_tenant_id=requested_tenant_id,
            requested_source=requested_source,
            allow_ambiguous=allow_ambiguous,
        )

    def _authorize_manage_memberships(self, *, actor_context: TenantRequestContext, tenant_id: int) -> None:
        if int(actor_context.tenant_id) != int(tenant_id):
            raise TenantMembershipServiceError(
                error_code="TENANT_CONTEXT_MISMATCH",
                message="Requested tenant does not match active tenant context.",
            )
        if not is_action_allowed(role=actor_context.role, action=ACTION_TENANT_MEMBERSHIP_MANAGE):
            raise TenantMembershipServiceError(
                error_code="TENANT_MEMBERSHIP_FORBIDDEN",
                message="Tenant owner/admin role is required for membership management.",
            )

    def _get_membership_or_error(self, *, tenant_id: int, membership_id: int) -> TenantMembership:
        membership = self._db.execute(
            select(TenantMembership).where(
                TenantMembership.id == int(membership_id),
                TenantMembership.tenant_id == int(tenant_id),
            )
        ).scalar_one_or_none()
        if membership is None:
            raise TenantMembershipServiceError(
                error_code="TENANT_MEMBERSHIP_NOT_FOUND",
                message="Tenant membership not found.",
            )
        return membership

    def _ensure_tenant_has_another_owner(self, *, tenant_id: int, excluding_membership_id: int) -> None:
        owner_count = self._db.execute(
            select(func.count(TenantMembership.id)).where(
                TenantMembership.tenant_id == int(tenant_id),
                TenantMembership.role == ROLE_OWNER,
                TenantMembership.status == ACTIVE_STATUS,
                TenantMembership.deactivated_at.is_(None),
                TenantMembership.id != int(excluding_membership_id),
            )
        ).scalar_one()
        if int(owner_count or 0) < 1:
            raise TenantMembershipServiceError(
                error_code="TENANT_OWNER_REQUIRED",
                message="Tenant must retain at least one owner membership.",
            )

    @staticmethod
    def _to_managed_membership(*, membership: TenantMembership) -> TenantManagedMembership:
        status_value = str(getattr(membership, "status", ACTIVE_STATUS) or ACTIVE_STATUS).strip().lower()
        status_value = status_value or ACTIVE_STATUS
        return TenantManagedMembership(
            membership_id=int(membership.id),
            tenant_id=int(membership.tenant_id),
            user_id=int(membership.user_id),
            role=str(membership.role or ROLE_OWNER),
            status=status_value,
            deactivated_at=getattr(membership, "deactivated_at", None),
            deactivated_by_user_id=getattr(membership, "deactivated_by_user_id", None),
        )

    @staticmethod
    def _membership_is_active(membership: TenantMembership) -> bool:
        status_value = str(getattr(membership, "status", ACTIVE_STATUS) or ACTIVE_STATUS).strip().lower()
        return status_value == ACTIVE_STATUS and getattr(membership, "deactivated_at", None) is None
