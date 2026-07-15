"""Token usage API endpoints.

This module provides endpoints for querying token usage data captured
from actual LLM API responses (not tiktoken estimates).

Endpoints:
    GET /api/tasks/{task_id}/usage - Get aggregated usage summary for a task
    GET /api/tasks/{task_id}/usage/breakdown - Get per-call breakdown
    GET /api/tasks/{task_id}/usage/cost - Legacy endpoint (deprecated)

Usage Insights endpoints (additive; same task-scoped auth/ownership checks
as the legacy routes above — see ``additive-router-only`` and
``task-scoped-backend-v1`` in the ownership checklist):
    GET /api/tasks/{task_id}/usage/insights/overview
    GET /api/tasks/{task_id}/usage/insights/groups?group_by=...
    GET /api/tasks/{task_id}/usage/insights/timeline
    GET /api/tasks/{task_id}/usage/insights/records
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import LLMUsageRecord, Task, User
from ..routers.tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from ..schemas.usage_insights import (
    GroupByKey,
    UsageInsightsGroupsResponse,
    UsageInsightsOverviewResponse,
    UsageInsightsRecordsResponse,
    UsageInsightsTimelineResponse,
)
from ..services.usage_tracking import UsageTrackingService, calculate_cost
from ..services.usage_tracking.insights_query_service import (
    UsageInsightsQueryService,
)
from ..services.usage_tracking.insights_response_models import InsightsFilters
from ..services.usage_tracking.pricing import (
    get_pricing_quote,
    pricing_status_for_usage,
    usage_from_persisted_record,
)
from ..services.tenant.authorization import ACTION_USAGE_EXPORT, ACTION_USAGE_READ
from ..services.tenant.context import TenantRequestContext
from ..services.tenant.dependencies import get_tenant_request_context
from agent.providers.llm.core.identity import ProviderModelRef

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["usage"])


# =============================================================================
# Response Models
# =============================================================================

class TokenUsageResponse(BaseModel):
    """Aggregated token usage for a task."""
    task_id: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    pricing_status: str = "available"
    unpriced_providers: List[str] = Field(default_factory=list)
    unpriced_models: List[str] = Field(default_factory=list)
    call_count: int
    models: List[str]
    first_call: Optional[str] = None
    last_call: Optional[str] = None


class UsageBreakdownItem(BaseModel):
    """Individual LLM call usage record."""
    id: int
    provider: str
    model: str
    source: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    pricing_status: str
    created_at: str
    conversation_id: Optional[str] = None


class UsageBreakdownResponse(BaseModel):
    """Paginated breakdown of per-call usage."""
    task_id: int
    items: List[UsageBreakdownItem]
    total_count: int
    page: int
    page_size: int
    has_more: bool


class TenantUsageExportResponse(BaseModel):
    """Tenant-scoped usage export summary across all visible tenant tasks."""

    tenant_id: int
    task_count: int
    call_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    pricing_status: str = "available"
    unpriced_providers: List[str] = Field(default_factory=list)
    unpriced_models: List[str] = Field(default_factory=list)
    models: List[str] = Field(default_factory=list)


# =============================================================================
# Endpoints
# =============================================================================

@router.get("/tasks/{task_id}/usage", response_model=TokenUsageResponse)
async def get_task_usage(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TokenUsageResponse:
    """Get aggregated token usage and cost for a task.
    
    Returns actual token counts captured from LLM API responses.
    This replaces the legacy /usage/cost endpoint which used tiktoken estimates.
    
    Args:
        task_id: ID of the task to get usage for
        current_user: Authenticated user (from JWT)
        db: Database session
        
    Returns:
        TokenUsageResponse with aggregated usage and cost
        
    Raises:
        404: Task not found or not owned by current user
    """
    del current_user
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_USAGE_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    # Get aggregated usage from service
    service = UsageTrackingService(db)
    summary = service.get_task_usage(task_id, tenant_id=int(tenant_context.tenant_id))
    
    return TokenUsageResponse(
        task_id=task_id,
        prompt_tokens=summary.total_prompt_tokens,
        completion_tokens=summary.total_completion_tokens,
        total_tokens=summary.total_tokens,
        cached_tokens=summary.total_cached_tokens,
        reasoning_tokens=summary.total_reasoning_tokens,
        cost_usd=round(summary.total_cost_usd, 6),
        pricing_status=summary.pricing_status,
        unpriced_providers=summary.unpriced_providers,
        unpriced_models=summary.unpriced_models,
        call_count=summary.call_count,
        models=summary.models_used,
        first_call=summary.first_call.isoformat() if summary.first_call else None,
        last_call=summary.last_call.isoformat() if summary.last_call else None,
    )


@router.get("/tasks/{task_id}/usage/breakdown", response_model=UsageBreakdownResponse)
async def get_task_usage_breakdown(
    task_id: int,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=50, ge=1, le=100, description="Items per page"),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> UsageBreakdownResponse:
    """Get per-call breakdown of token usage for a task.
    
    Returns individual usage records for each LLM call, useful for
    debugging and detailed cost analysis.
    
    Args:
        task_id: ID of the task
        page: Page number (1-indexed)
        page_size: Number of items per page (max 100)
        current_user: Authenticated user
        db: Database session
        
    Returns:
        UsageBreakdownResponse with paginated usage records
        
    Raises:
        404: Task not found or not owned by current user
    """
    del current_user
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_USAGE_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    # Get breakdown with pagination
    service = UsageTrackingService(db)
    offset = (page - 1) * page_size
    records = service.get_task_usage_breakdown(
        task_id,
        tenant_id=int(tenant_context.tenant_id),
        limit=page_size + 1,
        offset=offset,
    )
    
    # Check if there are more records
    has_more = len(records) > page_size
    if has_more:
        records = records[:page_size]
    
    # Convert records to response items
    items: List[UsageBreakdownItem] = []
    for record in records:
        usage = usage_from_persisted_record(record)
        cost = calculate_cost(usage)
        pricing_status = pricing_status_for_usage(usage)
        
        items.append(UsageBreakdownItem(
            id=record.id,
            provider=record.provider,
            model=record.model,
            source=record.source,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            cached_tokens=record.cached_tokens,
            reasoning_tokens=record.reasoning_tokens,
            cost_usd=round(cost, 8),
            pricing_status=pricing_status,
            created_at=record.created_at.isoformat() if record.created_at else "",
            conversation_id=record.conversation_id,
        ))
    
    # Get total count for pagination info
    summary = service.get_task_usage(
        task_id,
        tenant_id=int(tenant_context.tenant_id),
        use_cache=False,
    )
    
    return UsageBreakdownResponse(
        task_id=task_id,
        items=items,
        total_count=summary.call_count,
        page=page,
        page_size=page_size,
        has_more=has_more,
    )


# =============================================================================
# Legacy Endpoint (Deprecated)
# =============================================================================

@router.get("/tasks/{task_id}/usage/cost")
async def get_task_usage_cost(
    task_id: int,
    include_inputs: bool = Query(default=False, deprecated=True),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Calculate cost estimate for a task (DEPRECATED).
    
    **DEPRECATED**: Use GET /api/tasks/{task_id}/usage instead.
    
    This endpoint now returns actual usage from LLMUsageRecord table
    rather than tiktoken-based estimates.
    
    Args:
        task_id: ID of the task
        include_inputs: Deprecated parameter, ignored
        current_user: Authenticated user
        db: Database session
        
    Returns:
        Legacy format response with actual usage data
    """
    del current_user, include_inputs
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_USAGE_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    # Get actual usage from service
    service = UsageTrackingService(db)
    summary = service.get_task_usage(task_id, tenant_id=int(tenant_context.tenant_id))
    
    provider_rows = db.execute(
        select(LLMUsageRecord.provider, LLMUsageRecord.model)
        .where(
            LLMUsageRecord.task_id == task_id,
            LLMUsageRecord.tenant_id == int(tenant_context.tenant_id),
        )
        .distinct()
    ).all()
    unpriced_providers = sorted(summary.unpriced_providers)
    if unpriced_providers:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Usage pricing is unavailable for provider(s): "
                + ", ".join(unpriced_providers)
            ),
        )

    primary_provider = provider_rows[0][0] if provider_rows else ""
    primary_model = provider_rows[0][1] if provider_rows else ""
    quote = (
        get_pricing_quote(ProviderModelRef(str(primary_provider or "openai"), str(primary_model)))
        if primary_model
        else None
    )
    output_price = (
        quote.schedule.component_prices_per_million.get("output_tokens")
        if quote is not None and quote.schedule is not None
        else None
    )
    
    # Legacy format response
    return {
        "taskId": task_id,
        "provider": primary_provider,
        "model": primary_model,
        "output_tokens": summary.total_completion_tokens,
        "input_tokens": summary.total_prompt_tokens,
        "total_tokens": summary.total_tokens,
        "price_per_1k": (
            float(output_price) / 1000 if output_price is not None else 0.0
        ),
        "cost_usd": round(summary.total_cost_usd, 6),
        "pricing_status": summary.pricing_status,
        "unpriced_models": summary.unpriced_models,
        # New fields for clients that support them
        "prompt_tokens": summary.total_prompt_tokens,
        "completion_tokens": summary.total_completion_tokens,
        "cached_tokens": summary.total_cached_tokens,
        "call_count": summary.call_count,
    }


