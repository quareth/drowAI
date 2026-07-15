"""Internal read models for the Usage Insights query layer.

Purpose:
    Typed, backend-internal containers that
    ``UsageInsightsQueryService`` returns from its four composable read
    methods (overview, groups, timeline, records). Keeping these as plain
    frozen dataclasses lets the router layer (Task 2.2) wrap them in
    Pydantic response models without coupling this module to FastAPI or
    Pydantic. The router owns the wire contract; this module owns the
    in-process contract.

Responsibility:
    - Declare the filter envelope (`InsightsFilters`) and grouping-key
      enumeration (`GroupBy`) shared by every read method.
    - Declare the four return shapes (overview / group row / timeline
      point / records page) with every server-derived metric named
      explicitly — ``cache_hit_rate``, ``cache_ratio``,
      ``uncached_prompt_tokens``, ``cache_reporting_coverage``, and the
      three cost-split fields — so the frontend never has to re-derive
      them.
    - Pair each record row with its normalized
      ``UsageRecordMetadata``-shaped dict so historical rows with
      ``request_metadata = NULL`` still carry ``"unknown"`` for every
      canonical field.

Boundaries:
    - No database access, no FastAPI / Pydantic imports, no cost math.
      Aggregation and cost math live in
      ``insights_query_service.py``; pricing math lives in
      ``pricing.py``; the canonical write-time metadata contract lives
      in ``insights_models.py``.
    - Response-facing Pydantic schemas for the HTTP boundary are
      introduced in Task 2.2 (``backend/schemas/usage_insights.py`` or a
      sibling) and wrap these dataclasses — they are not defined here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional


# ---------------------------------------------------------------------------
# Filters and grouping keys shared by every read method on the service.
# ---------------------------------------------------------------------------


#: Canonical grouping keys supported by ``UsageInsightsQueryService.get_groups``.
#:
#: ``model`` reads from the row column; every other key reads from the
#: normalized ``request_metadata`` dict. ``source`` is deliberately absent —
#: see ``no-source-as-grouping-key`` in the ownership checklist.
GroupBy = Literal[
    "role",
    "node_name",
    "execution_branch",
    "provider",
    "model",
    "api_surface",
]


#: All valid ``GroupBy`` values at runtime. Kept in sync with the ``Literal``
#: above so the service can validate input without importing ``typing_extensions``.
GROUP_BY_VALUES: tuple[str, ...] = (
    "role",
    "node_name",
    "execution_branch",
    "provider",
    "model",
    "api_surface",
)


@dataclass(slots=True, frozen=True)
class InsightsFilters:
    """Optional narrow filters applied uniformly to all four read methods.

    Every field defaults to ``None`` which means "no filter on this
    dimension". When a field is set, rows must match exactly on the
    normalized value (``"unknown"`` for missing metadata) — callers that
    want to target historical rows can pass ``role="unknown"`` and that
    will match rows with ``request_metadata = NULL``.

    ``model`` and ``conversation_id`` read from the row columns; every
    other field reads from the normalized ``request_metadata`` dict.
    """

    conversation_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    role: Optional[str] = None
    execution_branch: Optional[str] = None
    api_surface: Optional[str] = None


# ---------------------------------------------------------------------------
# Overview (per-task totals + derived metrics).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UsageInsightsOverview:
    """Per-task rollup of token counts, cache behavior and cost splits.

    Every numeric field is computed server-side from the matching rows
    (honoring the supplied ``InsightsFilters``). Derived fields:

    - ``uncached_prompt_tokens = max(0, prompt_tokens - cached_tokens)``
    - ``cache_hit_calls`` counts rows where ``cached_tokens > 0`` AND
      ``cache_reporting == "reported"``.
    - ``cache_reporting_call_count`` counts rows where
      ``cache_reporting == "reported"``.
    - ``cache_hit_rate = cache_hit_calls / cache_reporting_call_count``
      when the denominator is positive, else ``0.0``.
    - ``cache_ratio = cached_tokens_reporting / prompt_tokens_reporting``
      where both numerator and denominator are restricted to rows with
      ``cache_reporting == "reported"``, else ``0.0``.
    - ``cache_reporting_coverage = cache_reporting_call_count / call_count``
      when the denominator is positive, else ``0.0``.
    - ``cost_usd``, ``cached_input_cost_usd``, ``uncached_input_cost_usd``,
      ``output_cost_usd`` are all computed per-row via
      ``pricing.calculate_cost_components`` and then summed. The three
      split fields sum to ``cost_usd`` within float tolerance.
    """

    task_id: int
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
    pricing_status: str = "available"
    unpriced_providers: List[str] = field(default_factory=list)
    unpriced_models: List[str] = field(default_factory=list)
    provider_coverage: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Group rows (same numeric fields at bucket scope).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UsageInsightsGroup:
    """Aggregated metrics for one bucket of a ``get_groups`` response.

    ``group_by`` identifies which dimension was used (e.g. ``"role"``) and
    ``bucket`` is the normalized value (e.g. ``"planner"`` or
    ``"unknown"``). Numeric fields mirror the overview contract at bucket
    scope so the frontend can render any dimension with the same
    component.
    """

    group_by: str
    bucket: str
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
    pricing_status: str = "available"


# ---------------------------------------------------------------------------
# Timeline points (v1: chronological per-call points, no bucketing).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UsageInsightsTimelinePoint:
    """One chronological call-level point for the timeline chart.

    ``cache_ratio`` is the per-row ratio
    ``cached_tokens / prompt_tokens`` (``0.0`` when ``prompt_tokens == 0``
    OR when ``cache_reporting != "reported"`` — non-reporting rows must
    not masquerade as honest zeros). ``cost_usd`` reuses the row's
    persisted per-row cost math via ``pricing.calculate_cost``.

    The ``cumulative_*`` fields are server-computed running sums across
    the already-sorted (chronological, deterministic) timeline so the
    frontend can chart cumulative trends without re-deriving any metric
    (``server-side-derived-metrics`` / ``no-frontend-cost-math``). They
    are re-computed per call so each ``get_timeline`` invocation sums
    only its filtered set — filter changes do not carry running totals
    across from a prior call. No cumulative ratio is exposed; ratios
    re-derive per frame and stay a rendering concern if ever needed.
    """

    created_at: datetime
    provider: str
    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    cache_ratio: float
    cumulative_prompt_tokens: int
    cumulative_completion_tokens: int
    cumulative_cached_tokens: int
    cumulative_cost_usd: float
    pricing_status: str = "available"


# ---------------------------------------------------------------------------
# Paginated detail records.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UsageInsightsRecord:
    """One row of the paginated detail view.

    ``source`` is surfaced verbatim from the row column for debug
    visibility only — it MUST NOT be parsed by the frontend. All
    grouping-relevant fields come from the canonical metadata keys
    (``role``, ``node_name``, ``execution_branch``, ``provider``,
    ``api_surface``, ``request_mode``, ``cache_reporting``) which default
    to ``"unknown"`` for rows with missing ``request_metadata``.
    """

    id: int
    created_at: datetime
    model: str
    source: str
    conversation_id: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    role: str
    node_name: str
    execution_branch: str
    provider: str
    api_surface: str
    request_mode: str
    cache_reporting: str
    turn_index: Optional[int]
    pricing_status: str = "available"


@dataclass(slots=True, frozen=True)
class UsageInsightsRecordsPage:
    """One page of detail records plus pagination metadata.

    ``has_more`` is ``True`` when ``page * page_size < total_count`` so
    the frontend does not have to re-derive it.
    """

    items: List[UsageInsightsRecord]
    total_count: int
    page: int
    page_size: int
    has_more: bool


__all__ = [
    "GROUP_BY_VALUES",
    "GroupBy",
    "InsightsFilters",
    "UsageInsightsGroup",
    "UsageInsightsOverview",
    "UsageInsightsRecord",
    "UsageInsightsRecordsPage",
    "UsageInsightsTimelinePoint",
]
