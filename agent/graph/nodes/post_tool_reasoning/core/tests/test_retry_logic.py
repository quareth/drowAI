"""Tests for capability-agnostic retry logic."""

import pytest

from ..retry_logic import (
    MAX_RETRIES,
    RETRY_METADATA_KEY,
    get_retry_count,
    can_retry,
    increment_retry_count,
)


class TestGetRetryCount:
    """Tests for get_retry_count function."""
    
    def test_get_retry_count_from_metadata(self):
        """Verify retry count extracted from metadata."""
        metadata = {
            RETRY_METADATA_KEY: {"count": 2}
        }
        
        count = get_retry_count(metadata)
        
        assert count == 2
    
    def test_get_retry_count_no_retry_data(self):
        """Verify returns 0 when no retry data exists."""
        metadata = {}
        
        count = get_retry_count(metadata)
        
        assert count == 0
    
    def test_get_retry_count_empty_retry_data(self):
        """Verify returns 0 when retry data is empty dict."""
        metadata = {
            RETRY_METADATA_KEY: {}
        }
        
        count = get_retry_count(metadata)
        
        assert count == 0
    
    def test_get_retry_count_none_retry_data(self):
        """Verify returns 0 when retry data is None."""
        metadata = {
            RETRY_METADATA_KEY: None
        }
        
        count = get_retry_count(metadata)
        
        assert count == 0


class TestCanRetry:
    """Tests for can_retry function."""
    
    def test_can_retry_below_limit(self):
        """Verify retry allowed when below limit."""
        assert can_retry(0) is True
        assert can_retry(1) is True
        assert can_retry(3) is True
    
    def test_can_retry_at_limit(self):
        """Verify retry not allowed at limit."""
        assert can_retry(MAX_RETRIES) is False
    
    def test_can_retry_above_limit(self):
        """Verify retry not allowed above limit."""
        assert can_retry(MAX_RETRIES + 1) is False
        assert can_retry(MAX_RETRIES + 10) is False
    
    def test_can_retry_custom_limit(self):
        """Verify custom max_retries parameter works."""
        assert can_retry(2, max_retries=3) is True
        assert can_retry(3, max_retries=3) is False


class TestIncrementRetryCount:
    """Tests for increment_retry_count function."""
    
    def test_increment_retry_count_from_zero(self):
        """Verify retry count increments from 0 to 1."""
        metadata = {}
        
        new_metadata = increment_retry_count(metadata)
        
        assert new_metadata[RETRY_METADATA_KEY]["count"] == 1
    
    def test_increment_retry_count_from_existing(self):
        """Verify retry count increments from existing value."""
        metadata = {
            RETRY_METADATA_KEY: {"count": 2}
        }
        
        new_metadata = increment_retry_count(metadata)
        
        assert new_metadata[RETRY_METADATA_KEY]["count"] == 3
    
    def test_increment_retry_count_caps_at_max(self):
        """Verify retry count caps at MAX_RETRIES."""
        metadata = {
            RETRY_METADATA_KEY: {"count": MAX_RETRIES}
        }
        
        new_metadata = increment_retry_count(metadata)
        
        assert new_metadata[RETRY_METADATA_KEY]["count"] == MAX_RETRIES
    
    def test_increment_retry_count_no_mutation(self):
        """Verify original metadata not mutated (pure function)."""
        metadata = {
            RETRY_METADATA_KEY: {"count": 1},
            "other_data": "value"
        }
        original_count = metadata[RETRY_METADATA_KEY]["count"]
        
        new_metadata = increment_retry_count(metadata)
        
        # Original unchanged
        assert metadata[RETRY_METADATA_KEY]["count"] == original_count
        # New incremented
        assert new_metadata[RETRY_METADATA_KEY]["count"] == original_count + 1
        # Other data preserved
        assert new_metadata["other_data"] == "value"
    
    def test_increment_retry_count_preserves_other_metadata(self):
        """Verify other metadata keys preserved."""
        metadata = {
            "synthesized_output": {"summary": "test"},
            "last_tool_result": {"stdout": "output"},
        }
        
        new_metadata = increment_retry_count(metadata)
        
        assert "synthesized_output" in new_metadata
        assert "last_tool_result" in new_metadata
        assert new_metadata["synthesized_output"] == metadata["synthesized_output"]

