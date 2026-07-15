"""Tests for usage API endpoints.

These tests verify that the usage endpoints return correct data
from the LLMUsageRecord table and do not use tiktoken estimation.
"""

import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.services.usage_tracking.models import (
    CACHE_REPORTING_NOT_REPORTED,
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
    UsageData,
    TaskUsageSummary,
)


class TestUsageEndpointReturnsActualData:
    """Tests verifying endpoints use actual usage data."""
    
    def test_get_task_usage_uses_service(self):
        """Endpoint should query UsageTrackingService, not tiktoken."""
        # This test verifies architectural correctness
        import inspect
        from backend.routers.usage import get_task_usage
        
        # Get source code of the endpoint
        source = inspect.getsource(get_task_usage)
        
        # Should NOT contain tiktoken library imports/usage
        assert 'import tiktoken' not in source
        assert 'tiktoken.' not in source
        assert 'encoding_for_model' not in source  # tiktoken.encoding_for_model
        
        # SHOULD use UsageTrackingService
        assert 'UsageTrackingService' in source
        assert 'get_task_usage' in source
    
    def test_get_task_usage_breakdown_uses_service(self):
        """Breakdown endpoint should use UsageTrackingService."""
        import inspect
        from backend.routers.usage import get_task_usage_breakdown
        
        source = inspect.getsource(get_task_usage_breakdown)
        
        assert 'import tiktoken' not in source
        assert 'tiktoken.' not in source
        assert 'UsageTrackingService' in source
        assert 'get_task_usage_breakdown' in source
    
    def test_legacy_endpoint_uses_service(self):
        """Legacy /usage/cost endpoint should use new service."""
        import inspect
        from backend.routers.usage import get_task_usage_cost
        
        source = inspect.getsource(get_task_usage_cost)
        
        assert 'import tiktoken' not in source
        assert 'tiktoken.' not in source
        assert 'UsageTrackingService' in source


class TestNoTiktokenInUsageEndpoint:
    """Tests ensuring no tiktoken usage in the usage module."""
    
    def test_no_tiktoken_import(self):
        """Usage router should not import tiktoken."""
        import backend.routers.usage as usage_module
        
        # Check module doesn't have tiktoken
        assert not hasattr(usage_module, 'tiktoken')
        
        # Check it's not in the module's imports
        import sys
        module_imports = [
            name for name in dir(usage_module) 
            if not name.startswith('_')
        ]
        assert 'tiktoken' not in module_imports
    
    def test_no_tiktoken_in_source(self):
        """Usage router source should not reference tiktoken."""
        import inspect
        import backend.routers.usage as usage_module
        
        source = inspect.getsource(usage_module)
        
        # Should not contain any tiktoken references
        assert 'import tiktoken' not in source
        assert 'from tiktoken' not in source
        assert 'tiktoken.' not in source


class TestPricingSingleSource:
    """Tests ensuring pricing comes from single source."""
    
    def test_pricing_from_pricing_module(self):
        """All pricing should come from pricing.py."""
        from backend.services.usage_tracking.pricing import OPENAI_PRICING, get_model_pricing
        
        # Verify pricing exists for common models
        assert 'gpt-4o' in OPENAI_PRICING
        assert 'gpt-4o-mini' in OPENAI_PRICING
        assert 'gpt-5' in OPENAI_PRICING
        
        # Verify pricing has required fields
        for model, pricing in OPENAI_PRICING.items():
            assert 'input_per_million' in pricing
            assert 'output_per_million' in pricing
            assert 'cached_input_per_million' in pricing
    
    def test_no_hardcoded_pricing_in_router(self):
        """Usage router should not have hardcoded pricing."""
        import inspect
        import backend.routers.usage as usage_module
        
        source = inspect.getsource(usage_module)
        
        # Should not contain hardcoded pricing patterns
        assert 'PRICING_PER_1K' not in source
        assert '0.002' not in source  # Old GPT-3.5 pricing
        assert '0.03' not in source   # Old GPT-4 pricing
        
        # Should import quote-level pricing from pricing module
        assert 'get_pricing_quote' in source
    
    def test_no_pricing_per_1k_in_codebase(self):
        """PRICING_PER_1K constant should not exist."""
        import os
        import re
        
        # Check key files
        files_to_check = [
            'backend/routers/usage.py',
            'backend/services/usage_tracking/service.py',
            'backend/services/usage_tracking/pricing.py',
        ]
        
        for filepath in files_to_check:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    content = f.read()
                    assert 'PRICING_PER_1K' not in content, f"Found PRICING_PER_1K in {filepath}"


