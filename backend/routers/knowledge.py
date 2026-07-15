"""Tenant/user-scoped knowledge query API router.

Exposes authenticated endpoints for the global knowledge workspace:
summary, findings, assets, services, evidence, and relationship graph.
All user-facing data is scoped to the active tenant and current user."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models.core import Engagement, User
from backend.models.knowledge import KnowledgeEvidenceArchive
from backend.routers.tasks.deps import enforce_tenant_action
from backend.services.knowledge.evidence_read_service import (
    KnowledgeEvidenceReadRequest,
    KnowledgeEvidenceReadService,
)
from backend.services.knowledge.query_service import (
    AssetsFilters,
    EvidenceFilters,
    FindingsFilters,
    KnowledgeQueryService,
)
from backend.services.tenant.authorization import ACTION_KNOWLEDGE_READ
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

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
        "runtime_path",
        "runner_path",
        "object_key",
    }
)


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


def _redact_internal_paths(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    lineage = cleaned.get("lineage")
    if isinstance(lineage, dict):
        cleaned["lineage"] = {
            k: ("<REDACTED>" if k in _REDACTED_PATH_KEYS else v)
            for k, v in lineage.items()
        }
    metadata = cleaned.get("metadata")
    if isinstance(metadata, dict):
        cleaned["metadata"] = {
            k: ("<REDACTED>" if k in _REDACTED_PATH_KEYS else v)
            for k, v in metadata.items()
        }
    return cleaned


@router.get("/summary")
def get_summary(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    return service.get_summary(user_id=current_user.id, tenant_id=int(tenant_context.tenant_id))


@router.get("/findings")
def list_findings(
    severity: str | None = Query(default=None),
    finding_status: str | None = Query(default=None, alias="status"),
    exploited: bool | None = Query(default=None),
    asset: str | None = Query(default=None),
    source: str | None = Query(default=None),
    query: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    include_candidates: bool | None = Query(default=None),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    filters = FindingsFilters(
        severity=severity, status=finding_status, exploited=exploited,
        asset=asset, source=source, query=query, sort=sort,
        include_candidates=include_candidates, limit=limit, offset=offset,
    )
    return service.list_findings(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
        filters=filters,
    )


@router.get("/findings/{finding_id}")
def get_finding(
    finding_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    result = service.get_finding(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
        finding_id=finding_id,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")
    return result


@router.get("/assets")
def list_assets(
    asset_type: str | None = Query(default=None, alias="type"),
    vulnerable: bool | None = Query(default=None),
    exploited: bool | None = Query(default=None),
    query: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    filters = AssetsFilters(
        type=asset_type, vulnerable=vulnerable, exploited=exploited,
        query=query, sort=sort, limit=limit, offset=offset,
    )
    return service.list_assets(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
        filters=filters,
    )


@router.get("/assets/{asset_id}")
def get_asset(
    asset_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    result = service.get_asset(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
        asset_id=asset_id,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return result


@router.get("/services")
def list_services(
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    return service.list_services(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
        limit=limit,
        offset=offset,
    )


@router.get("/evidence")
def list_evidence(
    source_tool: str | None = Query(default=None),
    evidence_type: str | None = Query(default=None, alias="type"),
    query: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    limit: int | str | None = Query(default=20),
    offset: int | str | None = Query(default=0),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    filters = EvidenceFilters(
        source_tool=source_tool, type=evidence_type, query=query,
        sort=sort, limit=limit, offset=offset,
    )
    return _sanitize_payload(
        service.list_evidence(
            user_id=current_user.id,
            tenant_id=int(tenant_context.tenant_id),
            filters=filters,
        )
    )


class EvidenceReadPayload(BaseModel):
    mode: Literal["auto", "head", "tail", "match", "full", "inline"] = "auto"
    query: str | None = None
    max_chars: int = Field(default=4000, ge=1, le=20000)
    # Backward-compatible field for older clients that still send max_bytes.
    max_bytes: int | None = Field(default=None, ge=1, le=20000)


@router.post("/evidence/{evidence_id}/read")
def read_evidence(
    evidence_id: str,
    payload: EvidenceReadPayload,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    row = (
        db.query(KnowledgeEvidenceArchive, Engagement)
        .join(Engagement, KnowledgeEvidenceArchive.engagement_id == Engagement.id)
        .filter(
            KnowledgeEvidenceArchive.id == evidence_id,
            Engagement.tenant_id == int(tenant_context.tenant_id),
            Engagement.user_id == int(current_user.id),
            KnowledgeEvidenceArchive.tenant_id == int(tenant_context.tenant_id),
            KnowledgeEvidenceArchive.user_id == int(current_user.id),
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")
    evidence_row, engagement = row
    engagement_id = int(evidence_row.engagement_id)
    read_service = KnowledgeEvidenceReadService(db)
    request = KnowledgeEvidenceReadRequest(
        mode="auto" if payload.mode == "inline" else payload.mode,
        query=payload.query,
        max_chars=payload.max_bytes if payload.max_bytes is not None else payload.max_chars,
    )
    result = read_service.read_evidence(
        tenant_id=int(engagement.tenant_id),
        user_id=int(current_user.id),
        engagement_id=engagement_id,
        evidence_id=evidence_id,
        request=request,
    )
    return _redact_internal_paths(asdict(result))


@router.get("/relationships/graph")
def get_relationship_graph(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_KNOWLEDGE_READ)
    service = KnowledgeQueryService(db)
    return service.get_graph_snapshot(
        user_id=current_user.id,
        tenant_id=int(tenant_context.tenant_id),
    )
