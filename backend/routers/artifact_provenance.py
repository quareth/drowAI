"""
Artifact provenance query API endpoints.

This router exposes read-only endpoints for execution/artifact provenance data
with tenant/user-owned task checks on every request path.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.database import get_db
from backend.models.core import User
from backend.services.artifact.memory_service import (
    ArtifactMemoryService,
    ArtifactReadRequest,
    ArtifactSearchFilters,
)
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from backend.services.tenant.authorization import ACTION_ARTIFACT_READ
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context
from backend.routers.tasks.deps import enforce_tenant_action, get_tenant_task_or_404

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/artifact-provenance", tags=["artifact_provenance"])


class ExecutionResponse(BaseModel):
    """Execution response shape used by execution lookup endpoints."""

    execution: Dict[str, Any]
    artifacts: Optional[list[Dict[str, Any]]] = None


class PaginatedExecutionsResponse(BaseModel):
    """Paginated list response for execution queries."""

    executions: list[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class TimelineResponse(BaseModel):
    """Paginated timeline response for task execution chronology."""

    timeline: list[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class ArtifactResponse(BaseModel):
    """Artifact detail response used for direct artifact lookup."""

    artifact_id: str
    execution_id: str
    task_id: int
    artifact_kind: str
    relative_path: Optional[str] = None
    content_text: Optional[str] = None
    content_sha256: Optional[str] = None
    byte_size: Optional[int] = None
    mime_type: Optional[str] = None
    is_text: bool
    content_availability: str
    artifact_metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


class PaginatedArtifactsResponse(BaseModel):
    """Paginated list response for artifact search within a task."""

    artifacts: list[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class ArtifactCatalogResponse(BaseModel):
    """Paginated artifact catalog response with execution lineage fields."""

    artifacts: list[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class ArtifactReadPayload(BaseModel):
    """Request payload for bounded artifact reads."""

    mode: Literal["auto", "head", "tail", "match", "full"] = "auto"
    query: Optional[str] = None
    max_chars: int = Field(default=4000, ge=1, le=20000)


class ArtifactReadResponse(BaseModel):
    """Bounded read response shape, distinct from artifact detail payload."""

    status: Literal["ready", "not_found", "not_available", "omitted_by_policy"]
    artifact_id: str
    content: Optional[str] = None
    content_availability: str
    availability_state: Literal["ready", "pending", "failed", "not_available", "not_found"]
    mode_used: Literal["auto", "head", "tail", "match", "full"]
    truncated: bool
    source: Literal["inline_db", "object_store", "workspace_file", "none"]
    artifact: Optional[Dict[str, Any]] = None


class RawOutputBatchRequest(BaseModel):
    """Batch lookup request for tool-call terminal raw outputs."""

    tool_call_ids: List[str]


class RawOutputBatchResponse(BaseModel):
    """Batch lookup response for tool-call terminal raw outputs."""

    results: Dict[str, Dict[str, Any]]
    missing: List[str]


def _query_service(db: Session) -> ArtifactProvenanceQueryService:
    return ArtifactProvenanceQueryService(db)


def _artifact_memory_service(db: Session) -> ArtifactMemoryService:
    return ArtifactMemoryService(db)


_OWNERSHIP_ERROR_RESPONSES: Dict[int, Dict[str, str]] = {
    401: {"description": "Authentication required or invalid credentials."},
    404: {"description": "Task, execution, or artifact not found for current user."},
    500: {"description": "Unexpected server error while querying provenance data."},
}

EXECUTION_NOT_FOUND_DETAIL = "Execution not found"
ARTIFACT_NOT_FOUND_DETAIL = "Artifact not found"
_READY_AVAILABILITY = frozenset({"ready", "available", "available_inline", "available_object"})
_PENDING_AVAILABILITY = frozenset({"upload_pending", "pending"})
_FAILED_AVAILABILITY = frozenset({"upload_failed", "failed"})


def _authorize_artifact_read_for_task(
    *,
    db: Session,
    task_id: int,
    tenant_context: TenantRequestContext,
) -> None:
    """Enforce tenant action policy and tenant/user-owned task access."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_ARTIFACT_READ)
    get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )


def _availability_state(content_availability: Any) -> str:
    """Map detailed availability labels to stable ready/pending/failed states."""
    normalized = str(content_availability or "").strip().lower()
    if normalized == "not_found":
        return "not_found"
    if normalized in _READY_AVAILABILITY:
        return "ready"
    if normalized in _PENDING_AVAILABILITY:
        return "pending"
    if normalized in _FAILED_AVAILABILITY:
        return "failed"
    return "not_available"


