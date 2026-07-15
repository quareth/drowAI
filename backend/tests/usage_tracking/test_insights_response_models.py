"""Tests for the Usage Insights Pydantic response schemas.

Purpose:
    Verify ``backend/schemas/usage_insights.py`` wraps the internal
    ``UsageInsightsQueryService`` dataclasses faithfully on the HTTP
    boundary — every server-derived metric flows through unchanged,
    ``datetime`` fields serialize to ISO strings (matching the legacy
    ``/usage/breakdown`` convention), the ``"unknown"`` bucket / metadata
    label is preserved verbatim, the ``group_by`` Literal rejects
    ``"source"`` and anything else outside the canonical set, the
    ``cache_reporting`` Literal rejects values outside the three honest
    states, and pagination fields round-trip untouched.

Boundaries:
    - Exercises only the schema module and its ``from_domain`` adapters
      (plus one end-to-end round-trip through
      ``UsageInsightsQueryService``). Does not hit any FastAPI router,
      authorization check, or HTTP client — router wiring belongs to
      Task 2.3.
    - Uses the backend-wide sqlite test DB via ``backend/tests/conftest``
      for the round-trip only; every pure-schema test builds dataclasses
      in memory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.llm import LLMUsageRecord
from backend.schemas.usage_insights import (
    UsageInsightsGroupRow,
    UsageInsightsGroupsResponse,
    UsageInsightsOverviewResponse,
    UsageInsightsRecord,
    UsageInsightsRecordsResponse,
    UsageInsightsTimelinePoint,
    UsageInsightsTimelineResponse,
)
from backend.services.usage_tracking.insights_models import UNKNOWN
from backend.services.usage_tracking.insights_query_service import (
    UsageInsightsQueryService,
)
from backend.services.usage_tracking.insights_response_models import (
    UsageInsightsGroup,
    UsageInsightsOverview,
    UsageInsightsRecord as DomainRecord,
    UsageInsightsRecordsPage,
    UsageInsightsTimelinePoint as DomainTimelinePoint,
)
from backend.services.usage_tracking.models import (
    CACHE_REPORTING_NOT_REPORTED,
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_overview(**overrides: object) -> UsageInsightsOverview:
    defaults: dict = dict(
        task_id=42,
        call_count=5,
        prompt_tokens=2800,
        completion_tokens=850,
        cached_tokens=400,
        uncached_prompt_tokens=2400,
        cache_hit_calls=1,
        cache_hit_rate=1 / 3,
        cache_ratio=400 / 1700,
        cache_reporting_call_count=3,
        cache_reporting_coverage=3 / 5,
        cost_usd=1.82,
        cached_input_cost_usd=0.22,
        uncached_input_cost_usd=0.81,
        output_cost_usd=0.79,
        pricing_status="available",
        unpriced_providers=[],
        unpriced_models=[],
        provider_coverage={"openai": 4, UNKNOWN: 1},
    )
    defaults.update(overrides)
    return UsageInsightsOverview(**defaults)


def _make_group(**overrides: object) -> UsageInsightsGroup:
    defaults: dict = dict(
        group_by="role",
        bucket="planner",
        call_count=2,
        prompt_tokens=300,
        completion_tokens=130,
        cached_tokens=100,
        uncached_prompt_tokens=200,
        cache_hit_calls=1,
        cache_hit_rate=0.5,
        cache_ratio=100 / 300,
        cache_reporting_call_count=2,
        cache_reporting_coverage=1.0,
        cost_usd=0.12,
        cached_input_cost_usd=0.02,
        uncached_input_cost_usd=0.05,
        output_cost_usd=0.05,
        pricing_status="available",
    )
    defaults.update(overrides)
    return UsageInsightsGroup(**defaults)


def _make_timeline_point(**overrides: object) -> DomainTimelinePoint:
    defaults: dict = dict(
        created_at=datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc),
        provider="openai",
        role="planner",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=40,
        cached_tokens=20,
        cost_usd=0.004,
        cache_ratio=0.2,
        cumulative_prompt_tokens=100,
        cumulative_completion_tokens=40,
        cumulative_cached_tokens=20,
        cumulative_cost_usd=0.004,
        pricing_status="available",
    )
    defaults.update(overrides)
    return DomainTimelinePoint(**defaults)


def _make_record(**overrides: object) -> DomainRecord:
    defaults: dict = dict(
        id=101,
        created_at=datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc),
        model="gpt-4o-mini",
        source="simple_chat",
        conversation_id="conv-abc",
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        cached_tokens=20,
        reasoning_tokens=0,
        cost_usd=0.004,
        pricing_status="available",
        role="planner",
        node_name="planner_node",
        execution_branch="main",
        provider="openai",
        api_surface="chat_completions",
        request_mode="non_streaming",
        cache_reporting=CACHE_REPORTING_REPORTED,
        turn_index=2,
    )
    defaults.update(overrides)
    return DomainRecord(**defaults)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestOverviewResponse:
    def test_from_domain_copies_every_field(self) -> None:
        overview = _make_overview()
        response = UsageInsightsOverviewResponse.from_domain(overview)

        dumped = response.model_dump()
        expected_keys = {
            "task_id",
            "provider_coverage",
            "call_count",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "uncached_prompt_tokens",
            "cache_hit_calls",
            "cache_hit_rate",
            "cache_ratio",
            "cache_reporting_call_count",
            "cache_reporting_coverage",
            "cost_usd",
            "cached_input_cost_usd",
            "uncached_input_cost_usd",
            "output_cost_usd",
            "pricing_status",
            "unpriced_providers",
            "unpriced_models",
        }
        assert set(dumped.keys()) == expected_keys

        # Values round-trip unchanged.
        assert dumped["task_id"] == 42
        assert dumped["provider_coverage"] == {"openai": 4, UNKNOWN: 1}
        assert dumped["call_count"] == 5
        assert dumped["cache_hit_calls"] == 1
        assert dumped["cache_hit_rate"] == pytest.approx(1 / 3)
        assert dumped["cache_ratio"] == pytest.approx(400 / 1700)
        assert dumped["cache_reporting_coverage"] == pytest.approx(3 / 5)
        assert dumped["uncached_prompt_tokens"] == 2400
        # Cost split fields are carried through verbatim (no frontend math).
        assert dumped["cached_input_cost_usd"] == pytest.approx(0.22)
        assert dumped["uncached_input_cost_usd"] == pytest.approx(0.81)
        assert dumped["output_cost_usd"] == pytest.approx(0.79)
        assert dumped["pricing_status"] == "available"
        assert dumped["unpriced_providers"] == []
        assert dumped["unpriced_models"] == []

    def test_overview_json_serializable(self) -> None:
        overview = _make_overview()
        response = UsageInsightsOverviewResponse.from_domain(overview)

        # model_dump_json must succeed (no datetime / bespoke types left).
        payload = json.loads(response.model_dump_json())
        assert payload["task_id"] == 42
        assert payload["provider_coverage"]["openai"] == 4
        assert payload["provider_coverage"][UNKNOWN] == 1

    def test_empty_overview_preserves_zeros(self) -> None:
        overview = _make_overview(
            call_count=0,
            prompt_tokens=0,
            completion_tokens=0,
            cached_tokens=0,
            uncached_prompt_tokens=0,
            cache_hit_calls=0,
            cache_hit_rate=0.0,
            cache_ratio=0.0,
            cache_reporting_call_count=0,
            cache_reporting_coverage=0.0,
            cost_usd=0.0,
            cached_input_cost_usd=0.0,
            uncached_input_cost_usd=0.0,
            output_cost_usd=0.0,
            provider_coverage={},
        )
        response = UsageInsightsOverviewResponse.from_domain(overview)
        dumped = response.model_dump()
        assert dumped["call_count"] == 0
        assert dumped["provider_coverage"] == {}

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UsageInsightsOverviewResponse(
                task_id=1,
                provider_coverage={},
                call_count=0,
                prompt_tokens=0,
                completion_tokens=0,
                cached_tokens=0,
                uncached_prompt_tokens=0,
                cache_hit_calls=0,
                cache_hit_rate=0.0,
                cache_ratio=0.0,
                cache_reporting_call_count=0,
                cache_reporting_coverage=0.0,
                cost_usd=0.0,
                cached_input_cost_usd=0.0,
                uncached_input_cost_usd=0.0,
                output_cost_usd=0.0,
                savings_usd=0.0,  # unknown field -> must be rejected
            )


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class TestGroupsResponse:
    def test_group_row_shape(self) -> None:
        row = UsageInsightsGroupRow.from_domain(_make_group())
        dumped = row.model_dump()
        assert set(dumped.keys()) == {
            "bucket_key",
            "call_count",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "uncached_prompt_tokens",
            "cache_hit_calls",
            "cache_hit_rate",
            "cache_ratio",
            "cache_reporting_call_count",
            "cache_reporting_coverage",
            "cost_usd",
            "cached_input_cost_usd",
            "uncached_input_cost_usd",
            "output_cost_usd",
            "pricing_status",
        }
        assert dumped["bucket_key"] == "planner"
        assert dumped["call_count"] == 2

    def test_groups_response_preserves_unknown_bucket_verbatim(self) -> None:
        rows: List[UsageInsightsGroup] = [
            _make_group(bucket="planner", call_count=2),
            _make_group(bucket=UNKNOWN, call_count=1),
        ]
        response = UsageInsightsGroupsResponse.from_domain(
            task_id=7,
            group_by="role",
            groups=rows,
        )
        payload = response.model_dump()
        assert payload["task_id"] == 7
        assert payload["group_by"] == "role"
        buckets = [row["bucket_key"] for row in payload["items"]]
        assert "planner" in buckets
        assert UNKNOWN in buckets

    def test_groups_response_rejects_source_group_by(self) -> None:
        # "source" is explicitly NOT a valid group_by (no-source-as-grouping-key).
        with pytest.raises(ValidationError):
            UsageInsightsGroupsResponse(
                task_id=1,
                group_by="source",  # type: ignore[arg-type]
                items=[],
            )

    def test_groups_response_rejects_arbitrary_group_by(self) -> None:
        with pytest.raises(ValidationError):
            UsageInsightsGroupsResponse(
                task_id=1,
                group_by="totally_made_up",  # type: ignore[arg-type]
                items=[],
            )

    @pytest.mark.parametrize(
        "group_by",
        ["role", "node_name", "execution_branch", "provider", "model", "api_surface"],
    )
    def test_groups_response_accepts_every_canonical_group_by(
        self, group_by: str
    ) -> None:
        response = UsageInsightsGroupsResponse(
            task_id=1,
            group_by=group_by,  # type: ignore[arg-type]
            items=[],
        )
        assert response.group_by == group_by

    def test_groups_response_json_serializable(self) -> None:
        response = UsageInsightsGroupsResponse.from_domain(
            task_id=7,
            group_by="role",
            groups=[_make_group(), _make_group(bucket=UNKNOWN, call_count=1)],
        )
        payload = json.loads(response.model_dump_json())
        assert payload["group_by"] == "role"
        assert len(payload["items"]) == 2


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


class TestTimelineResponse:
    def test_timeline_point_iso_serialization(self) -> None:
        point = _make_timeline_point()
        wire = UsageInsightsTimelinePoint.from_domain(point)
        # created_at must be an ISO string on the wire (no datetime leak).
        assert isinstance(wire.created_at, str)
        assert wire.created_at.startswith("2026-04-14T12:00:00")
        dumped = wire.model_dump()
        assert isinstance(dumped["created_at"], str)

    def test_timeline_response_shape(self) -> None:
        p1 = _make_timeline_point()
        p2 = _make_timeline_point(
            created_at=datetime(2026, 4, 14, 12, 5, 0, tzinfo=timezone.utc),
            role="simple_chat",
        )
        response = UsageInsightsTimelineResponse.from_domain(
            task_id=42, points=[p1, p2]
        )
        payload = response.model_dump()
        assert payload["task_id"] == 42
        assert [item["role"] for item in payload["items"]] == [
            "planner",
            "simple_chat",
        ]
        # End-to-end JSON serialization must not raise.
        assert json.loads(response.model_dump_json())["task_id"] == 42

    def test_timeline_naive_datetime_still_serializes(self) -> None:
        # Historical rows may carry naive datetimes — isoformat() still works.
        naive = datetime(2026, 4, 14, 12, 0, 0)
        point = _make_timeline_point(created_at=naive)
        wire = UsageInsightsTimelinePoint.from_domain(point)
        assert wire.created_at == naive.isoformat()

    def test_timeline_point_cumulative_fields_round_trip(self) -> None:
        """Task 2.4 — cumulative fields must be part of the wire contract.

        Adapter is a pure copy; every cumulative field on the internal
        dataclass lands on the Pydantic model with the same value and
        key. Extra fields are rejected by ``extra="forbid"``.
        """
        point = _make_timeline_point(
            prompt_tokens=200,
            completion_tokens=50,
            cached_tokens=40,
            cost_usd=0.01,
            cumulative_prompt_tokens=500,
            cumulative_completion_tokens=120,
            cumulative_cached_tokens=90,
            cumulative_cost_usd=0.025,
        )
        wire = UsageInsightsTimelinePoint.from_domain(point)
        dumped = wire.model_dump()

        # Field set includes every cumulative key.
        assert set(dumped.keys()) == {
            "created_at",
            "provider",
            "role",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "cost_usd",
            "pricing_status",
            "cache_ratio",
            "cumulative_prompt_tokens",
            "cumulative_completion_tokens",
            "cumulative_cached_tokens",
            "cumulative_cost_usd",
        }
        # Values copy 1:1 from the domain dataclass.
        assert dumped["cumulative_prompt_tokens"] == 500
        assert dumped["cumulative_completion_tokens"] == 120
        assert dumped["cumulative_cached_tokens"] == 90
        assert dumped["cumulative_cost_usd"] == pytest.approx(0.025)
        # JSON serialization includes the new keys.
        payload = json.loads(wire.model_dump_json())
        assert payload["cumulative_prompt_tokens"] == 500


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class TestRecordsResponse:
    def test_record_shape_and_iso_timestamp(self) -> None:
        record = UsageInsightsRecord.from_domain(_make_record())
        dumped = record.model_dump()
        assert set(dumped.keys()) == {
            "id",
            "created_at",
            "model",
            "source",
            "conversation_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_usd",
            "pricing_status",
            "role",
            "node_name",
            "execution_branch",
            "provider",
            "api_surface",
            "request_mode",
            "cache_reporting",
            "turn_index",
        }
        assert isinstance(dumped["created_at"], str)
        assert dumped["created_at"].startswith("2026-04-14T12:00:00")

    def test_record_preserves_unknown_metadata_verbatim(self) -> None:
        record = UsageInsightsRecord.from_domain(
            _make_record(
                role=UNKNOWN,
                node_name=UNKNOWN,
                execution_branch=UNKNOWN,
                provider=UNKNOWN,
                api_surface=UNKNOWN,
                request_mode=UNKNOWN,
                cache_reporting=CACHE_REPORTING_UNKNOWN,
                turn_index=None,
            )
        )
        dumped = record.model_dump()
        for field in (
            "role",
            "node_name",
            "execution_branch",
            "provider",
            "api_surface",
            "request_mode",
        ):
            assert dumped[field] == UNKNOWN
        assert dumped["cache_reporting"] == CACHE_REPORTING_UNKNOWN
        assert dumped["turn_index"] is None

    @pytest.mark.parametrize(
        "cache_reporting",
        [
            CACHE_REPORTING_REPORTED,
            CACHE_REPORTING_NOT_REPORTED,
            CACHE_REPORTING_UNKNOWN,
        ],
    )
    def test_cache_reporting_accepts_canonical_literals(
        self, cache_reporting: str
    ) -> None:
        record = UsageInsightsRecord.from_domain(
            _make_record(cache_reporting=cache_reporting)
        )
        assert record.cache_reporting == cache_reporting

    def test_cache_reporting_rejects_unknown_literal(self) -> None:
        with pytest.raises(ValidationError):
            UsageInsightsRecord(
                id=1,
                created_at="2026-04-14T12:00:00+00:00",
                model="gpt-4o-mini",
                source="simple_chat",
                conversation_id=None,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cached_tokens=0,
                reasoning_tokens=0,
                cost_usd=0.0,
                role=UNKNOWN,
                node_name=UNKNOWN,
                execution_branch=UNKNOWN,
                provider=UNKNOWN,
                api_surface=UNKNOWN,
                request_mode=UNKNOWN,
                cache_reporting="bogus",  # type: ignore[arg-type]
                turn_index=None,
            )

    def test_records_response_pagination_passthrough(self) -> None:
        page = UsageInsightsRecordsPage(
            items=[_make_record(), _make_record(id=102)],
            total_count=73,
            page=3,
            page_size=25,
            has_more=True,
        )
        response = UsageInsightsRecordsResponse.from_domain(
            task_id=42, page=page
        )
        dumped = response.model_dump()
        assert dumped["task_id"] == 42
        assert dumped["total_count"] == 73
        assert dumped["page"] == 3
        assert dumped["page_size"] == 25
        assert dumped["has_more"] is True
        assert len(dumped["items"]) == 2
        assert dumped["items"][0]["id"] == 101
        assert dumped["items"][1]["id"] == 102

    def test_records_response_json_serializable(self) -> None:
        page = UsageInsightsRecordsPage(
            items=[_make_record()],
            total_count=1,
            page=1,
            page_size=50,
            has_more=False,
        )
        response = UsageInsightsRecordsResponse.from_domain(
            task_id=42, page=page
        )
        payload = json.loads(response.model_dump_json())
        assert payload["items"][0]["cache_reporting"] == CACHE_REPORTING_REPORTED
        assert isinstance(payload["items"][0]["created_at"], str)


# ---------------------------------------------------------------------------
# Round-trip: service -> adapter -> Pydantic model -> JSON
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session() -> Session:
    """Fresh ``LLMUsageRecord``-scoped session for the round-trip test."""
    session = SessionLocal()
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
    task_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    request_metadata: dict | None,
) -> LLMUsageRecord:
    record = LLMUsageRecord(
        task_id=task_id,
        user_id=1,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=0,
        model="gpt-4o-mini",
        provider="openai",
        source="simple_chat",
        conversation_id=None,
        request_metadata=request_metadata,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


class TestRoundTrip:
    """Build rows, query via service, wrap via Pydantic, dump to JSON."""

    def test_overview_round_trip_matches_guide_example_keys(
        self, db_session: Session
    ) -> None:
        meta = {
            "role": "simple_chat",
            "node_name": "simple_chat",
            "execution_branch": "simple_chat",
            "provider": "openai",
            "api_surface": "chat_completions",
            "request_mode": "non_streaming",
            "cache_reporting": CACHE_REPORTING_REPORTED,
            "turn_index": None,
        }
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=1000,
            completion_tokens=200,
            cached_tokens=400,
            request_metadata=meta,
        )
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=500,
            completion_tokens=100,
            cached_tokens=0,
            request_metadata=meta,
        )
        # Historical row -> "unknown" bucket on everything.
        _insert_row(
            db_session,
            task_id=42,
            prompt_tokens=200,
            completion_tokens=50,
            cached_tokens=0,
            request_metadata=None,
        )

        service = UsageInsightsQueryService(db_session)
        overview = service.get_overview(task_id=42)
        response = UsageInsightsOverviewResponse.from_domain(overview)
        payload = json.loads(response.model_dump_json())

        # The payload key-set matches the guide's representative example.
        assert set(payload.keys()) == {
            "task_id",
            "provider_coverage",
            "call_count",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "uncached_prompt_tokens",
            "cache_hit_calls",
            "cache_hit_rate",
            "cache_ratio",
            "cache_reporting_call_count",
            "cache_reporting_coverage",
            "cost_usd",
            "cached_input_cost_usd",
            "uncached_input_cost_usd",
            "output_cost_usd",
            "pricing_status",
            "unpriced_providers",
            "unpriced_models",
        }
        assert payload["task_id"] == 42
        assert payload["call_count"] == 3
        # All three rows bucket under "openai": two from metadata and the
        # historical row (NULL metadata) from the ``LLMUsageRecord.provider``
        # column fallback.
        assert payload["provider_coverage"].get("openai") == 3
        assert UNKNOWN not in payload["provider_coverage"]

    def test_groups_round_trip_preserves_unknown_bucket(
        self, db_session: Session
    ) -> None:
        meta_planner = {
            "role": "planner",
            "node_name": "planner",
            "execution_branch": "main",
            "provider": "openai",
            "api_surface": "chat_completions",
            "request_mode": "non_streaming",
            "cache_reporting": CACHE_REPORTING_REPORTED,
            "turn_index": None,
        }
        _insert_row(
            db_session,
            task_id=11,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=0,
            request_metadata=meta_planner,
        )
        _insert_row(
            db_session,
            task_id=11,
            prompt_tokens=200,
            completion_tokens=80,
            cached_tokens=0,
            request_metadata=None,  # -> unknown bucket
        )

        service = UsageInsightsQueryService(db_session)
        groups = service.get_groups(task_id=11, group_by="role")
        response = UsageInsightsGroupsResponse.from_domain(
            task_id=11, group_by="role", groups=groups
        )
        payload = json.loads(response.model_dump_json())
        buckets = {item["bucket_key"] for item in payload["items"]}
        assert "planner" in buckets
        assert UNKNOWN in buckets
