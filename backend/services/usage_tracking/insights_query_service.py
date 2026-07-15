"""Read-only query service for per-task LLM usage insights.

Purpose:
    Owns the grouped, timeline, and paginated-record reads that power the
    Usage page. Every method is task-scoped, accepts the same
    ``InsightsFilters`` envelope, and returns a typed dataclass so the
    router layer (Task 2.2) can wrap results in Pydantic response models
    without re-implementing aggregation.

Responsibility:
    - ``get_overview`` — per-task rollup with derived cache-hit rate,
      cache ratio, cache-reporting coverage, and the cached vs uncached
      vs output cost split.
    - ``get_groups`` — bucket rows by a canonical metadata key (role,
      node_name, execution_branch, provider, api_surface) or by the
      ``model`` column, with an explicit ``"unknown"`` bucket for rows
      whose ``request_metadata`` is missing or partial.
    - ``get_timeline`` — chronological per-call points with
      cache-reporting-aware per-row ``cache_ratio``.
    - ``get_records`` — paginated rows with the full canonical metadata
      normalized to ``"unknown"`` defaults.

Boundaries:
    - **Reads only.** This service never calls
      ``UsageTrackingService.record_usage`` and never mutates any row.
      Write authority stays in ``UsageTrackingService``.
    - **Single pricing authority.** All cost math delegates to
      ``backend.services.usage_tracking.pricing`` —
      ``calculate_cost`` for per-row totals and
      ``calculate_cost_components`` for the cached / uncached / output
      split.
    - **Canonical metadata only for grouping.** The legacy ``source``
      string appears only on ``UsageInsightsRecord`` for debug
      visibility; it is never parsed to derive a role or branch.
    - **No new schema.** Rows are read from the existing
      ``LLMUsageRecord`` table; historical rows with
      ``request_metadata = NULL`` aggregate into the explicit
      ``"unknown"`` bucket rather than being silently excluded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.llm import LLMUsageRecord

from .insights_models import UNKNOWN
from .insights_response_models import (
    GROUP_BY_VALUES,
    GroupBy,
    InsightsFilters,
    UsageInsightsGroup,
    UsageInsightsOverview,
    UsageInsightsRecord,
    UsageInsightsRecordsPage,
    UsageInsightsTimelinePoint,
)
from .models import (
    CACHE_REPORTING_NOT_REPORTED,
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
)
from .pricing import (
    PRICING_UNAVAILABLE,
    aggregate_pricing_statuses,
    calculate_cost,
    calculate_cost_components,
    pricing_status_for_usage,
    usage_from_persisted_record,
)

logger = logging.getLogger(__name__)


# Canonical metadata keys read from ``LLMUsageRecord.request_metadata``.
# These are the only keys this service normalizes or buckets on; anything
# else in the JSON blob (legacy debug fields) is ignored.
_METADATA_STRING_KEYS: tuple[str, ...] = (
    "role",
    "node_name",
    "execution_branch",
    "provider",
    "api_surface",
    "request_mode",
    "cache_reporting",
)


def _normalize_metadata(
    raw: Any,
    *,
    fallback_provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a metadata dict with every canonical string key defaulted.

    ``raw`` is whatever ``LLMUsageRecord.request_metadata`` holds — ``None``
    for historical rows, a partial dict for rows written by handlers that
    predate the canonical contract, or the full
    ``UsageRecordMetadata`` shape for rows written after Phase 1.

    The returned dict always contains every key in
    ``_METADATA_STRING_KEYS`` with a non-empty string value (``"unknown"``
    when missing or falsy), plus a ``turn_index`` key that may be ``None``.
    Cache-reporting values outside the three known literals are coerced to
    ``"unknown"`` so the insights layer can trust the label.

    ``fallback_provider`` lets callers pass the row's ``LLMUsageRecord.provider``
    column value so historical rows (whose ``request_metadata`` is ``None``)
    still group under their real provider instead of collapsing to
    ``"unknown"``. Metadata-supplied providers always win.
    """
    result: Dict[str, Any] = {key: UNKNOWN for key in _METADATA_STRING_KEYS}
    result["turn_index"] = None

    if isinstance(raw, Mapping):
        for key in _METADATA_STRING_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value:
                result[key] = value

        # Cache-reporting must be one of the three known literals; anything
        # else (including ``None`` or a stray string) buckets as ``"unknown"``
        # so honest-reporting logic is never polluted by garbage input.
        if result["cache_reporting"] not in (
            CACHE_REPORTING_REPORTED,
            CACHE_REPORTING_NOT_REPORTED,
            CACHE_REPORTING_UNKNOWN,
        ):
            result["cache_reporting"] = CACHE_REPORTING_UNKNOWN

        turn_index = raw.get("turn_index")
        if isinstance(turn_index, int) and not isinstance(turn_index, bool):
            result["turn_index"] = turn_index

    if result["provider"] == UNKNOWN and isinstance(fallback_provider, str) and fallback_provider:
        result["provider"] = fallback_provider

    return result


