"""HTTP response schemas for the Usage Insights endpoints.

Purpose:
    Pydantic v2 models that mirror the backend-internal read dataclasses
    from ``backend.services.usage_tracking.insights_response_models``
    (``UsageInsightsOverview``, ``UsageInsightsGroup``,
    ``UsageInsightsTimelinePoint``, ``UsageInsightsRecord``,
    ``UsageInsightsRecordsPage``) 1:1 so the router wiring in Task 2.3
    can return them directly. Every server-derived metric (cache-hit
    rate, cache ratio, uncached prompt tokens, cache-reporting coverage,
    and the cached / uncached / output cost split) is declared here
    explicitly so the frontend never has to re-derive it.

Responsibility:
    - Declare the typed wire contracts for the four composable insights
      reads (overview, groups, timeline, records).
    - Provide narrow ``from_domain`` classmethod adapters that copy
      fields from the internal dataclasses into the Pydantic models and
      ISO-format ``datetime`` fields for JSON friendliness — matching
      the legacy ``/usage/breakdown`` timestamp convention.
    - Echo the ``"unknown"`` bucket identifier verbatim on group rows
      and on the canonical metadata fields of record rows so historical
      rows without ``request_metadata`` stay explicitly labeled instead
      of silently dropping out of the response.

Boundaries:
    - No cost math, no aggregation, no DB access. Pricing lives in
      ``backend/services/usage_tracking/pricing.py``; aggregation lives
      in ``insights_query_service.py``; write authority stays in
      ``UsageTrackingService``.
    - No imports from ``backend.routers`` — routers depend on schemas,
      not the reverse (see ``backend/schemas/__init__.py``).
    - The legacy ``TokenUsageResponse`` / ``UsageBreakdownItem`` /
      ``UsageBreakdownResponse`` models in ``backend/routers/usage.py``
      are intentionally untouched by this module (``additive-router-only``
      and ``compact-summary-untouched`` from the ownership checklist).
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.services.usage_tracking.insights_response_models import (
    GroupBy,
    UsageInsightsGroup,
    UsageInsightsOverview,
    UsageInsightsRecord as _DomainRecord,
    UsageInsightsRecordsPage,
    UsageInsightsTimelinePoint as _DomainTimelinePoint,
)


# ---------------------------------------------------------------------------
# Typed aliases exposed to downstream callers (router wiring in Task 2.3).
# ---------------------------------------------------------------------------


#: Canonical grouping-key Literal for ``/usage/insights/groups?group_by=...``.
#:
#: Re-exported from the internal dataclass module so the router can declare a
#: single ``group_by: GroupByKey`` parameter without pulling the service layer
#: into its type graph. ``"source"`` is deliberately absent
#: (``no-source-as-grouping-key`` in the ownership checklist).
GroupByKey = GroupBy


#: The three honest cache-reporting states a record row can carry.
#:
#: Declared as a ``Literal`` so FastAPI's OpenAPI schema documents the closed
#: set and ``model_validate`` rejects anything else (``honest-cache-reporting``).
CacheReporting = Literal["reported", "not_reported", "unknown"]
PricingStatus = Literal["available", "partial", "unavailable", "estimated"]


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class UsageInsightsOverviewResponse(BaseModel):
    """Per-task rollup with server-derived cache and cost metrics.

    Mirrors ``UsageInsightsOverview`` exactly. All derived numerics are
    backend-computed (``server-side-derived-metrics``); the frontend
    renders them verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: int
    provider_coverage: Dict[str, int]
    call_count: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    uncached_prompt_tokens: int
    cache_hit_calls: int
    cache_hit_rate: float
    cache_ratio: float
    cache_reporting_call_count: int
    cache_reporting_coverage: float
    cost_usd: float
    cached_input_cost_usd: float
    uncached_input_cost_usd: float
    output_cost_usd: float
    pricing_status: PricingStatus = "available"
    unpriced_providers: List[str] = Field(default_factory=list)
    unpriced_models: List[str] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls, overview: UsageInsightsOverview
    ) -> "UsageInsightsOverviewResponse":
        """Wrap the internal dataclass without recomputing any metric."""
        return cls(
            task_id=overview.task_id,
            provider_coverage=dict(overview.provider_coverage),
            call_count=overview.call_count,
            prompt_tokens=overview.prompt_tokens,
            completion_tokens=overview.completion_tokens,
            cached_tokens=overview.cached_tokens,
            uncached_prompt_tokens=overview.uncached_prompt_tokens,
            cache_hit_calls=overview.cache_hit_calls,
            cache_hit_rate=overview.cache_hit_rate,
            cache_ratio=overview.cache_ratio,
            cache_reporting_call_count=overview.cache_reporting_call_count,
            cache_reporting_coverage=overview.cache_reporting_coverage,
            cost_usd=overview.cost_usd,
            cached_input_cost_usd=overview.cached_input_cost_usd,
            uncached_input_cost_usd=overview.uncached_input_cost_usd,
            output_cost_usd=overview.output_cost_usd,
            pricing_status=overview.pricing_status,  # type: ignore[arg-type]
            unpriced_providers=list(overview.unpriced_providers),
            unpriced_models=list(overview.unpriced_models),
        )


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class UsageInsightsGroupRow(BaseModel):
    """One aggregated bucket row returned by the groups endpoint.

    ``bucket_key`` preserves the normalized metadata value verbatim —
    including the literal string ``"unknown"`` for rows whose
    ``request_metadata`` was missing (``explicit-unknown-buckets``).
    """

    model_config = ConfigDict(extra="forbid")

    bucket_key: str
    call_count: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    uncached_prompt_tokens: int
    cache_hit_calls: int
    cache_hit_rate: float
    cache_ratio: float
    cache_reporting_call_count: int
    cache_reporting_coverage: float
    cost_usd: float
    cached_input_cost_usd: float
    uncached_input_cost_usd: float
    output_cost_usd: float
    pricing_status: PricingStatus = "available"

    @classmethod
    def from_domain(cls, group: UsageInsightsGroup) -> "UsageInsightsGroupRow":
        return cls(
            bucket_key=group.bucket,
            call_count=group.call_count,
            prompt_tokens=group.prompt_tokens,
            completion_tokens=group.completion_tokens,
            cached_tokens=group.cached_tokens,
            uncached_prompt_tokens=group.uncached_prompt_tokens,
            cache_hit_calls=group.cache_hit_calls,
            cache_hit_rate=group.cache_hit_rate,
            cache_ratio=group.cache_ratio,
            cache_reporting_call_count=group.cache_reporting_call_count,
            cache_reporting_coverage=group.cache_reporting_coverage,
            cost_usd=group.cost_usd,
            cached_input_cost_usd=group.cached_input_cost_usd,
            uncached_input_cost_usd=group.uncached_input_cost_usd,
            output_cost_usd=group.output_cost_usd,
            pricing_status=group.pricing_status,  # type: ignore[arg-type]
        )