class TestUsageResponseFormat:
    """Tests for response format correctness."""
    
    def test_token_usage_response_has_required_fields(self):
        """TokenUsageResponse should have all required fields."""
        from backend.routers.usage import TokenUsageResponse
        
        # Create a valid response
        response = TokenUsageResponse(
            task_id=123,
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            cached_tokens=100,
            reasoning_tokens=0,
            cost_usd=0.05,
            pricing_status="available",
            unpriced_providers=[],
            call_count=5,
            models=["gpt-4o-mini"],
        )
        
        # Verify all fields
        assert response.task_id == 123
        assert response.prompt_tokens == 1000
        assert response.completion_tokens == 500
        assert response.total_tokens == 1500
        assert response.cached_tokens == 100
        assert response.reasoning_tokens == 0
        assert response.unpriced_models == []
        assert response.cost_usd == 0.05
        assert response.call_count == 5
        assert response.models == ["gpt-4o-mini"]
    
    def test_usage_breakdown_item_has_required_fields(self):
        """UsageBreakdownItem should have all required fields."""
        from backend.routers.usage import UsageBreakdownItem
        
        item = UsageBreakdownItem(
            id=1,
            provider="openai",
            model="gpt-4o-mini",
            source="langgraph_normal",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cached_tokens=0,
            reasoning_tokens=0,
            cost_usd=0.001,
            pricing_status="available",
            created_at="2026-01-17T10:00:00Z",
        )
        
        assert item.id == 1
        assert item.model == "gpt-4o-mini"
        assert item.source == "langgraph_normal"


class TestAPIUsageTrackerScope:
    """Tests verifying APIUsageTracker documentation."""

    @pytest.mark.skip(reason="Obsolete APIUsageTracker lived in removed agent.completion_engine.")
    def test_api_usage_tracker_has_scope_documentation(self):
        """APIUsageTracker should have documentation about its scope."""
        from agent.completion_engine import APIUsageTracker
        
        docstring = APIUsageTracker.__doc__
        
        # Should document that it's for rate limiting, not billing
        assert 'rate limit' in docstring.lower() or 'session' in docstring.lower()
        assert 'billing' in docstring.lower() or 'cost' in docstring.lower()
        
        # Should mention it's different from UsageTrackingService
        assert 'UsageTrackingService' in docstring

    @pytest.mark.skip(reason="Obsolete APIUsageTracker lived in removed agent.completion_engine.")
    def test_api_usage_tracker_is_in_memory(self):
        """APIUsageTracker should be in-memory (not DB-backed)."""
        from agent.completion_engine import APIUsageTracker
        
        tracker = APIUsageTracker(max_calls=10, max_tokens=1000)
        
        # Should track in memory
        assert tracker.calls_made == 0
        assert tracker.tokens_consumed == 0
        
        # Track some calls
        tracker.track_call(request_tokens=100, response_tokens=50)
        tracker.track_call(request_tokens=200, response_tokens=100)
        
        assert tracker.calls_made == 2
        assert tracker.tokens_consumed == 450
        
        # Create new tracker - should start fresh (not persistent)
        tracker2 = APIUsageTracker(max_calls=10, max_tokens=1000)
        assert tracker2.calls_made == 0


class TestMigrationCompatibility:
    """Tests for migration compatibility."""

    def test_empty_task_returns_zeros(self):
        """Tasks with no usage records should return zero usage."""
        summary = TaskUsageSummary.empty(task_id=999)

        assert summary.total_prompt_tokens == 0
        assert summary.total_completion_tokens == 0
        assert summary.total_tokens == 0
        assert summary.total_cost_usd == 0.0
        assert summary.call_count == 0
        assert summary.models_used == []

    def test_usage_data_handles_missing_fields(self):
        """UsageData should handle responses with missing fields."""
        mock_response = MagicMock()
        mock_response.usage = None

        usage = UsageData.from_openai_chat_response(mock_response, "gpt-4o")

        assert usage.is_empty()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0