def _row_matches_filters(
    row: LLMUsageRecord,
    metadata: Mapping[str, Any],
    filters: Optional[InsightsFilters],
) -> bool:
    """Return True when the row passes every non-None filter."""
    if filters is None:
        return True
    if filters.conversation_id is not None and row.conversation_id != filters.conversation_id:
        return False
    if filters.model is not None and row.model != filters.model:
        return False
    if filters.provider is not None and metadata["provider"] != filters.provider:
        return False
    if filters.role is not None and metadata["role"] != filters.role:
        return False
    if filters.execution_branch is not None and metadata["execution_branch"] != filters.execution_branch:
        return False
    if filters.api_surface is not None and metadata["api_surface"] != filters.api_surface:
        return False
    return True


def _row_cost_usd(row: LLMUsageRecord) -> float:
    """Return the per-row cost in USD by delegating to ``calculate_cost``.

    Wraps the row's token counts into a minimal ``UsageData`` so the
    single pricing authority owns the math. Any exception is swallowed
    and reported as ``0.0`` — insights reads must never crash a request
    on a noisy historical row.
    """
    try:
        return calculate_cost(usage_from_persisted_record(row))
    except Exception as exc:  # pragma: no cover - defensive log only
        logger.debug("cost calc failed for row id=%s: %s", getattr(row, "id", "?"), exc)
        return 0.0


