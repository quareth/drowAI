"""Engagement write router.

This module contains authenticated write endpoints for engagement management.
Read/query endpoints remain in `backend.routers.engagement_knowledge`.

Deletion is modeled as a soft archive by updating `Engagement.status` so
engagement-scoped durable knowledge remains preserved.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models.core import Engagement, User
from backend.schemas.core import EngagementCreate, EngagementResponse
from backend.services.engagement.management_service import EngagementManagementService
from backend.services.tenant.authorization import ACTION_KNOWLEDGE_WRITE
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context
from backend.routers.tasks.deps import enforce_tenant_action

router = APIRouter(prefix="/api/engagements", tags=["engagements"])


@router.post("/", response_model=EngagementResponse, status_code=201)
def create_engagement(
    payload: EngagementCreate,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementResponse:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_WRITE)
    engagement = Engagement(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
        name=payload.name,
        description=(payload.description or "").strip() or None,
        status="active",
    )
    db.add(engagement)
    db.commit()
    db.refresh(engagement)
    return EngagementResponse.model_validate(engagement)


@router.delete("/{engagement_id}", response_model=EngagementResponse)
def archive_engagement(
    engagement_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementResponse:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_WRITE)
    engagement_management_service = EngagementManagementService(db)
    engagement = engagement_management_service.archive_engagement(
        engagement_id=int(engagement_id),
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
    )
    return EngagementResponse.model_validate(engagement)


@router.post("/{engagement_id}/restore", response_model=EngagementResponse)
def restore_engagement(
    engagement_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> EngagementResponse:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_WRITE)
    engagement_management_service = EngagementManagementService(db)
    engagement = engagement_management_service.restore_engagement(
        engagement_id=int(engagement_id),
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
    )
    return EngagementResponse.model_validate(engagement)
