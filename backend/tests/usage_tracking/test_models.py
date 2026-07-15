"""Tests for UsageData model and factory methods.

These tests verify that UsageData correctly extracts token counts
from different API response formats, and that the per-call
cache-reporting classifier labels provider surfaces honestly.
"""

import pytest
from unittest.mock import MagicMock

from backend.services.usage_tracking.models import (
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
    ProviderUsageComponents,
    TaskUsageSummary,
    UsageData,
    classify_cache_reporting,
)


class TestUsageDataFromChatResponse:
    """Tests for UsageData.from_openai_chat_response()"""
    
    def test_extracts_basic_usage(self):
        """Should extract prompt, completion, and total tokens."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 150
        response.usage.prompt_tokens_details = None
        
        usage = UsageData.from_openai_chat_response(response, "gpt-4o-mini")
        
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150
        assert usage.model == "gpt-4o-mini"
        assert usage.provider == "openai"
    
    def test_extracts_cached_tokens(self):
        """Should extract cached tokens from prompt_tokens_details."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 150
        response.usage.prompt_tokens_details = MagicMock()
        response.usage.prompt_tokens_details.cached_tokens = 30
        
        usage = UsageData.from_openai_chat_response(response, "gpt-4o")
        
        assert usage.cached_tokens == 30
        assert usage.prompt_tokens == 100
    
    def test_handles_missing_usage(self):
        """Should return empty UsageData when response has no usage."""
        response = MagicMock()
        response.usage = None
        
        usage = UsageData.from_openai_chat_response(response, "gpt-4o-mini")
        
        assert usage.is_empty()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.model == "gpt-4o-mini"
    
    def test_handles_none_token_values(self):
        """Should handle None values in usage fields."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = None
        response.usage.completion_tokens = None
        response.usage.total_tokens = None
        response.usage.prompt_tokens_details = None
        
        usage = UsageData.from_openai_chat_response(response, "gpt-4o")
        
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
    
    def test_corrects_inconsistent_total(self):
        """Should ensure total is at least prompt + completion."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 100  # Incorrect total
        response.usage.prompt_tokens_details = None
        
        usage = UsageData.from_openai_chat_response(response, "gpt-4o")
        
        assert usage.total_tokens == 150  # Corrected


class TestUsageDataFromResponsesAPI:
    """Tests for UsageData.from_openai_responses_api()"""
    
    def test_extracts_basic_usage(self):
        """Should extract input_tokens and output_tokens."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 200
        response.usage.output_tokens = 100
        response.usage.total_tokens = 300
        response.usage.input_tokens_details = MagicMock()
        response.usage.input_tokens_details.cached_tokens = 25
        response.usage.output_tokens_details = MagicMock()
        response.usage.output_tokens_details.reasoning_tokens = 0
        
        usage = UsageData.from_openai_responses_api(response, "gpt-5")
        
        assert usage.prompt_tokens == 200
        assert usage.completion_tokens == 100
        assert usage.total_tokens == 300
        assert usage.cached_tokens == 25
        assert usage.model == "gpt-5"
    
    def test_extracts_reasoning_tokens(self):
        """Should extract reasoning_tokens for extended thinking."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 200
        response.usage.output_tokens = 100
        response.usage.total_tokens = 800
        response.usage.input_tokens_details = MagicMock()
        response.usage.input_tokens_details.cached_tokens = 0
        response.usage.output_tokens_details = MagicMock()
        response.usage.output_tokens_details.reasoning_tokens = 500
        
        usage = UsageData.from_openai_responses_api(response, "gpt-5-pro")
        
        assert usage.reasoning_tokens == 500
        assert usage.prompt_tokens == 200
        assert usage.completion_tokens == 100
        assert usage.total_tokens == 800
    
    def test_handles_missing_usage(self):
        """Should return empty UsageData when response has no usage."""
        response = MagicMock()
        response.usage = None
        
        usage = UsageData.from_openai_responses_api(response, "gpt-5")
        
        assert usage.is_empty()
        assert usage.model == "gpt-5"
    
    def test_handles_missing_reasoning_tokens(self):
        """Should handle responses without reasoning_tokens field."""
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 200
        response.usage.output_tokens = 100
        response.usage.total_tokens = 300
        # No output_tokens_details.reasoning_tokens field
        response.usage.input_tokens_details = MagicMock()
        response.usage.input_tokens_details.cached_tokens = 0
        del response.usage.output_tokens_details
        del response.usage.reasoning_tokens
        
        usage = UsageData.from_openai_responses_api(response, "gpt-5.1")
        
        assert usage.reasoning_tokens == 0
        assert usage.prompt_tokens == 200