class TestLegacySummaryAndBreakdownShapeStability:
    """Structural smoke tests for the lightweight summary/breakdown contracts.

    These assert the field set and field types of the Pydantic response
    models that the existing UI (notably ``TaskModelSelectors``) depends on.
    They are intentionally structural — not aggregation/unit tests — so that
    future Phase 1.x / Phase 2.x work can add new optional fields additively
    but cannot silently rename, remove, or re-type an existing key without
    tripping these tests. See CLAUDE.md "additive-router-only" invariant and
    the Phase 1 Task 1.4 acceptance criteria.
    """

    # Keys consumed by client/src/components/chat/TaskModelSelectors.tsx via
    # client/src/types/usage.ts::TokenUsage. Renaming or removing any of
    # these without a paired frontend change breaks the compact summary.
    _SUMMARY_REQUIRED_KEYS: dict[str, type] = {
        "task_id": int,
        "prompt_tokens": int,
        "completion_tokens": int,
        "total_tokens": int,
        "cached_tokens": int,
        "reasoning_tokens": int,
        "cost_usd": float,
        "pricing_status": str,
        "unpriced_providers": list,
        "unpriced_models": list,
        "call_count": int,
        "models": list,
    }

    # Keys consumed by client/src/types/usage.ts::UsageBreakdownItem and
    # UsageBreakdownResponse. No frontend component currently calls this
    # endpoint, but the response shape is part of the public contract.
    _BREAKDOWN_ITEM_REQUIRED_KEYS: dict[str, type] = {
        "id": int,
        "provider": str,
        "model": str,
        "source": str,
        "prompt_tokens": int,
        "completion_tokens": int,
        "total_tokens": int,
        "cached_tokens": int,
        "reasoning_tokens": int,
        "cost_usd": float,
        "pricing_status": str,
        "created_at": str,
    }
    _BREAKDOWN_RESPONSE_REQUIRED_KEYS: dict[str, type] = {
        "task_id": int,
        "items": list,
        "total_count": int,
        "page": int,
        "page_size": int,
        "has_more": bool,
    }

    @staticmethod
    def _annotation_matches(annotation: object, expected: type) -> bool:
        """Return True if a Pydantic field annotation matches ``expected``.

        Handles plain types (``int``/``str``) as well as typing generics such
        as ``List[...]`` by comparing against ``typing.get_origin``.
        """
        import typing

        if annotation is expected:
            return True
        origin = typing.get_origin(annotation)
        return origin is expected

    def test_token_usage_response_required_field_shape(self):
        """TokenUsageResponse must expose the keys TaskModelSelectors reads."""
        from backend.routers.usage import TokenUsageResponse

        fields = TokenUsageResponse.model_fields
        for key, expected_type in self._SUMMARY_REQUIRED_KEYS.items():
            assert key in fields, f"TokenUsageResponse missing required key {key!r}"
            annotation = fields[key].annotation
            assert self._annotation_matches(annotation, expected_type), (
                f"TokenUsageResponse.{key} expected {expected_type.__name__}, "
                f"got {annotation!r}"
            )

        # Optional fields exposed today; assert presence so they are not
        # silently dropped. Their Optional[str] typing is enforced by the
        # round-trip test below.
        assert "first_call" in fields
        assert "last_call" in fields

    def test_token_usage_response_optional_timestamps_round_trip(self):
        """first_call / last_call must remain Optional[str]."""
        from backend.routers.usage import TokenUsageResponse

        # None-valued timestamps are a real production case (empty tasks).
        response = TokenUsageResponse(
            task_id=1,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cached_tokens=0,
            reasoning_tokens=0,
            cost_usd=0.0,
            call_count=0,
            models=[],
            first_call=None,
            last_call=None,
        )
        dumped = response.model_dump()
        assert dumped["first_call"] is None
        assert dumped["last_call"] is None

        # String timestamps must also round-trip.
        response2 = TokenUsageResponse(
            task_id=1,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cached_tokens=0,
            reasoning_tokens=0,
            cost_usd=0.0,
            call_count=0,
            models=[],
            first_call="2026-04-14T00:00:00",
            last_call="2026-04-14T01:00:00",
        )
        dumped2 = response2.model_dump()
        assert dumped2["first_call"] == "2026-04-14T00:00:00"
        assert dumped2["last_call"] == "2026-04-14T01:00:00"

    def test_usage_breakdown_item_required_field_shape(self):
        """UsageBreakdownItem must expose the documented per-call keys."""
        from backend.routers.usage import UsageBreakdownItem

        fields = UsageBreakdownItem.model_fields
        for key, expected_type in self._BREAKDOWN_ITEM_REQUIRED_KEYS.items():
            assert key in fields, f"UsageBreakdownItem missing required key {key!r}"
            annotation = fields[key].annotation
            assert self._annotation_matches(annotation, expected_type), (
                f"UsageBreakdownItem.{key} expected {expected_type.__name__}, "
                f"got {annotation!r}"
            )
        # conversation_id is Optional[str] — assert presence only.
        assert "conversation_id" in fields

    def test_usage_breakdown_response_required_field_shape(self):
        """UsageBreakdownResponse must expose the documented pagination keys."""
        from backend.routers.usage import UsageBreakdownResponse

        fields = UsageBreakdownResponse.model_fields
        for key, expected_type in self._BREAKDOWN_RESPONSE_REQUIRED_KEYS.items():
            assert key in fields, (
                f"UsageBreakdownResponse missing required key {key!r}"
            )
            annotation = fields[key].annotation
            assert self._annotation_matches(annotation, expected_type), (
                f"UsageBreakdownResponse.{key} expected {expected_type.__name__}, "
                f"got {annotation!r}"
            )

    def test_record_usage_without_metadata_still_writes_row(self):
        """Legacy callers (no usage_metadata kwarg) must keep working.

        This guards the "single-write-authority" path: Task 1.2 added
        the optional ``usage_metadata`` kwarg to
        ``UsageTrackingService.record_usage``; callers that have not been
        migrated must still persist a row identically via the legacy
        ``metadata`` dict (or no metadata at all). Regression test — no new
        behavior asserted, just that the old call shape remains supported.
        """
        from backend.services.usage_tracking.service import UsageTrackingService

        # MagicMock stands in for the Session; we care only that record_usage
        # builds + stages an LLMUsageRecord when no usage_metadata is passed.
        mock_session = MagicMock()

        service = UsageTrackingService(mock_session)
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )

        record = service.record_usage(
            task_id=42,
            user_id=7,
            usage=usage,
            source="langgraph_normal",
            # No usage_metadata kwarg — legacy call path.
        )

        assert record is not None
        assert record.task_id == 42
        assert record.user_id == 7
        assert record.prompt_tokens == 100
        assert record.completion_tokens == 50
        # No metadata passed, no usage_metadata passed → request_metadata is None.
        assert record.request_metadata is None
        assert mock_session.add.called
        assert mock_session.commit.called