@router.get(
    "/tasks/{task_id}/executions/{execution_id}",
    response_model=ExecutionResponse,
    summary="Get execution by ID",
    description="Return one execution record and optional artifact metadata for an owned task.",
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_execution(
    task_id: int,
    execution_id: str,
    include_artifacts: bool = Query(
        default=True,
        description="When true, include linked artifact metadata in the response.",
    ),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get one execution by ID, constrained to an owned task."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        result = _query_service(db).get_execution_by_id(
            execution_id=execution_id,
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            include_artifacts=include_artifacts,
        )
        if not result:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch execution %s for task %s", execution_id, task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/executions/by-tool-call/{tool_call_id}",
    response_model=ExecutionResponse,
    summary="Get execution by task-scoped tool_call_id",
    description=(
        "Resolve a LangGraph tool_call_id within one task. Task scoping prevents "
        "cross-task collisions for repeated tool_call_id values."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_execution_by_tool_call(
    task_id: int,
    tool_call_id: str,
    include_artifacts: bool = Query(
        default=True,
        description="When true, include linked artifact metadata in the response.",
    ),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get execution by task-scoped tool_call_id.

    Failure contract for frontend raw-output resolution:
    - Missing execution returns HTTP 404 with detail "Execution not found".
    - Present execution includes `execution.raw_output` availability summary
      (command/stdout/stderr artifact identifiers when available).
    """
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        result = _query_service(db).get_execution_by_tool_call_id(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            tool_call_id=tool_call_id,
            include_artifacts=include_artifacts,
        )
        if not result:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EXECUTION_NOT_FOUND_DETAIL)
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to fetch execution by tool call. task_id=%s tool_call_id=%s",
            task_id,
            tool_call_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.post(
    "/tasks/{task_id}/raw-output/batch",
    response_model=RawOutputBatchResponse,
    summary="Batch resolve tool raw output states by tool_call_id",
    description=(
        "Resolve terminal raw-output state for multiple task-scoped tool_call_id "
        "values in a single request."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_raw_output_batch(
    task_id: int,
    payload: RawOutputBatchRequest,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return raw output states for many task-scoped tool_call_id values."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        return _query_service(db).get_raw_output_batch(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            tool_call_ids=payload.tool_call_ids,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to resolve raw-output batch for task %s (count=%s)",
            task_id,
            len(payload.tool_call_ids),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/executions",
    response_model=PaginatedExecutionsResponse,
    summary="List task executions",
    description=(
        "Return paginated tool execution records for an owned task with optional "
        "filters by tool name, status, and start-time range."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_task_executions(
    task_id: int,
    tool_name: Optional[str] = Query(default=None, description="Filter by exact tool name."),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description="Filter by execution status (for example: success, error, timeout, started).",
    ),
    start_time: Optional[datetime] = Query(
        default=None,
        description="Inclusive lower bound for execution start time (ISO 8601).",
    ),
    end_time: Optional[datetime] = Query(
        default=None,
        description="Inclusive upper bound for execution start time (ISO 8601).",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum rows to return."),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    include_artifacts: bool = Query(
        default=False,
        description="When true, embed linked artifact metadata on each execution row.",
    ),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get paginated execution records for one task."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        return _query_service(db).get_task_executions(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            tool_name=tool_name,
            status=status_filter,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
            include_artifacts=include_artifacts,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list executions for task %s", task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/timeline",
    response_model=TimelineResponse,
    summary="Get task execution timeline",
    description=(
        "Return a chronological timeline of tool executions for an owned task, "
        "including artifact counts per execution."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_task_timeline(
    task_id: int,
    limit: int = Query(default=50, ge=1, le=1000, description="Maximum timeline entries."),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get chronological timeline of tool executions for one task."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        return _query_service(db).get_tool_execution_timeline(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch timeline for task %s", task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/conversations/{conversation_id}/executions",
    response_model=PaginatedExecutionsResponse,
    summary="List conversation executions",
    description=(
        "Return execution rows within one task conversation. Optionally filter "
        "to a specific turn_id."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_conversation_executions(
    task_id: int,
    conversation_id: str,
    turn_id: Optional[str] = Query(default=None, description="Optional turn identifier filter."),
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum rows to return."),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    include_artifacts: bool = Query(
        default=True,
        description="When true, include linked artifact metadata for each execution.",
    ),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get paginated executions for one conversation (and optional turn)."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        return _query_service(db).get_conversation_executions(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            conversation_id=conversation_id,
            turn_id=turn_id,
            limit=limit,
            offset=offset,
            include_artifacts=include_artifacts,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to list conversation executions. task_id=%s conversation_id=%s",
            task_id,
            conversation_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/artifacts/{artifact_id}",
    response_model=ArtifactResponse,
    summary="Get artifact by ID",
    description=(
        "Return one artifact metadata row for an owned task. "
        "Use the bounded read endpoint for content retrieval."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_artifact(
    task_id: int,
    artifact_id: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get one artifact by ID, constrained to an owned task.

    This endpoint is metadata-only and intentionally omits inline content.
    Use `/artifacts/{artifact_id}/read` for bounded excerpt-first content reads.
    """
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        result = _query_service(db).get_artifact_by_id(
            artifact_id,
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
        )
        if not result:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ARTIFACT_NOT_FOUND_DETAIL)
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch artifact %s for task %s", artifact_id, task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/artifacts",
    response_model=PaginatedArtifactsResponse,
    summary="Search task artifacts",
    description=(
        "Return paginated artifacts for an owned task, with optional filtering "
        "by artifact kind."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def search_task_artifacts(
    task_id: int,
    artifact_kind: Optional[str] = Query(
        default=None,
        description="Filter by artifact kind (stdout, stderr, tool_file, other).",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum rows to return."),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Search artifacts for one task with optional kind filter."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        return _query_service(db).search_artifacts(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            artifact_kind=artifact_kind,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to search artifacts for task %s", task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.get(
    "/tasks/{task_id}/artifact-catalog",
    response_model=ArtifactCatalogResponse,
    summary="Get task artifact catalog",
    description=(
        "Return task-scoped artifact catalog rows joined with execution lineage, "
        "with deterministic labels and practical metadata filters."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def get_task_artifact_catalog(
    task_id: int,
    tool_name: Optional[str] = Query(default=None, description="Filter by exact tool name."),
    artifact_kind: Optional[str] = Query(default=None, description="Filter by artifact kind."),
    execution_id: Optional[str] = Query(default=None, description="Filter by execution UUID."),
    turn_id: Optional[str] = Query(default=None, description="Filter by turn identifier."),
    conversation_id: Optional[str] = Query(default=None, description="Filter by conversation identifier."),
    query_text: Optional[str] = Query(
        default=None,
        alias="query",
        description="Case-insensitive substring match against label and relative_path.",
    ),
    limit: int = Query(default=20, ge=1, le=1000, description="Maximum rows to return."),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return joined artifact catalog rows for one owned task."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        filters = ArtifactSearchFilters(
            query=query_text,
            tool_name=tool_name,
            artifact_kind=artifact_kind,
            execution_id=execution_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            limit=limit,
            offset=offset,
        )
        page = _artifact_memory_service(db).search_task_artifacts(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            filters=filters,
        )
        payload = ArtifactMemoryService.catalog_page_to_dict(page)
        for artifact in payload.get("artifacts", []):
            artifact["availability_state"] = _availability_state(artifact.get("content_availability"))
        return payload
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch artifact catalog for task %s", task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )


@router.post(
    "/tasks/{task_id}/artifacts/{artifact_id}/read",
    response_model=ArtifactReadResponse,
    summary="Read artifact content with bounded excerpt policy",
    description=(
        "Return task-scoped artifact content slices using `artifact_id` resolution "
        "and excerpt-first read behavior."
    ),
    responses=_OWNERSHIP_ERROR_RESPONSES,
)
def read_task_artifact(
    task_id: int,
    artifact_id: str,
    payload: ArtifactReadPayload,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Read one task artifact through shared bounded-read policy."""
    try:
        _authorize_artifact_read_for_task(db=db, task_id=task_id, tenant_context=tenant_context)
        request = ArtifactReadRequest(
            mode=payload.mode,
            query=payload.query,
            max_chars=payload.max_chars,
        )
        result = _artifact_memory_service(db).read_task_artifact(
            task_id=task_id,
            tenant_id=int(tenant_context.tenant_id),
            artifact_id=artifact_id,
            request=request,
            user_id=current_user.id,
        )
        artifact_payload = asdict(result.artifact) if result.artifact is not None else None
        if artifact_payload is not None:
            artifact_payload["availability_state"] = _availability_state(
                artifact_payload.get("content_availability")
            )
        return {
            "status": result.status,
            "artifact_id": result.artifact_id,
            "content": result.content,
            "content_availability": result.content_availability,
            "availability_state": _availability_state(result.content_availability),
            "mode_used": result.mode_used,
            "truncated": result.truncated,
            "source": result.source,
            "artifact": artifact_payload,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to read artifact %s for task %s",
            artifact_id,
            task_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query artifact provenance",
        )