class UsageInsightsGroupsResponse(BaseModel):
    """Grouped breakdown response for a single ``group_by`` dimension.

    ``group_by`` is a ``Literal`` drawn from the canonical set; passing
    ``"source"`` (or any other non-canonical value) raises a validation
    error at construction time (``no-source-as-grouping-key``).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: int
    group_by: GroupByKey
    items: List[UsageInsightsGroupRow]

    @classmethod
    def from_domain(
        cls,
        *,
        task_id: int,
        group_by: GroupByKey,
        groups: List[UsageInsightsGroup],
    ) -> "UsageInsightsGroupsResponse":
        return cls(
            task_id=task_id,
            group_by=group_by,
            items=[UsageInsightsGroupRow.from_domain(g) for g in groups],
        )


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class UsageInsightsTimelinePoint(BaseModel):
    """One chronological call-level timeline point.

    ``created_at`` is serialized as an ISO-8601 string to match the
    legacy ``/usage/breakdown`` convention so the frontend parses a
    single timestamp shape across every usage endpoint.

    ``cumulative_*`` are server-computed running sums across the points
    emitted from the same ``get_timeline`` call (in chronological
    order). They let the frontend render cumulative trends without
    re-deriving any metric (``server-side-derived-metrics`` /
    ``no-frontend-cost-math``).
    """

    model_config = ConfigDict(extra="forbid")

    created_at: str
    provider: str
    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    pricing_status: PricingStatus = "available"
    cache_ratio: float
    cumulative_prompt_tokens: int
    cumulative_completion_tokens: int
    cumulative_cached_tokens: int
    cumulative_cost_usd: float

    @classmethod
    def from_domain(
        cls, point: _DomainTimelinePoint
    ) -> "UsageInsightsTimelinePoint":
        return cls(
            created_at=_iso(point.created_at),
            provider=point.provider,
            role=point.role,
            model=point.model,
            prompt_tokens=point.prompt_tokens,
            completion_tokens=point.completion_tokens,
            cached_tokens=point.cached_tokens,
            cost_usd=point.cost_usd,
            pricing_status=point.pricing_status,  # type: ignore[arg-type]
            cache_ratio=point.cache_ratio,
            cumulative_prompt_tokens=point.cumulative_prompt_tokens,
            cumulative_completion_tokens=point.cumulative_completion_tokens,
            cumulative_cached_tokens=point.cumulative_cached_tokens,
            cumulative_cost_usd=point.cumulative_cost_usd,
        )


class UsageInsightsTimelineResponse(BaseModel):
    """Chronological per-call timeline for one task."""

    model_config = ConfigDict(extra="forbid")

    task_id: int
    items: List[UsageInsightsTimelinePoint]

    @classmethod
    def from_domain(
        cls, *, task_id: int, points: List[_DomainTimelinePoint]
    ) -> "UsageInsightsTimelineResponse":
        return cls(
            task_id=task_id,
            items=[UsageInsightsTimelinePoint.from_domain(p) for p in points],
        )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class UsageInsightsRecord(BaseModel):
    """One detail row with the full canonical metadata contract.

    Every canonical metadata field (``role``, ``node_name``,
    ``execution_branch``, ``provider``, ``api_surface``,
    ``request_mode``, ``cache_reporting``) is exposed as a plain ``str``
    and defaults to ``"unknown"`` upstream for rows whose
    ``request_metadata`` was missing. ``cache_reporting`` is typed as
    the closed ``CacheReporting`` Literal so FastAPI documents the three
    honest states; anything else would indicate a contract bug upstream.

    ``source`` is surfaced verbatim for debug visibility only — the
    frontend MUST NOT parse it for role/branch (``no-source-as-grouping-key``).
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    created_at: str
    model: str
    source: str
    conversation_id: Optional[str] = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    pricing_status: PricingStatus = "available"
    role: str
    node_name: str
    execution_branch: str
    provider: str
    api_surface: str
    request_mode: str
    cache_reporting: CacheReporting
    turn_index: Optional[int] = None

    @classmethod
    def from_domain(cls, record: _DomainRecord) -> "UsageInsightsRecord":
        return cls(
            id=record.id,
            created_at=_iso(record.created_at),
            model=record.model,
            source=record.source,
            conversation_id=record.conversation_id,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            cached_tokens=record.cached_tokens,
            reasoning_tokens=record.reasoning_tokens,
            cost_usd=record.cost_usd,
            pricing_status=record.pricing_status,  # type: ignore[arg-type]
            role=record.role,
            node_name=record.node_name,
            execution_branch=record.execution_branch,
            provider=record.provider,
            api_surface=record.api_surface,
            request_mode=record.request_mode,
            cache_reporting=record.cache_reporting,  # type: ignore[arg-type]
            turn_index=record.turn_index,
        )