@router.get("/usage/tenant/export", response_model=TenantUsageExportResponse)
async def export_tenant_usage(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> TenantUsageExportResponse:
    """Export tenant-wide usage summary across tenant-scoped tasks."""

    del current_user
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_USAGE_READ)
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_USAGE_EXPORT)
    service = UsageTrackingService(db)
    payload = service.get_tenant_usage_export(tenant_id=int(tenant_context.tenant_id))
    return TenantUsageExportResponse(**payload)


# =============================================================================
# Usage Insights Endpoints (additive; Phase 2 Task 2.3)
# =============================================================================
#
# Each handler:
#   1. Verifies task ownership with the same ``Task.id == task_id AND
#      Task.user_id == current_user.id`` check the legacy ``/usage`` route
#      uses — so unauthenticated or cross-tenant callers get the same 401/404
#      parity (``task-scoped-backend-v1``).
#   2. Builds an ``InsightsFilters`` envelope from the optional query params.
#      A missing (``None``) filter means "no filter on this dimension" and is
#      NOT collapsed into the literal ``"unknown"`` bucket — callers that
#      want historical rows still have to pass ``role="unknown"`` explicitly.
#   3. Calls one composable method on ``UsageInsightsQueryService`` and wraps
#      the internal dataclass via the matching ``from_domain`` adapter. The
#      router performs no cost math, aggregation, or cache-reporting
#      massaging (``single-pricing-authority``, ``server-side-derived-metrics``,
#      ``honest-cache-reporting``).