# ---------------------------------------------------------------------------
# Phase 2 / Task 2.3 — Usage Insights router endpoints
# ---------------------------------------------------------------------------


def _canonical_metadata(
    *,
    role: str = "simple_chat",
    node_name: str = "simple_chat",
    execution_branch: str = "simple_chat",
    provider: str = "openai",
    api_surface: str = "chat_completions",
    request_mode: str = "non_streaming",
    cache_reporting: str = CACHE_REPORTING_REPORTED,
    turn_index=None,
) -> dict:
    """Return a fully populated canonical metadata dict for seeding rows."""
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


def _build_insights_client():
    """Create a FastAPI TestClient with a seeded user + task and isolated DB.

    Pattern mirrors ``backend/tests/routers/test_chat_cancel_endpoint.py``:
    build an in-memory sqlite engine, install the production usage router,
    and override ``get_db`` / ``get_current_user`` with test-local shims so
    the insights endpoints run against a known-owned task.

    Returns a dict with ``client``, ``task_id``, ``user_id``, ``other_task_id``
    (owned by a different user, used for cross-tenant ownership checks), and
    ``SessionLocal`` for direct row seeding.
    """
    from backend.models.core import Task, User
    from backend.models.llm import LLMUsageRecord  # noqa: F401 — registers table
    from backend.routers import usage as usage_routes

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with SessionLocal() as db:
        owner = User(username="insights-owner", password="secret")
        other = User(username="insights-other", password="secret")
        db.add_all([owner, other])
        db.flush()
        owned_task = Task(user_id=owner.id, tenant_id=701, name="insights-task", status="running")
        other_task = Task(user_id=other.id, tenant_id=702, name="other-task", status="running")
        db.add_all([owned_task, other_task])
        db.commit()
        ids = {
            "user_id": owner.id,
            "task_id": owned_task.id,
            "other_user_id": other.id,
            "other_task_id": other_task.id,
        }

    app = FastAPI()
    app.include_router(usage_routes.router)

    def _fake_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _fake_get_current_user():
        return SimpleNamespace(
            id=ids["user_id"],
            username="insights-owner",
            is_active=True,
        )

    def _fake_get_tenant_context():
        return SimpleNamespace(
            tenant_id=701,
            user_id=ids["user_id"],
            role="owner",
        )

    app.dependency_overrides[usage_routes.get_db] = _fake_get_db
    app.dependency_overrides[usage_routes.get_current_user] = _fake_get_current_user
    app.dependency_overrides[usage_routes.get_tenant_request_context] = _fake_get_tenant_context
    client = TestClient(app)
    return {
        "app": app,
        "client": client,
        "SessionLocal": SessionLocal,
        **ids,
    }


