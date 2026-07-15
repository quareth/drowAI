"""Tests for centralized truncation configuration.

Tests the truncation thresholds, soft margin logic, and helper functions.
"""

from __future__ import annotations

import pytest

from agent.utils.truncation_config import (
    THRESHOLDS,
    SOFT_MARGIN_PERCENT,
    MIN_TRUNCATION_FOR_FILE_HINT,
    get_threshold_for_type,
    get_effective_limit,
    should_suggest_file_reading,
    # Legacy exports
    DEFAULT_TOTAL_LIMIT,
    DEFAULT_HEAD_CHARS,
    DEFAULT_TAIL_CHARS,
    STDOUT_SNIPPET,
    STDERR_SNIPPET,
    MAX_STDOUT_EXCERPT_CHARS,
)


class TestThresholds:
    """Tests for output type thresholds."""
    
    def test_help_threshold_is_highest(self):
        """Help output should have the highest threshold to prevent loops."""
        assert THRESHOLDS["help"] >= THRESHOLDS["scan"]
        assert THRESHOLDS["help"] >= THRESHOLDS["log"]
        assert THRESHOLDS["help"] >= THRESHOLDS["default"]
    
    def test_all_thresholds_are_high(self):
        """All thresholds should be significantly higher than old 2000 char limit."""
        old_limit = 2000
        for output_type, threshold in THRESHOLDS.items():
            assert threshold > old_limit * 3, f"{output_type} threshold too low"
    
    def test_get_threshold_for_known_types(self):
        """Should return correct threshold for known types."""
        assert get_threshold_for_type("help") == THRESHOLDS["help"]
        assert get_threshold_for_type("scan") == THRESHOLDS["scan"]
        assert get_threshold_for_type("log") == THRESHOLDS["log"]
        assert get_threshold_for_type("default") == THRESHOLDS["default"]
    
    def test_get_threshold_for_unknown_type(self):
        """Unknown types should return default threshold."""
        assert get_threshold_for_type("unknown") == THRESHOLDS["default"]
        assert get_threshold_for_type("") == THRESHOLDS["default"]


class TestSoftMargin:
    """Tests for soft margin logic."""
    
    def test_effective_limit_adds_margin(self):
        """Effective limit should be higher than base limit."""
        base = 10000
        effective = get_effective_limit(base)
        assert effective > base
        
        expected_margin = int(base * SOFT_MARGIN_PERCENT / 100)
        assert effective == base + expected_margin
    
    def test_margin_percentage(self):
        """Margin should be configurable percentage."""
        assert SOFT_MARGIN_PERCENT > 0
        assert SOFT_MARGIN_PERCENT <= 50  # Sanity check - margin shouldn't be huge
    
    def test_effective_limit_scales_with_base(self):
        """Larger bases should have proportionally larger margins."""
        small = get_effective_limit(1000)
        large = get_effective_limit(10000)
        
        # Margins should scale proportionally
        small_margin = small - 1000
        large_margin = large - 10000
        
        # 10x base should have 10x margin
        assert large_margin == small_margin * 10


class TestFileReadingSuggestion:
    """Tests for file reading suggestion logic."""
    
    def test_small_truncation_no_suggestion(self):
        """Small truncations shouldn't suggest file reading."""
        assert not should_suggest_file_reading(500)
        assert not should_suggest_file_reading(1000)
        assert not should_suggest_file_reading(MIN_TRUNCATION_FOR_FILE_HINT - 1)
    
    def test_large_truncation_suggests_reading(self):
        """Large truncations should suggest file reading."""
        assert should_suggest_file_reading(MIN_TRUNCATION_FOR_FILE_HINT)
        assert should_suggest_file_reading(MIN_TRUNCATION_FOR_FILE_HINT + 1000)
        assert should_suggest_file_reading(10000)
    
    def test_threshold_is_reasonable(self):
        """Threshold for file reading should be reasonable."""
        # At least 1000 chars should be truncated before suggesting file reading
        assert MIN_TRUNCATION_FOR_FILE_HINT >= 1000
        # But not so high that we never suggest it
        assert MIN_TRUNCATION_FOR_FILE_HINT <= 5000


class TestLegacyCompatibility:
    """Tests for backward-compatible legacy exports."""
    
    def test_default_total_limit_exported(self):
        """DEFAULT_TOTAL_LIMIT should be exported for backward compatibility."""
        assert DEFAULT_TOTAL_LIMIT > 0
        assert DEFAULT_TOTAL_LIMIT == THRESHOLDS["default"]
    
    def test_head_tail_chars_exported(self):
        """Head and tail char limits should be exported."""
        assert DEFAULT_HEAD_CHARS > 0
        assert DEFAULT_TAIL_CHARS > 0
        # Head + tail should be less than total limit
        assert DEFAULT_HEAD_CHARS + DEFAULT_TAIL_CHARS < DEFAULT_TOTAL_LIMIT
    
    def test_snippet_limits_exported(self):
        """Executor snippet limits should be exported."""
        assert STDOUT_SNIPPET > 0
        assert STDERR_SNIPPET > 0
    
    def test_max_excerpt_chars_exported(self):
        """Prompt excerpt limit should be exported."""
        assert MAX_STDOUT_EXCERPT_CHARS > 0
        # Should be higher than old 1500 limit
        assert MAX_STDOUT_EXCERPT_CHARS > 1500


class TestConfigurationValues:
    """Tests to ensure configuration values are sensible."""
    
    def test_thresholds_not_too_high(self):
        """Thresholds shouldn't exceed reasonable LLM context limits."""
        # Even 128K context LLMs shouldn't get 50K char outputs
        max_reasonable = 50000
        for output_type, threshold in THRESHOLDS.items():
            assert threshold < max_reasonable, f"{output_type} threshold too high"
    
    def test_margin_is_percentage(self):
        """Soft margin should be a valid percentage."""
        assert 0 < SOFT_MARGIN_PERCENT <= 100
    
    def test_all_thresholds_present(self):
        """All expected output types should have thresholds."""
        expected_types = {"help", "scan", "log", "default"}
        assert expected_types <= set(THRESHOLDS.keys())


