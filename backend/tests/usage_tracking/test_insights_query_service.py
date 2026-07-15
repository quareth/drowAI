"""Tests for ``UsageInsightsQueryService``.

Purpose:
    Verify the read-only insights query layer computes server-derived
    metrics (cache-hit rate, cache ratio, uncached prompt tokens,
    per-row cached/uncached/output cost splits, cache-reporting
    coverage) correctly; that historical rows with missing
    ``request_metadata`` bucket into explicit ``"unknown"`` rather than
    being dropped; that grouping works off the canonical metadata keys
    and never parses the legacy ``source`` string; that pagination and
    filters apply consistently across overview / groups / timeline /
    records; and that the cost split for a known-pricing row reconciles
    with ``calculate_cost`` within float tolerance.

Boundaries:
    - Exercises only ``UsageInsightsQueryService`` and the pricing
      helpers it delegates to. Does not hit any router, Pydantic
      schema, or FastAPI app wiring — those belong to Tasks 2.2 / 2.3.
    - Uses the backend-wide sqlite test DB via ``backend/tests/conftest``
      to keep the fixture faithful to the ORM layer (JSON column
      round-trip, CREATE TABLE schema). No ad-hoc in-memory store.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

import pytest
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.llm import LLMUsageRecord
from backend.services.usage_tracking.insights_models import UNKNOWN
from backend.services.usage_tracking.insights_query_service import (
    UsageInsightsQueryService,
)
from backend.services.usage_tracking.insights_response_models import (
    InsightsFilters,
)
from backend.services.usage_tracking.models import (
    CACHE_REPORTING_NOT_REPORTED,
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
)
from backend.services.usage_tracking.pricing import (
    calculate_cost_components,
    get_model_pricing,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session() -> Session:
    """Provide a fresh SQLAlchemy session scoped to one test.

    Rows written by the test are cleaned up afterwards so subsequent
    tests see an isolated table state. The backend-wide session
    fixture in ``backend/tests/conftest.py`` creates the schema once
    per session; we reuse that DB here.
    """
    session = SessionLocal()
    # Track rows inserted by the test so we can clean up by PK.
    session.query(LLMUsageRecord).delete()
    session.commit()
    try:
        yield session
    finally:
        session.query(LLMUsageRecord).delete()
        session.commit()
        session.close()


def _insert_row(
    session: Session,
    *,
    task_id: int = 1,
    user_id: int = 1,
    model: str = "gpt-4o-mini",
    provider: str = "openai",
    source: str = "simple_chat",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    conversation_id: str | None = None,
    request_metadata: dict | None = None,
    created_at: datetime | None = None,
) -> LLMUsageRecord:
    """Insert one ``LLMUsageRecord`` and return it.

    ``created_at`` is optional because the column defaults to ``now()``
    server-side; tests that care about ordering pass explicit values.
    """
    record = LLMUsageRecord(
        task_id=task_id,
        user_id=user_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        model=model,
        provider=provider,
        source=source,
        conversation_id=conversation_id,
        request_metadata=request_metadata,
    )
    if created_at is not None:
        record.created_at = created_at
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def _meta(
    *,
    role: str = "simple_chat",
    node_name: str = "simple_chat",
    execution_branch: str = "simple_chat",
    provider: str = "openai",
    api_surface: str = "chat_completions",
    request_mode: str = "non_streaming",
    cache_reporting: str = CACHE_REPORTING_REPORTED,
    turn_index: int | None = None,
) -> dict:
    return {
        "role": role,
        "node_name": node_name,
        "execution_branch": execution_branch,
        "provider": provider,
        "api_surface": api_surface,
        "request_mode": request_mode,
        "cache_reporting": cache_reporting,
        "turn_index": turn_index,
    }


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestGetOverview:
    """``get_overview`` derives cache and cost metrics from raw rows."""

    def test_overview_totals_and_derived_metrics_mixed_reporting(
        self, db_session: Session
    ) -> None:
        # 3 reported rows (one cache hit, two cold), 1 not_reported row,
        # 1 row with NULL request_metadata (unknown reporting).
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=1000,
            completion_tokens=200,
            cached_tokens=400,  # reported + hit
            request_metadata=_meta(role="planner"),
        )
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=500,
            completion_tokens=100,
            cached_tokens=0,  # reported but cold
            request_metadata=_meta(role="simple_chat"),
        )
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=200,
            completion_tokens=50,
            cached_tokens=0,
            request_metadata=_meta(role="simple_chat"),
        )
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=800,
            completion_tokens=400,
            cached_tokens=0,  # surface doesn't report cache
            request_metadata=_meta(
                api_surface="responses",
                cache_reporting=CACHE_REPORTING_NOT_REPORTED,
                role="planner",
            ),
        )
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=300,
            completion_tokens=100,
            cached_tokens=0,
            request_metadata=None,  # historical -> unknown
        )

        service = UsageInsightsQueryService(db_session)
        overview = service.get_overview(task_id=42)

        # Totals sum every row.
        assert overview.task_id == 42
        assert overview.call_count == 5
        assert overview.prompt_tokens == 1000 + 500 + 200 + 800 + 300
        assert overview.completion_tokens == 200 + 100 + 50 + 400 + 100
        assert overview.cached_tokens == 400

        # uncached_prompt_tokens = prompt - cached, clamped.
        assert overview.uncached_prompt_tokens == overview.prompt_tokens - 400

        # cache_hit_calls = rows with cached>0 AND reporting==reported.
        assert overview.cache_hit_calls == 1
        # cache_reporting_call_count = only the three reported rows.
        assert overview.cache_reporting_call_count == 3
        # coverage = 3/5.
        assert overview.cache_reporting_coverage == pytest.approx(3 / 5)
        # hit_rate = 1/3.
        assert overview.cache_hit_rate == pytest.approx(1 / 3)
        # cache_ratio uses ONLY reporting rows: 400 / (1000+500+200).
        assert overview.cache_ratio == pytest.approx(400 / 1700)

        # Provider coverage falls back to the ``LLMUsageRecord.provider``
        # column when ``request_metadata`` is missing, so all five rows
        # bucket under ``openai`` even though one row's metadata is NULL.
        assert overview.provider_coverage["openai"] == 5
        assert UNKNOWN not in overview.provider_coverage

    def test_historical_rows_keep_provider_identity_from_column(
        self, db_session: Session
    ) -> None:
        """Regression: pre-metadata rows must surface under their real
        ``LLMUsageRecord.provider`` column value instead of collapsing to
        ``"unknown"``. Before the provider-fallback fix, historical rows
        (``request_metadata = None``) misclassified as ``unknown`` in
        provider_coverage, provider filters, and per-record reads.
        """
        # Two historical rows with real provider on the column, NULL metadata.
        _insert_row(
            db_session,
            task_id=77,
            provider="openai",
            request_metadata=None,
        )
        _insert_row(
            db_session,
            task_id=77,
            provider="anthropic",
            request_metadata=None,
        )

        service = UsageInsightsQueryService(db_session)

        # 1. Overview provider_coverage uses the column values.
        overview = service.get_overview(task_id=77)
        assert overview.provider_coverage == {"openai": 1, "anthropic": 1}
        assert UNKNOWN not in overview.provider_coverage

        # 2. Provider filter matches rows whose provider comes from the column.
        overview_filtered = service.get_overview(
            task_id=77, filters=InsightsFilters(provider="openai")
        )
        assert overview_filtered.call_count == 1
        assert overview_filtered.provider_coverage == {"openai": 1}

        # 3. Per-record reads surface the column provider on historical rows.
        records_page = service.get_records(task_id=77)
        assert {rec.provider for rec in records_page.items} == {"openai", "anthropic"}

        # 4. Metadata-supplied provider still wins over the column fallback.
        _insert_row(
            db_session,
            task_id=78,
            provider="openai",  # column says openai
            request_metadata=_meta(provider="anthropic"),  # metadata says anthropic
        )
        overview_78 = service.get_overview(task_id=78)
        assert overview_78.provider_coverage == {"anthropic": 1}

    def test_overview_empty_task_returns_zeros(self, db_session: Session) -> None:
        service = UsageInsightsQueryService(db_session)
        overview = service.get_overview(task_id=9999)

        assert overview.call_count == 0
        assert overview.prompt_tokens == 0
        assert overview.cache_hit_rate == 0.0
        assert overview.cache_ratio == 0.0
        assert overview.cache_reporting_coverage == 0.0
        assert overview.cost_usd == 0.0
        assert overview.cached_input_cost_usd == 0.0
        assert overview.provider_coverage == {}
        assert overview.pricing_status == "available"
        assert overview.unpriced_providers == []
        assert overview.unpriced_models == []

    def test_cost_split_reconciles_with_total(self, db_session: Session) -> None:
        """cached + uncached + output cost should equal cost_usd within tolerance."""
        _insert_row(
            db_session,
            task_id=7,
            model="gpt-4o",  # known pricing: input 2.50, cached 1.25, output 10.00 per M
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            cached_tokens=400_000,
            request_metadata=_meta(),
        )

        service = UsageInsightsQueryService(db_session)
        overview = service.get_overview(task_id=7)

        # Known values for this one row:
        # uncached = 600_000 @ $2.50/M = $1.50
        # cached   = 400_000 @ $1.25/M = $0.50
        # output   = 500_000 @ $10.00/M = $5.00
        # total    = $7.00
        assert overview.uncached_input_cost_usd == pytest.approx(1.50, rel=1e-6)
        assert overview.cached_input_cost_usd == pytest.approx(0.50, rel=1e-6)
        assert overview.output_cost_usd == pytest.approx(5.00, rel=1e-6)
        assert overview.cost_usd == pytest.approx(7.00, rel=1e-6)
        # The three split fields must sum to cost_usd.
        assert (
            overview.cached_input_cost_usd
            + overview.uncached_input_cost_usd
            + overview.output_cost_usd
        ) == pytest.approx(overview.cost_usd, rel=1e-9)

    def test_registered_anthropic_tokens_are_reported_and_priced(
        self, db_session: Session
    ) -> None:
        """Registered Anthropic usage totals use Anthropic pricing."""
        _insert_row(
            db_session,
            task_id=8,
            model="claude-sonnet-4-6",
            provider="anthropic",
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            request_metadata=_meta(
                provider="anthropic",
                api_surface="messages",
                cache_reporting=CACHE_REPORTING_UNKNOWN,
            ),
        )

        service = UsageInsightsQueryService(db_session)
        overview = service.get_overview(task_id=8)
        records = service.get_records(task_id=8)

        assert overview.prompt_tokens == 1_000_000
        assert overview.completion_tokens == 500_000
        assert overview.provider_coverage == {"anthropic": 1}
        assert overview.cost_usd == pytest.approx(10.5)
        assert overview.uncached_input_cost_usd == pytest.approx(3.0)
        assert overview.output_cost_usd == pytest.approx(7.5)
        assert overview.pricing_status == "available"
        assert overview.unpriced_providers == []
        assert overview.unpriced_models == []
        assert records.items[0].cost_usd == pytest.approx(10.5)
        assert records.items[0].pricing_status == "available"


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class TestGetGroups:
    """``get_groups`` buckets on canonical keys only; unknown is explicit."""

    def test_group_by_role_including_unknown_bucket(
        self, db_session: Session
    ) -> None:
        _insert_row(
            db_session,
            task_id=11,
            prompt_tokens=100,
            completion_tokens=50,
            request_metadata=_meta(role="planner"),
        )
        _insert_row(
            db_session,
            task_id=11,
            prompt_tokens=200,
            completion_tokens=80,
            request_metadata=_meta(role="planner"),
        )
        _insert_row(
            db_session,
            task_id=11,
            prompt_tokens=50,
            completion_tokens=20,
            request_metadata=_meta(role="simple_chat"),
        )
        # Historical row -> must bucket as "unknown".
        _insert_row(
            db_session,
            task_id=11,
            prompt_tokens=10,
            completion_tokens=5,
            request_metadata=None,
        )

        service = UsageInsightsQueryService(db_session)
        groups = service.get_groups(task_id=11, group_by="role")

        by_bucket = {g.bucket: g for g in groups}
        assert set(by_bucket) == {"planner", "simple_chat", UNKNOWN}
        assert by_bucket["planner"].call_count == 2
        assert by_bucket["planner"].prompt_tokens == 300
        assert by_bucket["simple_chat"].call_count == 1
        assert by_bucket[UNKNOWN].call_count == 1
        # group_by is echoed on every row for the frontend.
        assert all(g.group_by == "role" for g in groups)
        # Sort order: descending call_count, bucket name tiebreaker.
        assert groups[0].bucket == "planner"

    def test_group_by_provider_including_null_metadata(
        self, db_session: Session
    ) -> None:
        _insert_row(
            db_session, task_id=12, request_metadata=_meta(provider="openai")
        )
        _insert_row(
            db_session, task_id=12, request_metadata=_meta(provider="openai")
        )
        # No metadata, but ``LLMUsageRecord.provider`` column still holds
        # the real provider ("openai" default) so the row buckets under
        # its column value rather than collapsing to "unknown".
        _insert_row(db_session, task_id=12, provider="openai", request_metadata=None)
        # Column-only row with a different provider lands in its own bucket.
        _insert_row(db_session, task_id=12, provider="anthropic", request_metadata=None)

        service = UsageInsightsQueryService(db_session)
        groups = service.get_groups(task_id=12, group_by="provider")

        by_bucket = {g.bucket: g for g in groups}
        assert set(by_bucket) == {"openai", "anthropic"}
        assert by_bucket["openai"].call_count == 3
        assert by_bucket["anthropic"].call_count == 1
        assert UNKNOWN not in by_bucket

    def test_group_by_model_uses_row_column(self, db_session: Session) -> None:
        _insert_row(
            db_session,
            task_id=13,
            model="gpt-4o-mini",
            request_metadata=_meta(),
        )
        _insert_row(
            db_session,
            task_id=13,
            model="gpt-4o",
            request_metadata=_meta(),
        )
        _insert_row(
            db_session,
            task_id=13,
            model="gpt-4o",
            request_metadata=_meta(),
        )

        service = UsageInsightsQueryService(db_session)
        groups = service.get_groups(task_id=13, group_by="model")

        by_bucket = {g.bucket: g for g in groups}
        assert by_bucket["gpt-4o"].call_count == 2
        assert by_bucket["gpt-4o-mini"].call_count == 1

    def test_invalid_group_by_raises(self, db_session: Session) -> None:
        service = UsageInsightsQueryService(db_session)
        with pytest.raises(ValueError):
            service.get_groups(task_id=1, group_by="source")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class TestGetTimeline:
    """``get_timeline`` returns chronological points with stable fields."""

    def test_timeline_is_chronological_and_has_documented_fields(
        self, db_session: Session
    ) -> None:
        # The sqlite test DB strips tzinfo on round-trip, so the fixture
        # uses naive datetimes. Production Postgres keeps tz; either way
        # the per-task timestamps are homogeneous so ordering is stable.
        base = datetime(2026, 3, 1)
        _insert_row(
            db_session,
            task_id=20,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=20,
            request_metadata=_meta(role="planner"),
            created_at=base + timedelta(seconds=2),
        )
        _insert_row(
            db_session,
            task_id=20,
            prompt_tokens=200,
            completion_tokens=80,
            cached_tokens=0,
            request_metadata=_meta(
                role="planner",
                api_surface="responses",
                cache_reporting=CACHE_REPORTING_NOT_REPORTED,
            ),
            created_at=base + timedelta(seconds=1),
        )
        _insert_row(
            db_session,
            task_id=20,
            prompt_tokens=50,
            completion_tokens=10,
            request_metadata=None,
            created_at=base,
        )

        service = UsageInsightsQueryService(db_session)
        points = service.get_timeline(task_id=20)

        # Oldest first. Strip tzinfo when comparing so the test is robust
        # across sqlite (naive) and Postgres (aware) backends.
        actual = [
            (p.created_at.replace(tzinfo=None) if p.created_at else None)
            for p in points
        ]
        assert actual == [
            base,
            base + timedelta(seconds=1),
            base + timedelta(seconds=2),
        ]

        # Documented fields present and typed correctly.
        first = points[0]
        # Null metadata row still surfaces its real provider from the
        # ``LLMUsageRecord.provider`` column (defaulted to "openai").
        # Role has no column fallback, so it remains "unknown".
        assert first.provider == "openai"
        assert first.role == UNKNOWN
        assert first.model == "gpt-4o-mini"  # default
        assert first.cache_ratio == 0.0  # unknown reporting -> 0
        assert first.cost_usd >= 0.0

        # not_reported row must NOT broadcast cached/prompt as a ratio.
        not_reported = points[1]
        assert not_reported.cache_ratio == 0.0

        # Reported row exposes honest cache_ratio = cached/prompt.
        reported = points[2]
        assert reported.cache_ratio == pytest.approx(20 / 100)


class TestGetTimelineCumulativeAndOrdering:
    """Task 2.4 — cumulative totals + deterministic ordering with id tiebreaker.

    These exercise the v1 decisions pinned in Task 2.4:
      - cumulative totals are server-side running sums across the already
        chronologically sorted sequence.
      - no cumulative ratios are exposed (ratios re-derive per frame).
      - ``_timeline_row_sort_key`` falls back to ``row.id`` when two
        rows share an identical ``created_at`` — no hourly/daily
        bucketing crept in (points stay per-call).
      - cumulative values do NOT persist across ``get_timeline`` calls:
        each call is computed from scratch over its own filtered set.
      - ``cache_ratio`` gating on ``cache_reporting == "reported"`` is
        unchanged (regression guard for ``honest-cache-reporting``).
    """

    def test_cumulative_fields_are_running_sums(
        self, db_session: Session
    ) -> None:
        base = datetime(2026, 3, 10)
        _insert_row(
            db_session,
            task_id=50,
            prompt_tokens=100,
            completion_tokens=40,
            cached_tokens=10,
            request_metadata=_meta(role="planner"),
            created_at=base,
        )
        _insert_row(
            db_session,
            task_id=50,
            prompt_tokens=200,
            completion_tokens=60,
            cached_tokens=50,
            request_metadata=_meta(role="planner"),
            created_at=base + timedelta(seconds=1),
        )
        _insert_row(
            db_session,
            task_id=50,
            prompt_tokens=50,
            completion_tokens=20,
            cached_tokens=0,
            request_metadata=_meta(role="simple_chat"),
            created_at=base + timedelta(seconds=2),
        )

        service = UsageInsightsQueryService(db_session)
        points = service.get_timeline(task_id=50)

        assert len(points) == 3
        # Per-point cumulative prompt tokens match a manual prefix-sum.
        assert points[0].cumulative_prompt_tokens == 100
        assert points[1].cumulative_prompt_tokens == 300
        assert points[2].cumulative_prompt_tokens == 350

        # Completion + cached running sums too.
        assert points[0].cumulative_completion_tokens == 40
        assert points[1].cumulative_completion_tokens == 100
        assert points[2].cumulative_completion_tokens == 120
        assert points[0].cumulative_cached_tokens == 10
        assert points[1].cumulative_cached_tokens == 60
        assert points[2].cumulative_cached_tokens == 60

        # Cumulative cost is monotonically non-decreasing and the last
        # point equals the sum of per-row costs.
        assert points[0].cumulative_cost_usd == pytest.approx(points[0].cost_usd)
        assert points[1].cumulative_cost_usd == pytest.approx(
            points[0].cost_usd + points[1].cost_usd
        )
        assert points[2].cumulative_cost_usd == pytest.approx(
            points[0].cost_usd + points[1].cost_usd + points[2].cost_usd
        )

    def test_ordering_stable_for_identical_timestamps(
        self, db_session: Session
    ) -> None:
        """Two rows sharing a ``created_at`` must break ties on ``row.id``."""
        shared = datetime(2026, 3, 11, 12, 0, 0)
        first = _insert_row(
            db_session,
            task_id=51,
            prompt_tokens=10,
            completion_tokens=5,
            request_metadata=_meta(role="planner"),
            created_at=shared,
        )
        second = _insert_row(
            db_session,
            task_id=51,
            prompt_tokens=20,
            completion_tokens=7,
            request_metadata=_meta(role="simple_chat"),
            created_at=shared,
        )
        # The second insert has the larger id; write-order must win.
        assert second.id > first.id

        service = UsageInsightsQueryService(db_session)
        points_a = service.get_timeline(task_id=51)
        points_b = service.get_timeline(task_id=51)

        # Ordering must be deterministic AND reproducible across calls.
        roles_a = [p.role for p in points_a]
        roles_b = [p.role for p in points_b]
        assert roles_a == roles_b == ["planner", "simple_chat"]

        # Cumulative sums follow the deterministic order.
        assert points_a[0].cumulative_prompt_tokens == 10
        assert points_a[1].cumulative_prompt_tokens == 30

    def test_cache_ratio_gating_preserved_across_mixed_reporting(
        self, db_session: Session
    ) -> None:
        """Regression guard: ``cache_ratio`` stays honest per reporting label."""
        base = datetime(2026, 3, 12)
        _insert_row(
            db_session,
            task_id=52,
            prompt_tokens=100,
            completion_tokens=20,
            cached_tokens=40,  # reported + non-zero
            request_metadata=_meta(cache_reporting=CACHE_REPORTING_REPORTED),
            created_at=base,
        )
        _insert_row(
            db_session,
            task_id=52,
            prompt_tokens=200,
            completion_tokens=50,
            cached_tokens=0,  # surface that does not report cache
            request_metadata=_meta(cache_reporting=CACHE_REPORTING_NOT_REPORTED),
            created_at=base + timedelta(seconds=1),
        )
        _insert_row(
            db_session,
            task_id=52,
            prompt_tokens=50,
            completion_tokens=10,
            cached_tokens=0,
            request_metadata=None,  # historical -> unknown reporting
            created_at=base + timedelta(seconds=2),
        )

        service = UsageInsightsQueryService(db_session)
        points = service.get_timeline(task_id=52)

        # Reported row: honest cached/prompt ratio.
        assert points[0].cache_ratio == pytest.approx(40 / 100)
        # not_reported row: forced to 0 regardless of cached_tokens.
        assert points[1].cache_ratio == 0.0
        # unknown reporting row: forced to 0.
        assert points[2].cache_ratio == 0.0

        # Cumulative cached tokens still sum raw cached column values
        # (honest zeros for not_reported rows are still zeros).
        assert points[2].cumulative_cached_tokens == 40

    def test_cumulative_values_recompute_per_call_across_filter_changes(
        self, db_session: Session
    ) -> None:
        """Each ``get_timeline`` call recomputes cumulatives from its filtered set."""
        base = datetime(2026, 3, 13)
        _insert_row(
            db_session,
            task_id=53,
            prompt_tokens=100,
            completion_tokens=10,
            request_metadata=_meta(role="planner"),
            created_at=base,
        )
        _insert_row(
            db_session,
            task_id=53,
            prompt_tokens=200,
            completion_tokens=20,
            request_metadata=_meta(role="simple_chat"),
            created_at=base + timedelta(seconds=1),
        )
        _insert_row(
            db_session,
            task_id=53,
            prompt_tokens=400,
            completion_tokens=40,
            request_metadata=_meta(role="planner"),
            created_at=base + timedelta(seconds=2),
        )

        service = UsageInsightsQueryService(db_session)

        # Unfiltered: cumulative runs over all three rows.
        all_points = service.get_timeline(task_id=53)
        assert [p.cumulative_prompt_tokens for p in all_points] == [100, 300, 700]

        # Filtered to planner only: the two planner rows become the full
        # sequence. Cumulative sums must start over — NOT carry values
        # from the unfiltered call above.
        planner_points = service.get_timeline(
            task_id=53, filters=InsightsFilters(role="planner")
        )
        assert len(planner_points) == 2
        assert planner_points[0].cumulative_prompt_tokens == 100
        assert planner_points[1].cumulative_prompt_tokens == 500

        # And filtering to simple_chat: cumulative is just that one row.
        chat_points = service.get_timeline(
            task_id=53, filters=InsightsFilters(role="simple_chat")
        )
        assert len(chat_points) == 1
        assert chat_points[0].cumulative_prompt_tokens == 200


# ---------------------------------------------------------------------------
# Records (pagination + metadata normalization)
# ---------------------------------------------------------------------------


class TestGetRecords:
    """``get_records`` paginates newest-first and normalizes metadata."""

    def _seed_rows(self, db_session: Session, task_id: int, n: int) -> List[int]:
        # Naive datetimes; sqlite strips tzinfo either way. See
        # TestGetTimeline for context.
        base = datetime(2026, 3, 1)
        ids: List[int] = []
        for i in range(n):
            row = _insert_row(
                db_session,
                task_id=task_id,
                prompt_tokens=10 + i,
                completion_tokens=5,
                request_metadata=_meta(role="planner"),
                created_at=base + timedelta(seconds=i),
            )
            ids.append(row.id)
        return ids

    def test_pagination_page_one_and_two(self, db_session: Session) -> None:
        ids = self._seed_rows(db_session, task_id=30, n=5)

        service = UsageInsightsQueryService(db_session)

        page1 = service.get_records(task_id=30, page=1, page_size=3)
        assert page1.total_count == 5
        assert page1.page == 1
        assert page1.page_size == 3
        assert len(page1.items) == 3
        assert page1.has_more is True
        # Newest first: inserted last should be first.
        assert page1.items[0].id == ids[-1]

        page2 = service.get_records(task_id=30, page=2, page_size=3)
        assert page2.total_count == 5
        assert page2.page == 2
        assert len(page2.items) == 2
        assert page2.has_more is False

    def test_missing_metadata_defaults_to_unknown_on_record(
        self, db_session: Session
    ) -> None:
        _insert_row(
            db_session,
            task_id=31,
            provider="openai",
            request_metadata=None,  # historical row
        )

        service = UsageInsightsQueryService(db_session)
        page = service.get_records(task_id=31)
        assert len(page.items) == 1
        rec = page.items[0]
        # Canonical metadata fields without a column fallback stay "unknown".
        assert rec.role == UNKNOWN
        assert rec.node_name == UNKNOWN
        assert rec.execution_branch == UNKNOWN
        assert rec.api_surface == UNKNOWN
        assert rec.request_mode == UNKNOWN
        # Provider has a column-level fallback, so the historical row still
        # surfaces its real provider instead of collapsing to "unknown".
        assert rec.provider == "openai"
        # cache_reporting must be one of the three known literals; a
        # null metadata row buckets as "unknown".
        assert rec.cache_reporting == CACHE_REPORTING_UNKNOWN
        assert rec.turn_index is None
        # source stays available for debug visibility.
        assert rec.source == "simple_chat"

    def test_partial_metadata_fills_remaining_with_unknown(
        self, db_session: Session
    ) -> None:
        # Only role is set; every other canonical field must default.
        _insert_row(
            db_session,
            task_id=32,
            request_metadata={"role": "planner"},
        )
        service = UsageInsightsQueryService(db_session)
        page = service.get_records(task_id=32)
        rec = page.items[0]
        assert rec.role == "planner"
        assert rec.node_name == UNKNOWN
        assert rec.cache_reporting == CACHE_REPORTING_UNKNOWN

    def test_stray_cache_reporting_value_is_coerced_to_unknown(
        self, db_session: Session
    ) -> None:
        _insert_row(
            db_session,
            task_id=33,
            request_metadata=_meta(cache_reporting="partial_reported"),
        )
        service = UsageInsightsQueryService(db_session)
        page = service.get_records(task_id=33)
        assert page.items[0].cache_reporting == CACHE_REPORTING_UNKNOWN


# ---------------------------------------------------------------------------
# Filters apply uniformly across all four read methods
# ---------------------------------------------------------------------------


class TestFiltersAcrossMethods:
    """``InsightsFilters`` is honored by overview / groups / timeline / records."""

    def test_role_filter_narrows_all_four_methods(
        self, db_session: Session
    ) -> None:
        _insert_row(
            db_session,
            task_id=40,
            prompt_tokens=100,
            completion_tokens=10,
            request_metadata=_meta(role="planner"),
        )
        _insert_row(
            db_session,
            task_id=40,
            prompt_tokens=200,
            completion_tokens=20,
            request_metadata=_meta(role="planner"),
        )
        _insert_row(
            db_session,
            task_id=40,
            prompt_tokens=50,
            completion_tokens=5,
            request_metadata=_meta(role="simple_chat"),
        )
        # Historical row -> role == "unknown"; must be excluded by role="planner".
        _insert_row(db_session, task_id=40, request_metadata=None)

        service = UsageInsightsQueryService(db_session)
        filt = InsightsFilters(role="planner")

        overview = service.get_overview(task_id=40, filters=filt)
        assert overview.call_count == 2
        assert overview.prompt_tokens == 300

        groups = service.get_groups(task_id=40, group_by="role", filters=filt)
        assert len(groups) == 1
        assert groups[0].bucket == "planner"
        assert groups[0].call_count == 2

        timeline = service.get_timeline(task_id=40, filters=filt)
        assert len(timeline) == 2
        assert all(p.role == "planner" for p in timeline)

        records = service.get_records(task_id=40, filters=filt)
        assert records.total_count == 2
        assert all(r.role == "planner" for r in records.items)

    def test_provider_filter_and_model_filter_combine(
        self, db_session: Session
    ) -> None:
        _insert_row(
            db_session,
            task_id=41,
            model="gpt-4o",
            request_metadata=_meta(provider="openai", role="planner"),
        )
        _insert_row(
            db_session,
            task_id=41,
            model="gpt-4o-mini",
            request_metadata=_meta(provider="openai", role="planner"),
        )
        _insert_row(
            db_session,
            task_id=41,
            model="gpt-4o",
            request_metadata=_meta(provider="openai", role="simple_chat"),
        )

        service = UsageInsightsQueryService(db_session)
        filt = InsightsFilters(provider="openai", model="gpt-4o", role="planner")
        overview = service.get_overview(task_id=41, filters=filt)
        assert overview.call_count == 1