def _verify_task_scope(
    task_id: int,
    tenant_context: TenantRequestContext,
    db: Session,
) -> Task:
    """Return tenant-scoped task or raise 404."""

    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_USAGE_READ)
    return get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )


def _build_insights_filters(
    *,
    conversation_id: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    role: Optional[str],
    execution_branch: Optional[str],
) -> Optional[InsightsFilters]:
    """Build an ``InsightsFilters`` envelope, or ``None`` when every field is unset.

    Returning ``None`` matches the service contract ("no filter at all") and
    is the cheapest path — the service short-circuits the row-match loop when
    no filter is supplied.
    """
    if (
        conversation_id is None
        and provider is None
        and model is None
        and role is None
        and execution_branch is None
    ):
        return None
    return InsightsFilters(
        conversation_id=conversation_id,
        provider=provider,
        model=model,
        role=role,
        execution_branch=execution_branch,
    )


@router.get(
    "/tasks/{task_id}/usage/insights/overview",
    response_model=UsageInsightsOverviewResponse,
)
async def get_task_usage_insights_overview(
    task_id: int,
    conversation_id: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    execution_branch: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> UsageInsightsOverviewResponse:
    """Return a per-task rollup with server-derived cache and cost metrics."""
    del current_user
    _verify_task_scope(task_id, tenant_context, db)
    filters = _build_insights_filters(
        conversation_id=conversation_id,
        provider=provider,
        model=model,
        role=role,
        execution_branch=execution_branch,
    )
    service = UsageInsightsQueryService(db)
    overview = service.get_overview(
        task_id,
        tenant_id=int(tenant_context.tenant_id),
        filters=filters,
    )
    return UsageInsightsOverviewResponse.from_domain(overview)


@router.get(
    "/tasks/{task_id}/usage/insights/groups",
    response_model=UsageInsightsGroupsResponse,
)
async def get_task_usage_insights_groups(
    task_id: int,
    group_by: GroupByKey = Query(..., description="Canonical grouping dimension"),
    conversation_id: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    execution_branch: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> UsageInsightsGroupsResponse:
    """Return bucketed rows for one canonical metadata dimension.

    ``group_by`` is typed as the ``GroupByKey`` Literal, so FastAPI returns
    422 automatically for ``"source"`` or any other unsupported value
    (``no-source-as-grouping-key``). ``"unknown"`` buckets pass through
    verbatim (``explicit-unknown-buckets``).
    """
    del current_user
    _verify_task_scope(task_id, tenant_context, db)
    filters = _build_insights_filters(
        conversation_id=conversation_id,
        provider=provider,
        model=model,
        role=role,
        execution_branch=execution_branch,
    )
    service = UsageInsightsQueryService(db)
    groups = service.get_groups(
        task_id,
        tenant_id=int(tenant_context.tenant_id),
        group_by=group_by,
        filters=filters,
    )
    return UsageInsightsGroupsResponse.from_domain(
        task_id=task_id,
        group_by=group_by,
        groups=groups,
    )


@router.get(
    "/tasks/{task_id}/usage/insights/timeline",
    response_model=UsageInsightsTimelineResponse,
)
async def get_task_usage_insights_timeline(
    task_id: int,
    conversation_id: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    execution_branch: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> UsageInsightsTimelineResponse:
    """Return chronological per-call timeline points for the task."""
    del current_user
    _verify_task_scope(task_id, tenant_context, db)
    filters = _build_insights_filters(
        conversation_id=conversation_id,
        provider=provider,
        model=model,
        role=role,
        execution_branch=execution_branch,
    )
    service = UsageInsightsQueryService(db)
    points = service.get_timeline(
        task_id,
        tenant_id=int(tenant_context.tenant_id),
        filters=filters,
    )
    return UsageInsightsTimelineResponse.from_domain(task_id=task_id, points=points)


@router.get(
    "/tasks/{task_id}/usage/insights/records",
    response_model=UsageInsightsRecordsResponse,
)
async def get_task_usage_insights_records(
    task_id: int,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Items per page (1..200)",
    ),
    conversation_id: Optional[str] = Query(default=None),
    provider: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    execution_branch: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> UsageInsightsRecordsResponse:
    """Return a paginated slice of per-call detail rows (newest first)."""
    del current_user
    _verify_task_scope(task_id, tenant_context, db)
    filters = _build_insights_filters(
        conversation_id=conversation_id,
        provider=provider,
        model=model,
        role=role,
        execution_branch=execution_branch,
    )
    service = UsageInsightsQueryService(db)
    page_obj = service.get_records(
        task_id,
        tenant_id=int(tenant_context.tenant_id),
        page=page,
        page_size=page_size,
        filters=filters,
    )
    return UsageInsightsRecordsResponse.from_domain(task_id=task_id, page=page_obj)


__all__ = ["router"]
