"""Tenant context resolution and membership summary service.

Responsibilities:
- Resolve an active tenant request context for a user.
- Enforce explicit tenant selection when multiple active memberships exist.
- Preserve default-tenant bootstrap/repair for standalone installations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Tenant, TenantMembership
from backend.services.tenant.authorization import POLICY_VERSION, allowed_actions_for_role

DEFAULT_TENANT_ID = 1
DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default Tenant"
DEFAULT_TENANT_ROLE = "owner"
ACTIVE_STATUS = "active"


class TenantContextResolutionError(ValueError):
    """Raised when tenant context cannot be resolved safely."""

    def __init__(self, *, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class TenantMembershipSummary:
    membership_id: int
    tenant_id: int
    tenant_slug: str
    tenant_name: str
    role: str
    membership_status: str
    tenant_status: str
    is_default_tenant: bool


@dataclass(frozen=True)
class TenantRequestContext:
    tenant_id: int
    user_id: int
    role: str
    membership_id: int
    is_default_tenant: bool
    source: str = "default"


@dataclass(frozen=True)
class TenantEffectivePermissions:
    actions: tuple[str, ...]
    role: str
    tenant_id: int
    policy_version: str = POLICY_VERSION


# Backward-compatible alias for existing imports.
TenantContext = TenantRequestContext


class TenantContextService:
    """Resolve active tenant context and provide tenant membership summaries."""

    def __init__(self, db: Session):
        self.db = db

    def resolve_for_user(
        self,
        *,
        user_id: int,
        requested_tenant_id: int | None = None,
        requested_source: str = "default",
        preferred_tenant_id: int | None = None,
        allow_ambiguous: bool = False,
    ) -> TenantRequestContext | None:
        """Resolve active tenant for user with explicit-selection enforcement."""

        memberships = self._load_membership_rows(user_id=int(user_id))
        if not memberships and allow_ambiguous:
            repaired = self._repair_default_membership_if_standalone(user_id=int(user_id))
            if repaired:
                memberships = self._load_membership_rows(user_id=int(user_id))
        active_memberships = [row for row in memberships if self._membership_and_tenant_are_active(*row)]

        if requested_tenant_id is not None:
            membership, tenant = self._select_membership_for_tenant(
                memberships=memberships,
                tenant_id=int(requested_tenant_id),
            )
            if not self._membership_and_tenant_are_active(membership, tenant):
                raise TenantContextResolutionError(
                    code="inactive_tenant_membership",
                    message="Requested tenant membership is inactive.",
                )
            return self._build_context(
                membership=membership,
                tenant=tenant,
                user_id=int(user_id),
                source=requested_source or "explicit",
            )

        if preferred_tenant_id is not None:
            membership, tenant = self._select_membership_for_tenant(
                memberships=memberships,
                tenant_id=int(preferred_tenant_id),
            )
            if not self._membership_and_tenant_are_active(membership, tenant):
                raise TenantContextResolutionError(
                    code="inactive_tenant_membership",
                    message="Preferred tenant membership is inactive.",
                )
            return self._build_context(
                membership=membership,
                tenant=tenant,
                user_id=int(user_id),
                source="persisted_preference",
            )

        if not active_memberships:
            if memberships:
                raise TenantContextResolutionError(
                    code="inactive_tenant_membership",
                    message="No active tenant membership available for user.",
                )
            raise TenantContextResolutionError(
                code="no_active_membership",
                message="No active tenant membership available for user.",
            )

        if len(active_memberships) == 1:
            membership, tenant = active_memberships[0]
            return self._build_context(
                membership=membership,
                tenant=tenant,
                user_id=int(user_id),
                source="single_membership",
            )

        if allow_ambiguous:
            return None

        raise TenantContextResolutionError(
            code="explicit_tenant_required",
            message="Explicit tenant selection is required for users with multiple active memberships.",
        )

    def list_membership_summaries_for_user(self, *, user_id: int) -> list[TenantMembershipSummary]:
        """Return deterministic tenant membership summaries for a user."""

        memberships = self._load_membership_rows(user_id=int(user_id))
        if not memberships and self._repair_default_membership_if_standalone(user_id=int(user_id)):
            memberships = self._load_membership_rows(user_id=int(user_id))
        return [self._build_membership_summary(membership=row[0], tenant=row[1]) for row in memberships]

    @staticmethod
    def build_effective_permissions(
        context: TenantRequestContext | None,
    ) -> TenantEffectivePermissions | None:
        """Build server-derived effective permissions from current role."""

        if context is None:
            return None
        actions = allowed_actions_for_role(context.role)
        return TenantEffectivePermissions(
            actions=actions,
            role=str(context.role),
            tenant_id=int(context.tenant_id),
        )

    def ensure_default_tenant(self) -> Tenant:
        tenant = self.db.execute(
            select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID).with_for_update()
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                id=DEFAULT_TENANT_ID,
                slug=DEFAULT_TENANT_SLUG,
                name=DEFAULT_TENANT_NAME,
                status=ACTIVE_STATUS,
            )
            self.db.add(tenant)
            self.db.flush()
        elif not self._is_active(tenant):
            tenant.status = ACTIVE_STATUS
            tenant.deactivated_at = None
            self.db.flush()
        return tenant

    def ensure_default_membership(self, *, user_id: int) -> TenantMembership:
        tenant = self.ensure_default_tenant()
        membership = self.db.execute(
            select(TenantMembership)
            .where(
                TenantMembership.tenant_id == tenant.id,
                TenantMembership.user_id == user_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if membership is None:
            membership = TenantMembership(
                tenant_id=int(tenant.id),
                user_id=int(user_id),
                role=DEFAULT_TENANT_ROLE,
                status=ACTIVE_STATUS,
            )
            self.db.add(membership)
            self.db.flush()
        return membership

    def _load_membership_rows(self, *, user_id: int) -> list[tuple[TenantMembership, Tenant]]:
        return self.db.execute(
            select(TenantMembership, Tenant)
            .join(Tenant, Tenant.id == TenantMembership.tenant_id)
            .where(TenantMembership.user_id == int(user_id))
            .order_by(TenantMembership.tenant_id.asc(), TenantMembership.id.asc())
        ).all()

    def _repair_default_membership_if_standalone(self, *, user_id: int) -> bool:
        """Auto-repair default membership only when no non-default tenants exist."""

        non_default_tenant_id = self.db.execute(
            select(Tenant.id).where(Tenant.id != DEFAULT_TENANT_ID).limit(1)
        ).scalar_one_or_none()
        if non_default_tenant_id is not None:
            return False

        self.ensure_default_membership(user_id=int(user_id))
        self.db.commit()
        return True

    def _select_membership_for_tenant(
        self,
        *,
        memberships: list[tuple[TenantMembership, Tenant]],
        tenant_id: int,
    ) -> tuple[TenantMembership, Tenant]:
        for membership, tenant in memberships:
            if int(membership.tenant_id) == int(tenant_id) and int(tenant.id) == int(tenant_id):
                return membership, tenant
        raise TenantContextResolutionError(
            code="tenant_membership_required",
            message="Requested tenant is not associated with the authenticated user.",
        )

    @staticmethod
    def _status_value(record: Any) -> str:
        status_value = getattr(record, "status", ACTIVE_STATUS)
        if status_value is None:
            return ACTIVE_STATUS
        normalized = str(status_value).strip().lower()
        return normalized or ACTIVE_STATUS

    @staticmethod
    def _is_active(record: Any) -> bool:
        if TenantContextService._status_value(record) != ACTIVE_STATUS:
            return False
        deactivated_at = getattr(record, "deactivated_at", None)
        return deactivated_at is None

    @classmethod
    def _membership_and_tenant_are_active(cls, membership: TenantMembership, tenant: Tenant) -> bool:
        return cls._is_active(membership) and cls._is_active(tenant)

    def _build_membership_summary(
        self,
        *,
        membership: TenantMembership,
        tenant: Tenant,
    ) -> TenantMembershipSummary:
        return TenantMembershipSummary(
            membership_id=int(membership.id),
            tenant_id=int(tenant.id),
            tenant_slug=str(tenant.slug),
            tenant_name=str(tenant.name),
            role=str(membership.role or DEFAULT_TENANT_ROLE),
            membership_status=self._status_value(membership),
            tenant_status=self._status_value(tenant),
            is_default_tenant=int(tenant.id) == DEFAULT_TENANT_ID,
        )

    def _build_context(
        self,
        *,
        membership: TenantMembership,
        tenant: Tenant,
        user_id: int,
        source: str,
    ) -> TenantRequestContext:
        return TenantRequestContext(
            tenant_id=int(tenant.id),
            user_id=int(user_id),
            role=str(membership.role or DEFAULT_TENANT_ROLE),
            membership_id=int(membership.id),
            is_default_tenant=int(tenant.id) == DEFAULT_TENANT_ID,
            source=str(source),
        )