def _seed_row(
    SessionLocal,
    *,
    task_id: int,
    user_id: int,
    tenant_id: int = 701,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int = 0,
    model: str = "gpt-4o-mini",
    provider: str = "openai",
    source: str = "simple_chat",
    conversation_id=None,
    request_metadata: dict | None = None,
    created_at: datetime | None = None,
) -> int:
    """Insert one ``LLMUsageRecord`` and return its primary key."""
    from backend.models.llm import LLMUsageRecord

    with SessionLocal() as db:
        row = LLMUsageRecord(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=0,
            model=model,
            provider=provider,
            source=source,
            conversation_id=conversation_id,
            request_metadata=request_metadata,
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


class TestLegacyUsageProviderPricing:
    """Regression tests for provider-aware legacy usage pricing paths."""

    def test_breakdown_uses_record_provider_for_cost_status(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            request_metadata=_canonical_metadata(
                provider="anthropic",
                api_surface="messages",
            ),
        )

        resp = env["client"].get(f"/api/tasks/{task_id}/usage/breakdown")

        assert resp.status_code == 200, resp.text
        item = resp.json()["items"][0]
        assert item["provider"] == "anthropic"
        assert item["model"] == "claude-sonnet-4-6"
        assert item["cost_usd"] == pytest.approx(18.0)
        assert item["pricing_status"] == "available"

    def test_legacy_cost_endpoint_prices_registered_anthropic_provider(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            request_metadata=_canonical_metadata(
                provider="anthropic",
                api_surface="messages",
            ),
        )

        resp = env["client"].get(f"/api/tasks/{task_id}/usage/cost")

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["provider"] == "anthropic"
        assert payload["model"] == "claude-sonnet-4-6"
        assert payload["pricing_status"] == "available"
        assert payload["cost_usd"] == pytest.approx(0.00105)

    def test_legacy_cost_endpoint_uses_aggregate_pricing_status(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            provider="openai",
            model="gpt-4o-mini",
        )
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            provider="openai",
            model="future-openai-model",
        )

        resp = env["client"].get(f"/api/tasks/{task_id}/usage/cost")

        assert resp.status_code == 200, resp.text
        assert resp.json()["pricing_status"] == "estimated"