class UsageInsightsRecordsResponse(BaseModel):
    """Paginated detail-record response.

    ``has_more``, ``total_count``, ``page`` and ``page_size`` mirror the
    internal ``UsageInsightsRecordsPage`` exactly; the frontend renders
    them verbatim so pagination UI needs no local re-derivation.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: int
    items: List[UsageInsightsRecord]
    total_count: int
    page: int
    page_size: int
    has_more: bool

    @classmethod
    def from_domain(
        cls, *, task_id: int, page: UsageInsightsRecordsPage
    ) -> "UsageInsightsRecordsResponse":
        return cls(
            task_id=task_id,
            items=[UsageInsightsRecord.from_domain(r) for r in page.items],
            total_count=page.total_count,
            page=page.page,
            page_size=page.page_size,
            has_more=page.has_more,
        )


# ---------------------------------------------------------------------------
# Internal helpers (module-private; no wire surface).
# ---------------------------------------------------------------------------


def _iso(value: Optional[datetime]) -> str:
    """Serialize a timestamp the same way ``/usage/breakdown`` does.

    The legacy breakdown endpoint emits ``record.created_at.isoformat()``
    and ``""`` for ``None``; mirror that so every usage endpoint uses one
    timestamp shape on the wire.
    """
    if value is None:
        return ""
    return value.isoformat()


__all__ = [
    "CacheReporting",
    "GroupByKey",
    "UsageInsightsGroupRow",
    "UsageInsightsGroupsResponse",
    "UsageInsightsOverviewResponse",
    "UsageInsightsRecord",
    "UsageInsightsRecordsResponse",
    "UsageInsightsTimelinePoint",
    "UsageInsightsTimelineResponse",
]