class TestUsageDataEmpty:
    """Tests for UsageData.empty()"""
    
    def test_returns_zero_values(self):
        """Should return UsageData with all zero values."""
        usage = UsageData.empty("test-model")
        
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.cached_tokens == 0
        assert usage.reasoning_tokens == 0
        assert usage.model == "test-model"
    
    def test_is_empty_returns_true(self):
        """is_empty() should return True for empty usage."""
        usage = UsageData.empty()
        
        assert usage.is_empty() is True
    
    def test_is_empty_returns_false_for_non_empty(self):
        """is_empty() should return False for non-empty usage."""
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o",
        )
        
        assert usage.is_empty() is False


class TestUsageDataImmutability:
    """Tests for UsageData immutability."""
    
    def test_is_frozen(self):
        """Should not allow attribute modification."""
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o",
        )
        
        with pytest.raises(AttributeError):
            usage.prompt_tokens = 200  # type: ignore


class TestTaskUsageSummary:
    """Tests for TaskUsageSummary."""

    def test_empty_summary(self):
        """Should create empty summary for task with no usage."""
        summary = TaskUsageSummary.empty(task_id=123)

        assert summary.task_id == 123
        assert summary.total_prompt_tokens == 0
        assert summary.total_completion_tokens == 0
        assert summary.total_tokens == 0
        assert summary.total_cost_usd == 0.0
        assert summary.call_count == 0
        assert summary.models_used == []
        assert summary.first_call is None
        assert summary.last_call is None


# ---------------------------------------------------------------------------
# Task 1.3 — Cache-reporting classifier and extractor labeling.
# ---------------------------------------------------------------------------


class TestClassifyCacheReporting:
    """``classify_cache_reporting`` is the single place provider +
    api_surface are consulted to derive the cache-reporting label."""

    def test_openai_chat_completions_is_reported(self):
        assert classify_cache_reporting("openai", "chat_completions") == CACHE_REPORTING_REPORTED

    def test_openai_responses_is_reported(self):
        assert classify_cache_reporting("openai", "responses") == CACHE_REPORTING_REPORTED

    def test_unknown_provider_surface_falls_back_to_unknown(self):
        assert classify_cache_reporting("anthropic", "messages") == CACHE_REPORTING_UNKNOWN
        assert classify_cache_reporting("openai", "brand_new_surface") == CACHE_REPORTING_UNKNOWN

    def test_empty_or_missing_strings_are_unknown(self):
        assert classify_cache_reporting("", "chat_completions") == CACHE_REPORTING_UNKNOWN
        assert classify_cache_reporting("openai", "") == CACHE_REPORTING_UNKNOWN
        assert classify_cache_reporting("unknown", "unknown") == CACHE_REPORTING_UNKNOWN

    def test_classifier_is_case_and_whitespace_tolerant(self):
        # Defensive: provider / surface strings sometimes come through
        # with trailing whitespace or mixed case. The classifier should
        # still resolve to the canonical label rather than degrading to
        # "unknown" for cosmetic reasons.
        assert classify_cache_reporting("OpenAI", "Chat_Completions") == CACHE_REPORTING_REPORTED
        assert classify_cache_reporting(" openai ", " responses ") == CACHE_REPORTING_REPORTED

    def test_non_string_inputs_are_unknown(self):
        assert classify_cache_reporting(None, "chat_completions") == CACHE_REPORTING_UNKNOWN  # type: ignore[arg-type]
        assert classify_cache_reporting("openai", None) == CACHE_REPORTING_UNKNOWN  # type: ignore[arg-type]


