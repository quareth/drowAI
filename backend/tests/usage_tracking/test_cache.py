"""Tests for UsageCache.

These tests verify the TTL-based caching for usage summaries works
correctly, including thread safety, expiration, and invalidation.
"""

import pytest
import time
import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

from backend.services.usage_tracking.cache import UsageCache, get_default_cache
from backend.services.usage_tracking.models import TaskUsageSummary


class TestUsageCacheBasicOperations:
    """Tests for basic cache operations."""
    
    def test_get_returns_none_for_empty_cache(self):
        """Should return None for missing entries."""
        cache = UsageCache(ttl_seconds=30)
        
        result = cache.get(task_id=999)
        
        assert result is None
    
    def test_set_and_get(self):
        """Should store and retrieve summary."""
        cache = UsageCache(ttl_seconds=30)
        summary = TaskUsageSummary(
            task_id=123,
            total_prompt_tokens=1000,
            total_completion_tokens=500,
            total_tokens=1500,
            total_cached_tokens=0,
            total_reasoning_tokens=0,
            total_cost_usd=0.05,
            call_count=5,
            models_used=["gpt-4o-mini"],
        )
        
        cache.set(task_id=123, summary=summary)
        result = cache.get(task_id=123)
        
        assert result is not None
        assert result.task_id == 123
        assert result.total_tokens == 1500
        assert result.call_count == 5
    
    def test_invalidate_removes_entry(self):
        """Should remove entry on invalidation."""
        cache = UsageCache(ttl_seconds=30)
        summary = TaskUsageSummary.empty(task_id=123)
        
        cache.set(task_id=123, summary=summary)
        assert cache.get(task_id=123) is not None
        
        removed = cache.invalidate(task_id=123)
        
        assert removed is True
        assert cache.get(task_id=123) is None
    
    def test_invalidate_returns_false_for_missing(self):
        """Should return False when invalidating missing entry."""
        cache = UsageCache(ttl_seconds=30)
        
        removed = cache.invalidate(task_id=999)
        
        assert removed is False
    
    def test_clear_removes_all(self):
        """Should remove all entries."""
        cache = UsageCache(ttl_seconds=30)
        
        for i in range(10):
            cache.set(task_id=i, summary=TaskUsageSummary.empty(task_id=i))
        
        assert cache.size() == 10
        
        count = cache.clear()
        
        assert count == 10
        assert cache.size() == 0


class TestUsageCacheTTL:
    """Tests for TTL expiration."""
    
    def test_entry_expires_after_ttl(self):
        """Should return None for expired entries."""
        cache = UsageCache(ttl_seconds=1)  # 1 second TTL
        summary = TaskUsageSummary.empty(task_id=123)
        
        cache.set(task_id=123, summary=summary)
        assert cache.get(task_id=123) is not None
        
        # Wait for TTL to expire
        time.sleep(1.1)
        
        result = cache.get(task_id=123)
        assert result is None
    
    def test_entry_valid_before_ttl(self):
        """Should return entry before TTL expires."""
        cache = UsageCache(ttl_seconds=10)
        summary = TaskUsageSummary.empty(task_id=123)
        
        cache.set(task_id=123, summary=summary)
        
        # Access multiple times within TTL
        for _ in range(5):
            result = cache.get(task_id=123)
            assert result is not None
            time.sleep(0.1)
    
    def test_cleanup_removes_expired(self):
        """Should remove expired entries on cleanup."""
        cache = UsageCache(ttl_seconds=1)
        
        for i in range(5):
            cache.set(task_id=i, summary=TaskUsageSummary.empty(task_id=i))
        
        assert cache.size() == 5
        
        # Wait for expiration
        time.sleep(1.1)
        
        removed = cache.cleanup()
        
        assert removed == 5
        assert cache.size() == 0