class _AggregateBucket:
    """In-memory accumulator used for overview and group aggregation.

    Lives inside the service module because its shape mirrors
    ``UsageInsightsOverview`` / ``UsageInsightsGroup`` exactly and exists
    only as an intermediate for aggregation. Not part of the public API.
    """

    __slots__ = (
        "call_count",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cache_hit_calls",
        "cache_reporting_call_count",
        "reporting_prompt_tokens",
        "reporting_cached_tokens",
        "cost_usd",
        "cached_input_cost_usd",
        "uncached_input_cost_usd",
        "output_cost_usd",
        "pricing_statuses",
        "unpriced_providers_set",
        "unpriced_models_set",
    )

    def __init__(self) -> None:
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.cache_hit_calls = 0
        self.cache_reporting_call_count = 0
        self.reporting_prompt_tokens = 0
        self.reporting_cached_tokens = 0
        self.cost_usd = 0.0
        self.cached_input_cost_usd = 0.0
        self.uncached_input_cost_usd = 0.0
        self.output_cost_usd = 0.0
        self.pricing_statuses: list[str] = []
        self.unpriced_providers_set: set[str] = set()
        self.unpriced_models_set: set[str] = set()

    def add(self, row: LLMUsageRecord, metadata: Mapping[str, Any]) -> None:
        prompt = int(row.prompt_tokens or 0)
        completion = int(row.completion_tokens or 0)
        cached = int(row.cached_tokens or 0)

        self.call_count += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += cached
        provider = str(row.provider or "openai").strip().lower() or "openai"
        usage = usage_from_persisted_record(row)
        row_pricing_status = pricing_status_for_usage(usage)
        self.pricing_statuses.append(row_pricing_status)
        if row_pricing_status == PRICING_UNAVAILABLE:
            self.unpriced_providers_set.add(provider)
            self.unpriced_models_set.add(_provider_model_label(provider, row.model))

        reporting = metadata["cache_reporting"]
        if reporting == CACHE_REPORTING_REPORTED:
            self.cache_reporting_call_count += 1
            self.reporting_prompt_tokens += prompt
            self.reporting_cached_tokens += cached
            if cached > 0:
                self.cache_hit_calls += 1

        components = calculate_cost_components(
            row.model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            provider=provider,
            api_surface=metadata["api_surface"],
            provider_usage_components=usage.provider_usage_components,
            effective_date=usage.pricing_date,
        )
        self.cached_input_cost_usd += components["cached_input_cost_usd"]
        self.uncached_input_cost_usd += components["uncached_input_cost_usd"]
        self.output_cost_usd += components["output_cost_usd"]
        self.cost_usd += (
            components["cached_input_cost_usd"]
            + components["uncached_input_cost_usd"]
            + components["output_cost_usd"]
        )

    @property
    def pricing_status(self) -> str:
        return aggregate_pricing_statuses(self.pricing_statuses)

    @property
    def unpriced_providers(self) -> list[str]:
        return sorted(self.unpriced_providers_set)

    @property
    def unpriced_models(self) -> list[str]:
        return sorted(self.unpriced_models_set)

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cached_tokens)

    @property
    def cache_hit_rate(self) -> float:
        if self.cache_reporting_call_count <= 0:
            return 0.0
        return self.cache_hit_calls / self.cache_reporting_call_count

    @property
    def cache_ratio(self) -> float:
        # Restricted to reporting rows so non-reporting surfaces do not
        # drag the ratio down with honest-unknown zeros.
        if self.reporting_prompt_tokens <= 0:
            return 0.0
        return self.reporting_cached_tokens / self.reporting_prompt_tokens

    @property
    def cache_reporting_coverage(self) -> float:
        if self.call_count <= 0:
            return 0.0
        return self.cache_reporting_call_count / self.call_count


