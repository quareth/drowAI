"""Engagement-scoped durable knowledge query API router.

This module exposes authenticated, tenant/user-checked read endpoints for
engagement knowledge views (summary/findings/assets/services/evidence/graph)
and bounded evidence reads without leaking internal storage paths."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models.core import User
from backend.routers.tasks.deps import enforce_tenant_action
from backend.services.engagement.access_service import get_owned_engagement_or_404
from backend.services.knowledge.evidence_read_service import (
    KnowledgeEvidenceReadRequest,
    KnowledgeEvidenceReadService,
)
from backend.services.knowledge.query_service import (
    AssetsFilters,
    EngagementListFilters,
    EvidenceFilters,
    FindingsFilters,
    KnowledgeQueryService,
    WebSurfacePathsFilters,
    normalize_optional_bool,
)
from backend.services.tenant.authorization import ACTION_KNOWLEDGE_READ
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context

router = APIRouter(prefix="/api/engagements", tags=["engagement_knowledge"])

_REDACTED_PATH_KEYS = frozenset(
    {
        "workspace_path",
        "container_path",
        "source_path",
        "fallback_path",
        "archived_file_ref",
        "host_path",
        "absolute_path",
        "local_path",
        "object_key",
    }
)


class PaginatedItemsResponse(BaseModel):
    """Shared list envelope for engagement list/query endpoints."""

    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class EvidenceReadPayload(BaseModel):
    """Request payload for bounded durable evidence reads."""

    mode: Literal["auto", "head", "tail", "match", "full"] = "auto"
    query: str | None = None
    max_chars: int = Field(default=4000, ge=1, le=20000)


class EvidenceReadResponse(BaseModel):
    """Bounded evidence read response shape."""

    status: Literal["ready", "not_found", "not_available"]
    evidence_archive_id: str
    storage_mode: str
    content: str | None = None
    mode_used: Literal["auto", "head", "tail", "match", "full"]
    truncated: bool
    source: Literal["inline_excerpt", "object_ref", "archived_file", "none"]


def _query_service(db: Session) -> KnowledgeQueryService:
    return KnowledgeQueryService(db)


def _evidence_read_service(db: Session) -> KnowledgeEvidenceReadService:
    return KnowledgeEvidenceReadService(db)


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_payload(item)
            for key, item in value.items()
            if key not in _REDACTED_PATH_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


@router.get("", response_model=PaginatedItemsResponse)
@router.get("/", response_model=PaginatedItemsResponse)
def list_engagements(
    query: str | None = Query(default=None),
    status: str = Query(default="active"),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    filters = EngagementListFilters(query=query, status=status, limit=limit, offset=offset)
    result = _query_service(db).list_engagements(
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
        filters=filters,
    )
    return _sanitize_payload(result)


@router.get("/{engagement_id}")
def get_engagement(
    engagement_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    row = _query_service(db).get_engagement(
        engagement_id=engagement_id,
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Engagement not found")
    return _sanitize_payload(row)


@router.get("/{engagement_id}/summary")
def get_engagement_summary(
    engagement_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    summary = dict(
        _query_service(db).get_summary(
            user_id=int(current_user.id),
            tenant_id=int(engagement.tenant_id),
            engagement_id=engagement_id,
        )
    )
    summary.setdefault("engagement_id", engagement_id)
    return _sanitize_payload(summary)


@router.get("/{engagement_id}/findings", response_model=PaginatedItemsResponse)
def list_findings(
    engagement_id: int,
    severity: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    exploited: bool | str | None = Query(default=None),
    asset: str | None = Query(default=None),
    source: str | None = Query(default=None),
    query: str | None = Query(default=None),
    sort: str = Query(default="last_seen_desc"),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    filters = FindingsFilters(
        severity=severity,
        status=status_filter,
        exploited=exploited,
        asset=asset,
        source=source,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    result = _query_service(db).list_findings(
        filters=filters,
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
    )
    return _sanitize_payload(result)


@router.get("/{engagement_id}/findings/{finding_id}")
def get_finding(
    engagement_id: int,
    finding_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    row = _query_service(db).get_finding(
        finding_id=finding_id,
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")
    return _sanitize_payload(row)


@router.get("/{engagement_id}/assets", response_model=PaginatedItemsResponse)
def list_assets(
    engagement_id: int,
    type_filter: str | None = Query(default=None, alias="type"),
    vulnerable: bool | str | None = Query(default=None),
    exploited: bool | str | None = Query(default=None),
    query: str | None = Query(default=None),
    sort: str = Query(default="last_seen_desc"),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    filters = AssetsFilters(
        type=type_filter,
        vulnerable=vulnerable,
        exploited=exploited,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    result = _query_service(db).list_assets(
        filters=filters,
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
    )
    return _sanitize_payload(result)


@router.get("/{engagement_id}/assets/{asset_id}")
def get_asset(
    engagement_id: int,
    asset_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    row = _query_service(db).get_asset(
        asset_id=asset_id,
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return _sanitize_payload(row)


@router.get("/{engagement_id}/services", response_model=PaginatedItemsResponse)
def list_services(
    engagement_id: int,
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    result = _query_service(db).list_services(
        limit=limit,
        offset=offset,
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
    )
    return _sanitize_payload(result)


@router.get("/{engagement_id}/web-surface")
def list_web_surface_origins(
    engagement_id: int,
    service_key: str = Query(..., min_length=1),
    include_noisy: bool | str | None = Query(default=False),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    result = _query_service(db).list_service_web_surface_origins(
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
        service_key=service_key,
        include_noisy=normalize_optional_bool(include_noisy) is True,
    )
    return _sanitize_payload(result)


@router.get("/{engagement_id}/web-surface/paths")
def list_web_surface_paths(
    engagement_id: int,
    service_key: str = Query(..., min_length=1),
    origin_key: str | None = Query(default=None),
    include_noisy: bool | str | None = Query(default=False),
    limit: int | str | None = Query(default=100),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    result = _query_service(db).list_service_web_surface_paths(
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
        filters=WebSurfacePathsFilters(
            service_key=service_key,
            origin_key=origin_key,
            include_noisy=include_noisy,
            limit=limit,
            offset=offset,
        ),
    )
    return _sanitize_payload(result)


@router.get("/{engagement_id}/evidence", response_model=PaginatedItemsResponse)
def list_evidence(
    engagement_id: int,
    source_tool: str | None = Query(default=None),
    type_filter: str | None = Query(default=None, alias="type"),
    query: str | None = Query(default=None),
    sort: str = Query(default="observed_desc"),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    filters = EvidenceFilters(
        source_tool=source_tool,
        type=type_filter,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    result = _query_service(db).list_evidence(
        filters=filters,
        user_id=int(current_user.id),
        tenant_id=int(engagement.tenant_id),
        engagement_id=engagement_id,
    )
    return _sanitize_payload(result)


@router.post("/{engagement_id}/evidence/{evidence_id}/read", response_model=EvidenceReadResponse)
def read_evidence(
    engagement_id: int,
    evidence_id: str,
    payload: EvidenceReadPayload,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    result = _evidence_read_service(db).read_evidence(
        tenant_id=int(engagement.tenant_id),
        user_id=int(current_user.id),
        engagement_id=engagement_id,
        evidence_id=evidence_id,
        request=KnowledgeEvidenceReadRequest(
            mode=payload.mode,
            query=payload.query,
            max_chars=payload.max_chars,
        ),
    )
    return _sanitize_payload(asdict(result))


@router.get("/{engagement_id}/relationships/graph")
def get_relationship_graph(
    engagement_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    engagement = get_owned_engagement_or_404(
        db=db,
        engagement_id=engagement_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    graph = dict(
        _query_service(db).get_graph_snapshot(
            user_id=int(current_user.id),
            tenant_id=int(engagement.tenant_id),
            engagement_id=engagement_id,
        )
    )
    graph.setdefault("engagement_id", engagement_id)
    return _sanitize_payload(graph)