class TestUsageCacheStats:
    """Tests for cache statistics."""
    
    def test_stats_empty_cache(self):
        """Should return correct stats for empty cache."""
        cache = UsageCache(ttl_seconds=30)
        
        stats = cache.stats()
        
        assert stats["total"] == 0
        assert stats["valid"] == 0
        assert stats["expired"] == 0
        assert stats["ttl_seconds"] == 30
    
    def test_stats_with_entries(self):
        """Should count valid and expired entries."""
        cache = UsageCache(ttl_seconds=1)
        
        # Add some entries
        for i in range(3):
            cache.set(task_id=i, summary=TaskUsageSummary.empty(task_id=i))
        
        stats = cache.stats()
        assert stats["total"] == 3
        assert stats["valid"] == 3
        assert stats["expired"] == 0
        
        # Wait for expiration
        time.sleep(1.1)
        
        # Add fresh entries
        cache.set(task_id=100, summary=TaskUsageSummary.empty(task_id=100))
        
        stats = cache.stats()
        assert stats["total"] == 4
        assert stats["valid"] == 1
        assert stats["expired"] == 3


class TestUsageCacheThreadSafety:
    """Tests for thread safety."""
    
    def test_concurrent_reads_and_writes(self):
        """Should handle concurrent access safely."""
        cache = UsageCache(ttl_seconds=30)
        errors = []
        
        def writer(task_id: int):
            try:
                for _ in range(100):
                    cache.set(task_id, TaskUsageSummary.empty(task_id=task_id))
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
        
        def reader(task_id: int):
            try:
                for _ in range(100):
                    cache.get(task_id)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
        
        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=writer, args=(i,)))
            threads.append(threading.Thread(target=reader, args=(i,)))
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Thread errors: {errors}"
    
    def test_concurrent_invalidation(self):
        """Should handle concurrent invalidation safely."""
        cache = UsageCache(ttl_seconds=30)
        
        # Pre-populate cache
        for i in range(100):
            cache.set(task_id=i, summary=TaskUsageSummary.empty(task_id=i))
        
        errors = []
        
        def invalidator():
            try:
                for i in range(100):
                    cache.invalidate(i)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=invalidator) for _ in range(5)]
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Thread errors: {errors}"


class TestDefaultCache:
    """Tests for module-level default cache."""
    
    def test_get_default_cache_returns_same_instance(self):
        """Should return singleton instance."""
        # Reset the module-level singleton
        import backend.services.usage_tracking.cache as cache_module
        cache_module._default_cache = None
        
        cache1 = get_default_cache(ttl_seconds=30)
        cache2 = get_default_cache(ttl_seconds=60)  # Different TTL ignored
        
        assert cache1 is cache2
    
    def test_default_cache_is_functional(self):
        """Default cache should work correctly."""
        # Reset the module-level singleton
        import backend.services.usage_tracking.cache as cache_module
        cache_module._default_cache = None
        
        cache = get_default_cache(ttl_seconds=30)
        summary = TaskUsageSummary.empty(task_id=999)
        
        cache.set(task_id=999, summary=summary)
        result = cache.get(task_id=999)
        
        assert result is not None
        assert result.task_id == 999


class TestUsageCacheIntegrationPattern:
    """Tests for typical integration patterns."""
    
    def test_cache_with_service_pattern(self):
        """Should work with typical service integration."""
        cache = UsageCache(ttl_seconds=30)
        
        # Simulate service queries
        query_count = 0
        
        def mock_get_task_usage(task_id: int) -> TaskUsageSummary:
            nonlocal query_count
            query_count += 1
            return TaskUsageSummary(
                task_id=task_id,
                total_prompt_tokens=100 * query_count,
                total_completion_tokens=50 * query_count,
                total_tokens=150 * query_count,
                total_cached_tokens=0,
                total_reasoning_tokens=0,
                total_cost_usd=0.01 * query_count,
                call_count=query_count,
                models_used=["gpt-4o-mini"],
            )
        
        # First access - cache miss, query DB
        summary = cache.get(task_id=123)
        if summary is None:
            summary = mock_get_task_usage(task_id=123)
            cache.set(task_id=123, summary=summary)
        
        assert query_count == 1
        assert summary.total_tokens == 150
        
        # Second access - cache hit, no DB query
        summary = cache.get(task_id=123)
        if summary is None:
            summary = mock_get_task_usage(task_id=123)
            cache.set(task_id=123, summary=summary)
        
        assert query_count == 1  # No new query
        assert summary.total_tokens == 150  # Same data
        
        # Invalidate and re-query
        cache.invalidate(task_id=123)
        
        summary = cache.get(task_id=123)
        if summary is None:
            summary = mock_get_task_usage(task_id=123)
            cache.set(task_id=123, summary=summary)
        
        assert query_count == 2  # New query after invalidation
        assert summary.total_tokens == 300  # Updated data