class UsageInsightsQueryService:
    """Read-only query surface for per-task LLM usage insights.

    Instantiated per-request with an already-open SQLAlchemy ``Session``.
    The service holds no state beyond the injected session: it does not
    cache results, does not mutate rows, and exposes no write methods.

    All four public methods share the same filtering envelope
    (``InsightsFilters``) so the UI can apply a single filter set across
    overview cards, the groups chart, the timeline, and the records
    table consistently.
    """

    #: Hard cap on rows materialized per call. Kept explicit so reviewers
    #: see the v1 limit; a follow-up can introduce proper pagination at
    #: the SQL layer once the v1 page ships. 10k rows per task is well
    #: above current per-task call counts.
    MAX_ROWS_PER_QUERY: int = 10_000

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_overview(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
        filters: Optional[InsightsFilters] = None,
    ) -> UsageInsightsOverview:
        """Return a single per-task rollup with derived cache/cost metrics."""
        rows = self._load_rows(task_id, tenant_id=tenant_id)
        bucket = _AggregateBucket()
        provider_coverage: Dict[str, int] = {}

        for row in rows:
            metadata = _normalize_metadata(
                row.request_metadata, fallback_provider=row.provider
            )
            if not _row_matches_filters(row, metadata, filters):
                continue
            bucket.add(row, metadata)
            provider = metadata["provider"]
            provider_coverage[provider] = provider_coverage.get(provider, 0) + 1

        return UsageInsightsOverview(
            task_id=task_id,
            call_count=bucket.call_count,
            prompt_tokens=bucket.prompt_tokens,
            completion_tokens=bucket.completion_tokens,
            cached_tokens=bucket.cached_tokens,
            uncached_prompt_tokens=bucket.uncached_prompt_tokens,
            cache_hit_calls=bucket.cache_hit_calls,
            cache_hit_rate=bucket.cache_hit_rate,
            cache_ratio=bucket.cache_ratio,
            cache_reporting_call_count=bucket.cache_reporting_call_count,
            cache_reporting_coverage=bucket.cache_reporting_coverage,
            cost_usd=bucket.cost_usd,
            cached_input_cost_usd=bucket.cached_input_cost_usd,
            uncached_input_cost_usd=bucket.uncached_input_cost_usd,
            output_cost_usd=bucket.output_cost_usd,
            pricing_status=bucket.pricing_status,
            unpriced_providers=bucket.unpriced_providers,
            unpriced_models=bucket.unpriced_models,
            provider_coverage=provider_coverage,
        )

    def get_groups(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
        group_by: GroupBy,
        filters: Optional[InsightsFilters] = None,
    ) -> List[UsageInsightsGroup]:
        """Return one row per distinct bucket of ``group_by``.

        Rows with missing metadata bucket into ``"unknown"`` rather than
        being silently excluded. The returned list is sorted by
        descending ``call_count`` so charts render the dominant bucket
        first; ties break lexicographically on the bucket name.
        """
        if group_by not in GROUP_BY_VALUES:
            raise ValueError(
                f"group_by must be one of {GROUP_BY_VALUES!r}, got {group_by!r}"
            )

        rows = self._load_rows(task_id, tenant_id=tenant_id)
        buckets: Dict[str, _AggregateBucket] = {}

        for row in rows:
            metadata = _normalize_metadata(
                row.request_metadata, fallback_provider=row.provider
            )
            if not _row_matches_filters(row, metadata, filters):
                continue
            bucket_key = self._bucket_key(row, metadata, group_by)
            bucket = buckets.setdefault(bucket_key, _AggregateBucket())
            bucket.add(row, metadata)

        groups = [
            UsageInsightsGroup(
                group_by=group_by,
                bucket=bucket_key,
                call_count=bucket.call_count,
                prompt_tokens=bucket.prompt_tokens,
                completion_tokens=bucket.completion_tokens,
                cached_tokens=bucket.cached_tokens,
                uncached_prompt_tokens=bucket.uncached_prompt_tokens,
                cache_hit_calls=bucket.cache_hit_calls,
                cache_hit_rate=bucket.cache_hit_rate,
                cache_ratio=bucket.cache_ratio,
                cache_reporting_call_count=bucket.cache_reporting_call_count,
                cache_reporting_coverage=bucket.cache_reporting_coverage,
                cost_usd=bucket.cost_usd,
                cached_input_cost_usd=bucket.cached_input_cost_usd,
                uncached_input_cost_usd=bucket.uncached_input_cost_usd,
                output_cost_usd=bucket.output_cost_usd,
                pricing_status=bucket.pricing_status,
            )
            for bucket_key, bucket in buckets.items()
        ]
        groups.sort(key=lambda g: (-g.call_count, g.bucket))
        return groups

    def get_timeline(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
        filters: Optional[InsightsFilters] = None,
    ) -> List[UsageInsightsTimelinePoint]:
        """Return chronological per-call timeline points (oldest first).

        Ordering is fully deterministic: rows sort on
        ``(created_at is None, created_at, row.id)`` so ties on
        ``created_at`` still produce a stable, write-order-preserving
        sequence. Running cumulative totals are computed server-side
        across this sorted sequence (``server-side-derived-metrics``)
        so the frontend can chart cumulative trends without any math of
        its own. Cumulative values are recomputed per call: each
        ``get_timeline`` invocation sums only its filtered set and does
        not carry running totals across from an earlier call.
        """
        rows = self._load_rows(task_id, tenant_id=tenant_id)
        matched: List[tuple[LLMUsageRecord, Dict[str, Any]]] = []

        for row in rows:
            metadata = _normalize_metadata(
                row.request_metadata, fallback_provider=row.provider
            )
            if not _row_matches_filters(row, metadata, filters):
                continue
            matched.append((row, metadata))

        # Deterministic chronological sort. ``None`` timestamps fall to
        # the start of the sequence; ``row.id`` breaks ties for rows
        # sharing an identical ``created_at`` (DB clock granularity or
        # bulk-write races) so repeated calls return identical ordering.
        matched.sort(key=lambda pair: _timeline_row_sort_key(pair[0]))

        points: List[UsageInsightsTimelinePoint] = []
        cumulative_prompt = 0
        cumulative_completion = 0
        cumulative_cached = 0
        cumulative_cost = 0.0

        for row, metadata in matched:
            prompt = int(row.prompt_tokens or 0)
            completion = int(row.completion_tokens or 0)
            cached = int(row.cached_tokens or 0)
            # Only reporting rows report an honest per-row cache ratio; a
            # ``not_reported`` / ``unknown`` row must not broadcast
            # ``cached / prompt`` because the numerator is forced to
            # ``0`` upstream.
            if (
                prompt > 0
                and metadata["cache_reporting"] == CACHE_REPORTING_REPORTED
            ):
                cache_ratio = cached / prompt
            else:
                cache_ratio = 0.0

            row_cost = _row_cost_usd(row)
            row_pricing_status = pricing_status_for_usage(
                usage_from_persisted_record(row)
            )
            cumulative_prompt += prompt
            cumulative_completion += completion
            cumulative_cached += cached
            cumulative_cost += row_cost

            points.append(
                UsageInsightsTimelinePoint(
                    created_at=row.created_at,
                    provider=metadata["provider"],
                    role=metadata["role"],
                    model=row.model,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    cached_tokens=cached,
                    cost_usd=row_cost,
                    pricing_status=row_pricing_status,
                    cache_ratio=cache_ratio,
                    cumulative_prompt_tokens=cumulative_prompt,
                    cumulative_completion_tokens=cumulative_completion,
                    cumulative_cached_tokens=cumulative_cached,
                    cumulative_cost_usd=cumulative_cost,
                )
            )

        return points

    def get_records(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
        filters: Optional[InsightsFilters] = None,
    ) -> UsageInsightsRecordsPage:
        """Return a paginated slice of detail records (newest first).

        ``page`` is 1-indexed. ``page_size`` is clamped into ``[1, 500]``
        so a malicious caller cannot materialize the entire table via
        the insights route.
        """
        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 500))

        rows = self._load_rows(task_id, tenant_id=tenant_id)
        matched: List[LLMUsageRecord] = []
        metadata_by_id: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            metadata = _normalize_metadata(
                row.request_metadata, fallback_provider=row.provider
            )
            if not _row_matches_filters(row, metadata, filters):
                continue
            matched.append(row)
            metadata_by_id[row.id] = metadata

        # Newest-first ordering for the detail table — matches the
        # existing ``get_task_usage_breakdown`` convention. Use a
        # ``None``-guarded key so malformed historical rows do not
        # crash the comparison when a mix of naive / aware timestamps
        # shows up.
        matched.sort(key=_records_sort_key, reverse=True)

        total_count = len(matched)
        start = (safe_page - 1) * safe_page_size
        end = start + safe_page_size
        page_rows = matched[start:end]
        items: List[UsageInsightsRecord] = []
        for row in page_rows:
            metadata = metadata_by_id[row.id]
            items.append(
                UsageInsightsRecord(
                    id=row.id,
                    created_at=row.created_at,
                    model=row.model,
                    source=row.source,
                    conversation_id=row.conversation_id,
                    prompt_tokens=int(row.prompt_tokens or 0),
                    completion_tokens=int(row.completion_tokens or 0),
                    total_tokens=int(row.total_tokens or 0),
                    cached_tokens=int(row.cached_tokens or 0),
                    reasoning_tokens=int(row.reasoning_tokens or 0),
                    cost_usd=_row_cost_usd(row),
                    pricing_status=pricing_status_for_usage(
                        usage_from_persisted_record(row)
                    ),
                    role=metadata["role"],
                    node_name=metadata["node_name"],
                    execution_branch=metadata["execution_branch"],
                    provider=metadata["provider"],
                    api_surface=metadata["api_surface"],
                    request_mode=metadata["request_mode"],
                    cache_reporting=metadata["cache_reporting"],
                    turn_index=metadata["turn_index"],
                )
            )
        has_more = end < total_count
        return UsageInsightsRecordsPage(
            items=items,
            total_count=total_count,
            page=safe_page,
            page_size=safe_page_size,
            has_more=has_more,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_rows(self, task_id: int, *, tenant_id: int | None = None) -> List[LLMUsageRecord]:
        """Load rows for a task, capped at ``MAX_ROWS_PER_QUERY``.

        Deliberately a full load (no SQL JSON filtering yet) — keeps v1
        simple. Metadata filtering and bucketing run in Python against
        the normalized dict so missing ``request_metadata`` cannot
        accidentally exclude historical rows.
        """
        try:
            stmt = (
                select(LLMUsageRecord)
                .where(
                    LLMUsageRecord.task_id == task_id,
                    self._tenant_filter(tenant_id),
                )
                .order_by(LLMUsageRecord.id.asc())
                .limit(self.MAX_ROWS_PER_QUERY)
            )
            return list(self._db.execute(stmt).scalars().all())
        except Exception as exc:
            logger.warning(
                "Failed to load LLMUsageRecord rows for task %s: %s",
                task_id,
                exc,
                exc_info=True,
            )
            return []

    @staticmethod
    def _tenant_filter(tenant_id: int | None):
        if tenant_id is None:
            return True
        return LLMUsageRecord.tenant_id == int(tenant_id)

    @staticmethod
    def _bucket_key(
        row: LLMUsageRecord,
        metadata: Mapping[str, Any],
        group_by: GroupBy,
    ) -> str:
        """Return the bucket value for one row under ``group_by``.

        The ``model`` dimension reads the row column directly; every
        other dimension reads the normalized metadata dict so historical
        rows end up in ``"unknown"`` instead of crashing.
        """
        if group_by == "model":
            model = row.model
            return model if isinstance(model, str) and model else UNKNOWN
        value = metadata.get(group_by, UNKNOWN)
        if isinstance(value, str) and value:
            return value
        return UNKNOWN


def _timeline_row_sort_key(row: "LLMUsageRecord") -> tuple:
    """Sort key for timeline row ordering that tolerates ``None`` timestamps.

    Mirrors ``_records_sort_key`` so timeline and records both break
    ``created_at`` ties on ``row.id``. This keeps the chronological
    timeline fully deterministic even when two rows share an identical
    ``created_at`` (e.g. DB timestamp granularity or bulk writes in the
    same transaction).
    """
    ts = row.created_at
    return (ts is None, ts, row.id)


def _records_sort_key(row: "LLMUsageRecord") -> tuple:
    """Sort key for detail records that tolerates ``None`` timestamps.

    Mirrors ``_timeline_sort_key`` but falls back to ``row.id`` so
    ``reverse=True`` still produces a deterministic newest-first order
    even for rows that share a timestamp.
    """
    ts = row.created_at
    return (ts is None, ts, row.id)


def _provider_model_label(provider: str, model: str) -> str:
    """Return a stable provider/model label for unpriced usage reporting."""

    provider_id = str(provider or "openai").strip().lower() or "openai"
    model_id = str(model or "unknown").strip() or "unknown"
    return f"{provider_id}/{model_id}"


__all__ = ["UsageInsightsQueryService"]