class TestUsageInsightsEndpoints:
    """Router wiring for the four ``/usage/insights/*`` endpoints."""

    # ---- Overview ---------------------------------------------------------

    def test_overview_returns_200_with_derived_metrics(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=1000,
            completion_tokens=200,
            cached_tokens=400,
            request_metadata=_canonical_metadata(role="planner"),
        )
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=500,
            completion_tokens=100,
            cached_tokens=0,
            request_metadata=_canonical_metadata(role="simple_chat"),
        )
        # NULL request_metadata (historical) — provider still surfaces
        # from the ``LLMUsageRecord.provider`` column (defaulted to
        # "openai"), so it buckets under the real provider rather than
        # collapsing to "unknown".
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=200,
            completion_tokens=50,
            cached_tokens=0,
            request_metadata=None,
        )

        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/overview"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Smoke field set matches UsageInsightsOverviewResponse.
        assert body["task_id"] == task_id
        assert body["call_count"] == 3
        assert body["prompt_tokens"] == 1700
        assert body["completion_tokens"] == 350
        assert body["cached_tokens"] == 400
        assert body["uncached_prompt_tokens"] == 1300
        assert "cache_hit_calls" in body
        assert "cache_hit_rate" in body
        assert "cache_ratio" in body
        assert "cache_reporting_call_count" in body
        assert "cache_reporting_coverage" in body
        assert "cost_usd" in body
        assert "cached_input_cost_usd" in body
        assert "uncached_input_cost_usd" in body
        assert "output_cost_usd" in body
        assert "pricing_status" in body
        assert "unpriced_providers" in body
        assert "unpriced_models" in body

        # provider_coverage dict present; historical row's provider
        # comes from the column so all three rows bucket under "openai".
        # (Canonical fields without a column fallback — role, node_name,
        # execution_branch, api_surface, request_mode — still land as
        # "unknown" for historical rows; that's covered by the records
        # test below.)
        assert isinstance(body["provider_coverage"], dict)
        assert body["provider_coverage"].get("openai") == 3
        assert "unknown" not in body["provider_coverage"]

    def test_overview_filter_role_narrows_results(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=1000,
            completion_tokens=200,
            request_metadata=_canonical_metadata(role="planner"),
        )
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=500,
            completion_tokens=100,
            request_metadata=_canonical_metadata(role="simple_chat"),
        )

        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/overview",
            params={"role": "planner"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Parity check against what the service returns directly for this
        # filter set — router is a thin pass-through.
        from backend.services.usage_tracking.insights_query_service import (
            UsageInsightsQueryService,
        )
        from backend.services.usage_tracking.insights_response_models import (
            InsightsFilters,
        )

        with env["SessionLocal"]() as db:
            expected = UsageInsightsQueryService(db).get_overview(
                task_id=task_id,
                filters=InsightsFilters(role="planner"),
            )
        assert body["call_count"] == expected.call_count == 1
        assert body["prompt_tokens"] == expected.prompt_tokens == 1000
        assert body["completion_tokens"] == expected.completion_tokens == 200

    # ---- Groups -----------------------------------------------------------

    def test_groups_by_role_preserves_unknown_bucket(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            request_metadata=_canonical_metadata(role="planner"),
        )
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            request_metadata=None,  # -> unknown bucket
        )

        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/groups",
            params={"group_by": "role"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == task_id
        assert body["group_by"] == "role"
        buckets = {row["bucket_key"] for row in body["items"]}
        assert buckets == {"planner", "unknown"}

    def test_groups_rejects_source_as_group_by(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/groups",
            params={"group_by": "source"},
        )
        # FastAPI rejects the Literal mismatch with 422 — no-source-as-grouping-key.
        assert resp.status_code == 422, resp.text

    def test_groups_missing_group_by_is_422(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/groups"
        )
        assert resp.status_code == 422, resp.text

    # ---- Timeline ---------------------------------------------------------

    def test_timeline_is_chronologically_ordered(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        base = datetime(2026, 4, 14, 10, 0, 0)
        # Insert out-of-order so the service's sort is observable.
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=300,
            completion_tokens=50,
            request_metadata=_canonical_metadata(role="a"),
            created_at=base + timedelta(minutes=2),
        )
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=100,
            completion_tokens=20,
            request_metadata=_canonical_metadata(role="b"),
            created_at=base,
        )
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            prompt_tokens=200,
            completion_tokens=30,
            request_metadata=_canonical_metadata(role="c"),
            created_at=base + timedelta(minutes=1),
        )

        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/timeline"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == task_id
        timestamps = [item["created_at"] for item in body["items"]]
        assert timestamps == sorted(timestamps), (
            f"timeline not chronologically ordered: {timestamps}"
        )
        # Roles mirror the chronological ordering we inserted.
        assert [item["role"] for item in body["items"]] == ["b", "c", "a"]

        # Task 2.4 — cumulative fields are present on every point and
        # sum monotonically across the chronological sequence.
        cumulative_fields = (
            "cumulative_prompt_tokens",
            "cumulative_completion_tokens",
            "cumulative_cached_tokens",
            "cumulative_cost_usd",
        )
        for item in body["items"]:
            for key in cumulative_fields:
                assert key in item, f"timeline point missing {key}"
        # Running prompt-token sum follows the inserted order: 100, 300, 600.
        assert [item["cumulative_prompt_tokens"] for item in body["items"]] == [
            100,
            300,
            600,
        ]

    # ---- Records ----------------------------------------------------------

    def test_records_pagination_has_more_and_page_size(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        base = datetime(2026, 4, 14, 10, 0, 0)
        for idx in range(5):
            _seed_row(
                env["SessionLocal"],
                task_id=task_id,
                user_id=user_id,
                prompt_tokens=10 * (idx + 1),
                completion_tokens=5,
                request_metadata=_canonical_metadata(role=f"role-{idx}"),
                created_at=base + timedelta(minutes=idx),
            )

        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/records",
            params={"page": 1, "page_size": 2},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == task_id
        assert body["total_count"] == 5
        assert body["page"] == 1
        assert body["page_size"] == 2
        assert body["has_more"] is True
        assert len(body["items"]) == 2

        # Page 3 (last page): 1 item, has_more=False.
        resp3 = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/records",
            params={"page": 3, "page_size": 2},
        )
        assert resp3.status_code == 200, resp3.text
        body3 = resp3.json()
        assert body3["total_count"] == 5
        assert body3["page"] == 3
        assert body3["page_size"] == 2
        assert body3["has_more"] is False
        assert len(body3["items"]) == 1

    def test_records_page_size_above_bound_is_422(self):
        env = _build_insights_client()
        task_id = env["task_id"]
        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/records",
            params={"page_size": 500},
        )
        assert resp.status_code == 422, resp.text

    def test_records_includes_canonical_metadata_and_unknown_defaults(self):
        """Rows with missing request_metadata carry "unknown" defaults.

        Provider is the exception: it has a column-level fallback
        (``LLMUsageRecord.provider``) so historical rows surface under
        their real provider rather than collapsing to "unknown".
        """
        env = _build_insights_client()
        task_id = env["task_id"]
        user_id = env["user_id"]
        _seed_row(
            env["SessionLocal"],
            task_id=task_id,
            user_id=user_id,
            provider="openai",
            request_metadata=None,
        )
        resp = env["client"].get(
            f"/api/tasks/{task_id}/usage/insights/records"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_count"] == 1
        item = body["items"][0]
        # Canonical fields without a column fallback stay "unknown".
        for key in (
            "role",
            "node_name",
            "execution_branch",
            "api_surface",
            "request_mode",
        ):
            assert item[key] == "unknown"
        # Provider falls back to the column value.
        assert item["provider"] == "openai"
        assert item["cache_reporting"] == CACHE_REPORTING_UNKNOWN

    # ---- Authorization / task ownership parity ---------------------------

    def test_overview_cross_tenant_task_returns_404_parity_with_legacy_usage(self):
        """Requesting someone else's task 404s — same shape as /usage."""
        env = _build_insights_client()
        other_task_id = env["other_task_id"]

        legacy_resp = env["client"].get(f"/api/tasks/{other_task_id}/usage")
        assert legacy_resp.status_code == 404, legacy_resp.text

        for suffix in ("overview", "timeline", "records"):
            resp = env["client"].get(
                f"/api/tasks/{other_task_id}/usage/insights/{suffix}"
            )
            assert resp.status_code == legacy_resp.status_code, (
                f"{suffix} auth parity mismatch: {resp.status_code}"
            )
            # Both return the same detail string ("Task not found").
            assert resp.json() == legacy_resp.json()

        # Groups requires group_by; ownership check runs only after the
        # query-param validation, so the authoritative cross-tenant check
        # passes group_by explicitly.
        groups_resp = env["client"].get(
            f"/api/tasks/{other_task_id}/usage/insights/groups",
            params={"group_by": "role"},
        )
        assert groups_resp.status_code == legacy_resp.status_code
        assert groups_resp.json() == legacy_resp.json()

    def test_unauthenticated_caller_rejected(self):
        """With no current_user override, auth fails the same way /usage does."""
        from backend.models.core import Task, User
        from backend.routers import usage as usage_routes

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with SessionLocal() as db:
            owner = User(username="noauth-owner", password="secret")
            db.add(owner)
            db.flush()
            task = Task(user_id=owner.id, name="noauth-task", status="running")
            db.add(task)
            db.commit()
            task_id = task.id

        app = FastAPI()
        app.include_router(usage_routes.router)

        def _fake_get_db():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        # Intentionally do NOT override get_current_user — it requires a JWT.
        app.dependency_overrides[usage_routes.get_db] = _fake_get_db
        client = TestClient(app)

        legacy_resp = client.get(f"/api/tasks/{task_id}/usage")
        overview_resp = client.get(
            f"/api/tasks/{task_id}/usage/insights/overview"
        )
        # Parity: whatever status the real JWT dependency returns on no
        # credentials (401/403), both endpoints must agree.
        assert overview_resp.status_code == legacy_resp.status_code
        assert overview_resp.status_code in (401, 403)
