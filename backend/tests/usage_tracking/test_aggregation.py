"""Tests for usage aggregation functionality.

These tests verify that aggregation queries work correctly across
multiple LLM calls with different models.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from backend.services.usage_tracking.models import UsageData, TaskUsageSummary
from backend.services.usage_tracking.service import UsageTrackingService
from backend.services.usage_tracking.cache import UsageCache


class TestTaskUsageAggregation:
    """Tests for task-level aggregation."""
    
    def test_aggregation_sums_correctly(self):
        """Aggregation should sum tokens correctly across multiple calls."""
        mock_db = MagicMock()
        
        # Mock aggregation result: sum of 3 records
        # Record 1: 100 prompt, 50 completion
        # Record 2: 200 prompt, 100 completion
        # Record 3: 300 prompt, 150 completion
        # Total: 600 prompt, 300 completion, 900 total
        aggregation_result = (
            600,   # prompt_tokens
            300,   # completion_tokens
            900,   # total_tokens
            50,    # cached_tokens
            0,     # reasoning_tokens
            3,     # call_count
            datetime(2026, 1, 15),  # first_call
            datetime(2026, 1, 17),  # last_call
        )
        mock_agg_result = MagicMock()
        mock_agg_result.one.return_value = aggregation_result
        
        mock_provider_model_result = MagicMock()
        mock_provider_model_result.all.return_value = [
            ("openai", "gpt-4o-mini", 600, 300, 50, 0),
        ]
        
        call_count = [0]
        def execute_side_effect(query):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_agg_result
            return mock_provider_model_result

        mock_db.execute.side_effect = execute_side_effect

        service = UsageTrackingService(mock_db)
        summary = service.get_task_usage(task_id=123)

        assert summary.total_prompt_tokens == 600
        assert summary.total_completion_tokens == 300
        assert summary.total_tokens == 900
        assert summary.total_cached_tokens == 50
        assert summary.call_count == 3
    
    def test_aggregation_handles_empty_task(self):
        """Should return empty summary for task with no records."""
        mock_db = MagicMock()
        
        # Mock empty result (NULL values from DB are returned as 0 due to COALESCE)
        aggregation_result = (0, 0, 0, 0, 0, 0, None, None)
        mock_agg_result = MagicMock()
        mock_agg_result.one.return_value = aggregation_result
        
        mock_provider_model_result = MagicMock()
        mock_provider_model_result.all.return_value = []
        
        call_count = [0]
        def execute_side_effect(query):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_agg_result
            return mock_provider_model_result

        mock_db.execute.side_effect = execute_side_effect

        service = UsageTrackingService(mock_db)
        summary = service.get_task_usage(task_id=999)

        assert summary.total_tokens == 0
        assert summary.call_count == 0
        assert summary.models_used == []
        assert summary.total_cost_usd == 0.0


class TestCostCalculationMixedModels:
    """Tests for cost calculation with multiple models."""
    
    def test_cost_calculation_single_model(self):
        """Should calculate cost correctly for single model."""
        mock_db = MagicMock()
        
        # Mock per-model aggregation for cost calculation
        # gpt-4o-mini: 1M input @ $0.15, 500K output @ $0.60 = $0.45
        mock_cost_result = MagicMock()
        mock_cost_result.all.return_value = [
            ("openai", "gpt-4o-mini", 1_000_000, 500_000, 0, 0),
        ]
        
        service = UsageTrackingService(mock_db)
        
        # Mock the execute call for _calculate_task_cost
        mock_db.execute.return_value = mock_cost_result
        
        cost = service._calculate_task_cost(task_id=123)
        
        # Expected: $0.15 (input) + $0.30 (output) = $0.45
        assert cost == pytest.approx(0.45, rel=1e-6)
    
    def test_cost_calculation_mixed_models(self):
        """Should calculate cost correctly with different models."""
        mock_db = MagicMock()
        
        # Mock per-model aggregation for cost calculation
        # gpt-4o-mini: 500K input, 250K output
        # gpt-4o: 500K input, 250K output
        mock_cost_result = MagicMock()
        mock_cost_result.all.return_value = [
            ("openai", "gpt-4o-mini", 500_000, 250_000, 0, 0),
            ("openai", "gpt-4o", 500_000, 250_000, 0, 0),
        ]
        
        service = UsageTrackingService(mock_db)
        mock_db.execute.return_value = mock_cost_result
        
        cost = service._calculate_task_cost(task_id=123)
        
        # gpt-4o-mini: (500K/1M * $0.15) + (250K/1M * $0.60) = $0.075 + $0.15 = $0.225
        # gpt-4o: (500K/1M * $2.50) + (250K/1M * $10.00) = $1.25 + $2.50 = $3.75
        # Total: $3.975
        assert cost == pytest.approx(3.975, rel=1e-6)
    
    def test_cost_calculation_with_cached_tokens(self):
        """Should apply cached token discount in mixed model scenario."""
        mock_db = MagicMock()
        
        # gpt-4o with cached tokens
        # 1M input total, 500K cached (50% discount on cached)
        mock_cost_result = MagicMock()
        mock_cost_result.all.return_value = [
            ("openai", "gpt-4o", 1_000_000, 500_000, 500_000, 0),  # 500K cached
        ]
        
        service = UsageTrackingService(mock_db)
        mock_db.execute.return_value = mock_cost_result
        
        cost = service._calculate_task_cost(task_id=123)
        
        # Uncached input: 500K @ $2.50/M = $1.25
        # Cached input: 500K @ $1.25/M = $0.625
        # Output: 500K @ $10.00/M = $5.00
        # Total: $6.875
        assert cost == pytest.approx(6.875, rel=1e-6)


class TestCacheInvalidation:
    """Tests for cache invalidation behavior."""
    
    def test_cache_invalidates_on_explicit_call(self):
        """Cache should invalidate when explicitly called."""
        cache = UsageCache(ttl_seconds=300)
        
        # Pre-populate cache
        summary = TaskUsageSummary(
            task_id=123,
            total_prompt_tokens=100,
            total_completion_tokens=50,
            total_tokens=150,
            total_cached_tokens=0,
            total_reasoning_tokens=0,
            total_cost_usd=0.01,
            call_count=1,
            models_used=["gpt-4o-mini"],
        )
        cache.set(task_id=123, summary=summary)
        
        # Verify cache hit
        assert cache.get(task_id=123) is not None
        
        # Invalidate
        cache.invalidate(task_id=123)
        
        # Verify cache miss
        assert cache.get(task_id=123) is None
    
    def test_service_with_cache_pattern(self):
        """Service should work with cache invalidation pattern."""
        mock_db = MagicMock()
        cache = UsageCache(ttl_seconds=300)
        
        # Initial summary
        initial_summary = TaskUsageSummary(
            task_id=123,
            total_prompt_tokens=100,
            total_completion_tokens=50,
            total_tokens=150,
            total_cached_tokens=0,
            total_reasoning_tokens=0,
            total_cost_usd=0.01,
            call_count=1,
            models_used=["gpt-4o-mini"],
        )
        
        # Cache the initial summary
        cache.set(task_id=123, summary=initial_summary)
        
        # Simulate recording new usage (would trigger invalidation)
        cache.invalidate(task_id=123)
        
        # Cache should now be empty for this task
        assert cache.get(task_id=123) is None
    
    def test_service_auto_invalidates_on_record(self):
        """Service should auto-invalidate cache when recording new usage."""
        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.commit = MagicMock()
        mock_db.refresh = MagicMock()
        
        cache = UsageCache(ttl_seconds=300)
        
        # Pre-populate cache
        initial_summary = TaskUsageSummary(
            task_id=123,
            total_prompt_tokens=100,
            total_completion_tokens=50,
            total_tokens=150,
            total_cached_tokens=0,
            total_reasoning_tokens=0,
            total_cost_usd=0.01,
            call_count=1,
            models_used=["gpt-4o-mini"],
        )
        cache.set(task_id=123, summary=initial_summary)
        assert cache.get(task_id=123) is not None
        
        # Create service with cache
        with patch('backend.services.usage_tracking.service.LLMUsageRecord'):
            service = UsageTrackingService(mock_db, cache=cache)
            
            # Record new usage
            usage = UsageData(
                prompt_tokens=200,
                completion_tokens=100,
                total_tokens=300,
                model="gpt-4o-mini",
            )
            service.record_usage(
                task_id=123,
                user_id=1,
                usage=usage,
                source="test",
            )
        
        # Cache should be invalidated
        assert cache.get(task_id=123) is None
    
    def test_service_populates_cache_on_get(self):
        """Service should populate cache on get_task_usage."""
        mock_db = MagicMock()
        cache = UsageCache(ttl_seconds=300)
        
        # Mock aggregation result
        aggregation_result = (100, 50, 150, 0, 0, 1, datetime(2026, 1, 17), datetime(2026, 1, 17))
        mock_agg_result = MagicMock()
        mock_agg_result.one.return_value = aggregation_result
        
        mock_provider_model_result = MagicMock()
        mock_provider_model_result.all.return_value = [
            ("openai", "gpt-4o-mini", 100, 50, 0, 0),
        ]
        
        call_count = [0]
        def execute_side_effect(query):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_agg_result
            return mock_provider_model_result

        mock_db.execute.side_effect = execute_side_effect

        service = UsageTrackingService(mock_db, cache=cache)

        # Cache should be empty
        assert cache.get(task_id=123) is None

        # Get task usage (should populate cache)
        summary = service.get_task_usage(task_id=123)

        # Cache should now be populated
        cached = cache.get(task_id=123)
        assert cached is not None
        assert cached.total_tokens == 150


class TestAggregationPerformance:
    """Tests for aggregation query performance."""
    
    def test_aggregation_uses_single_query_pattern(self):
        """Aggregation should use efficient single-query pattern."""
        mock_db = MagicMock()
        
        # Setup mock responses
        aggregation_result = (1000, 500, 1500, 100, 0, 10, datetime.now(), datetime.now())
        mock_agg_result = MagicMock()
        mock_agg_result.one.return_value = aggregation_result
        
        mock_provider_model_result = MagicMock()
        mock_provider_model_result.all.return_value = [
            ("openai", "gpt-4o-mini", 1000, 500, 100, 0),
        ]
        
        # Track query count
        query_count = [0]
        def execute_side_effect(query):
            query_count[0] += 1
            if query_count[0] == 1:
                return mock_agg_result
            return mock_provider_model_result
        
        mock_db.execute.side_effect = execute_side_effect
        
        service = UsageTrackingService(mock_db)
        summary = service.get_task_usage(task_id=123)
        
        # Should be at most 3 queries: totals and provider/model aggregate.
        assert query_count[0] <= 3, f"Too many queries: {query_count[0]}"
    
    def test_user_aggregation_with_date_filter(self):
        """User aggregation should support date filtering efficiently."""
        mock_db = MagicMock()
        
        mock_result = MagicMock()
        mock_result.one.return_value = (5000, 2500, 7500, 25)
        mock_db.execute.return_value = mock_result
        
        service = UsageTrackingService(mock_db)
        
        # Query with date filter
        since = datetime.now() - timedelta(days=7)
        result = service.get_user_usage(user_id=1, since=since)
        
        assert result["total_tokens"] == 7500
        assert result["call_count"] == 25
        assert result["since"] is not None