class TestUsageDataCacheReportingExtraction:
    """Factory methods must stamp ``api_surface`` / ``cache_reporting``
    onto ``UsageData`` at extraction time so the read side inherits a
    honest label without re-parsing ``source`` strings."""

    def test_chat_completions_with_cached_tokens_labels_reported(self):
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 150
        response.usage.prompt_tokens_details = MagicMock()
        response.usage.prompt_tokens_details.cached_tokens = 30

        usage = UsageData.from_openai_chat_response(response, "gpt-4o-mini")

        assert usage.cached_tokens == 30
        assert usage.api_surface == "chat_completions"
        assert usage.cache_reporting == CACHE_REPORTING_REPORTED

    def test_chat_completions_with_zero_cached_is_still_reported(self):
        # Core honesty guarantee: a provider surface that DOES expose
        # cache info but happened to report 0 is ``reported``. The
        # insights layer must distinguish this from a surface that never
        # reports cache.
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 80
        response.usage.completion_tokens = 20
        response.usage.total_tokens = 100
        response.usage.prompt_tokens_details = MagicMock()
        response.usage.prompt_tokens_details.cached_tokens = 0

        usage = UsageData.from_openai_chat_response(response, "gpt-4o")

        assert usage.cached_tokens == 0
        assert usage.cache_reporting == CACHE_REPORTING_REPORTED
        assert usage.api_surface == "chat_completions"

    def test_chat_completions_without_details_still_labels_reported(self):
        # Even when ``prompt_tokens_details`` is missing, the surface
        # itself is known to support cache reporting — the row simply
        # carries 0 cached tokens. The label stays ``reported``; the
        # backward-compat extraction of ``cached_tokens=0`` is unchanged.
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 150
        response.usage.prompt_tokens_details = None

        usage = UsageData.from_openai_chat_response(response, "gpt-4o-mini")

        assert usage.cached_tokens == 0
        assert usage.cache_reporting == CACHE_REPORTING_REPORTED

    def test_responses_api_reports_cached_tokens(self):
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 200
        response.usage.output_tokens = 100
        response.usage.total_tokens = 350
        response.usage.input_tokens_details = MagicMock()
        response.usage.input_tokens_details.cached_tokens = 40
        response.usage.output_tokens_details = MagicMock()
        response.usage.output_tokens_details.reasoning_tokens = 50

        usage = UsageData.from_openai_responses_api(response, "gpt-5")

        assert usage.cached_tokens == 40
        assert usage.reasoning_tokens == 50
        assert usage.total_tokens == 350
        assert usage.api_surface == "responses"
        assert usage.cache_reporting == CACHE_REPORTING_REPORTED

    def test_empty_usage_defaults_to_unknown_reporting(self):
        # ``empty()`` is the fallback for malformed / missing responses;
        # we don't know which surface it came from.
        usage = UsageData.empty("gpt-4o")

        assert usage.api_surface == CACHE_REPORTING_UNKNOWN
        assert usage.cache_reporting == CACHE_REPORTING_UNKNOWN

    def test_missing_usage_preserves_extraction_target_surface(self):
        response = MagicMock()
        response.usage = None

        chat_usage = UsageData.from_openai_chat_response(response, "gpt-4o")
        resp_usage = UsageData.from_openai_responses_api(response, "gpt-5")

        # Extractor-target identity is preserved even for empty provider
        # responses; the standalone ``empty()`` fallback remains unknown.
        assert chat_usage.api_surface == "chat_completions"
        assert chat_usage.cache_reporting == CACHE_REPORTING_REPORTED
        assert resp_usage.api_surface == "responses"
        assert resp_usage.cache_reporting == CACHE_REPORTING_REPORTED

    def test_default_constructor_still_unknown(self):
        # Backward compatibility: legacy callers build ``UsageData`` via
        # the constructor with no surface hint. Defaults must preserve
        # "unknown" (NOT silently report zero cache as "reported").
        usage = UsageData(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            model="gpt-4o",
        )
        assert usage.api_surface == CACHE_REPORTING_UNKNOWN
        assert usage.cache_reporting == CACHE_REPORTING_UNKNOWN


class TestUsageDataToDictPropagatesCacheSignals:
    """``UsageData.to_dict`` must emit ``api_surface`` / ``cache_reporting``
    so the graph's ``trace.usage_records`` dict carries the signal through
    to the handler-boundary metadata builder."""

    def test_to_dict_includes_cache_reporting_and_surface(self):
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
            provider="openai",
            cached_tokens=10,
            api_surface="chat_completions",
            cache_reporting=CACHE_REPORTING_REPORTED,
        )

        result = usage.to_dict(source="simple_chat")

        assert result["api_surface"] == "chat_completions"
        assert result["cache_reporting"] == CACHE_REPORTING_REPORTED
        # All the legacy keys remain so existing consumers don't break.
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["cached_tokens"] == 10
        assert result["source"] == "simple_chat"
        assert result["provider"] == "openai"

    def test_to_dict_backwards_compatible_keys_unchanged(self):
        """Legacy fields must keep their exact names so downstream
        persistence / insights code doesn't silently lose tokens."""
        usage = UsageData(
            prompt_tokens=1, completion_tokens=1, total_tokens=2, model="m"
        )
        result = usage.to_dict("x")

        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "model",
            "provider",
            "cached_tokens",
            "reasoning_tokens",
            "source",
        ):
            assert key in result, f"legacy key {key!r} missing from to_dict"

    def test_to_dict_includes_provider_usage_components_when_present(self):
        usage = UsageData(
            prompt_tokens=150,
            completion_tokens=50,
            total_tokens=200,
            model="claude-sonnet-4-5",
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 50,
                },
            ),
        )

        result = usage.to_dict("planner")

        assert result["provider_usage_components"] == {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 20,
                "output_tokens": 50,
            },
        }
