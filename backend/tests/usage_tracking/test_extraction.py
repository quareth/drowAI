"""Tests for provider-aware usage extraction.

This module verifies that provider response parsing lives behind the Remote Runtime
extractor boundary while preserving the existing normalized ``UsageData``
contract for OpenAI callers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services.usage_tracking.extraction import (
    UnsupportedUsageExtractorError,
    UsageExtractionTarget,
    extract_usage,
)
from backend.services.usage_tracking.models import (
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
    UsageData,
)
from backend.services.usage_tracking.pricing import calculate_cost


class TestUsageExtractorRegistry:
    """Provider-aware extraction should dispatch by provider and API surface."""

    def test_openai_chat_extractor_preserves_existing_shape(self):
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 150
        response.usage.prompt_tokens_details = MagicMock()
        response.usage.prompt_tokens_details.cached_tokens = 25

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="openai",
                model="gpt-4o-mini",
                api_surface="chat_completions",
            ),
        )

        assert usage == UsageData.from_openai_chat_response(response, "gpt-4o-mini")
        assert usage.cache_reporting == CACHE_REPORTING_REPORTED

    def test_openai_responses_extractor_reads_nested_cache_and_reasoning(self):
        response = MagicMock()
        response.usage = MagicMock()
        response.usage.input_tokens = 200
        response.usage.output_tokens = 100
        response.usage.total_tokens = 800
        response.usage.input_tokens_details = MagicMock()
        response.usage.input_tokens_details.cached_tokens = 40
        response.usage.output_tokens_details = MagicMock()
        response.usage.output_tokens_details.reasoning_tokens = 500

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="openai",
                model="gpt-5",
                api_surface="responses",
            ),
        )

        assert usage.prompt_tokens == 200
        assert usage.completion_tokens == 100
        assert usage.total_tokens == 800
        assert usage.cached_tokens == 40
        assert usage.reasoning_tokens == 500
        assert usage.cache_reporting == CACHE_REPORTING_REPORTED

    def test_openai_responses_extractor_preserves_gpt56_cache_writes(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=100,
                total_tokens=300,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=40,
                    cache_write_tokens=60,
                ),
                output_tokens_details=SimpleNamespace(reasoning_tokens=25),
            )
        )

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="openai",
                model="gpt-5.6-sol",
                api_surface="responses",
            ),
        )

        assert usage.provider_usage_components is not None
        assert usage.provider_usage_components.components == {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "cache_write_tokens": 60,
            "output_tokens": 100,
        }

    def test_openai_chat_extractor_preserves_gpt56_cache_writes(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=200,
                completion_tokens=100,
                total_tokens=300,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=40,
                    cache_write_tokens=60,
                ),
            )
        )

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="openai",
                model="gpt-5.6-sol",
                api_surface="chat_completions",
            ),
        )

        assert usage.provider_usage_components is not None
        assert usage.provider_usage_components.components == {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "cache_write_tokens": 60,
            "output_tokens": 100,
        }

    def test_anthropic_extractor_preserves_provider_components(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                cache_creation_input_tokens=30,
                cache_read_input_tokens=20,
                output_tokens=50,
            )
        )

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="anthropic",
                model="claude-sonnet-4-5",
                api_surface="messages",
            ),
        )

        assert usage.provider == "anthropic"
        assert usage.api_surface == "messages"
        assert usage.cache_reporting == CACHE_REPORTING_UNKNOWN
        assert usage.prompt_tokens == 150
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 200
        assert usage.cached_tokens == 0
        assert usage.provider_usage_components is not None
        assert usage.provider_usage_components.to_dict() == {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 20,
                "output_tokens": 50,
            },
        }

    def test_anthropic_extractor_splits_5m_and_1h_cache_creation(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=1_000_000,
                cache_creation_input_tokens=2_000_000,
                cache_read_input_tokens=1_000_000,
                output_tokens=1_000_000,
                cache_creation=SimpleNamespace(
                    ephemeral_5m_input_tokens=1_000_000,
                    ephemeral_1h_input_tokens=1_000_000,
                ),
            )
        )

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="anthropic",
                model="claude-opus-4-8",
                api_surface="messages",
            ),
        )

        assert usage.prompt_tokens == 4_000_000
        assert usage.completion_tokens == 1_000_000
        assert usage.total_tokens == 5_000_000
        assert usage.provider_usage_components is not None
        assert usage.provider_usage_components.components == {
            "input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
            "cache_creation_input_tokens_1h": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        }
        assert calculate_cost(usage) == pytest.approx(46.75, rel=1e-6)

    def test_anthropic_extractor_reports_adaptive_thinking_tokens(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=50,
                output_tokens_details=SimpleNamespace(thinking_tokens=35),
            )
        )

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="anthropic",
                model="claude-fable-5",
                api_surface="messages",
            ),
        )

        assert usage.reasoning_tokens == 35

    def test_missing_usage_preserves_target_identity(self):
        response = SimpleNamespace(usage=None)

        usage = extract_usage(
            response,
            UsageExtractionTarget(
                provider="anthropic",
                model="claude-sonnet-4-5",
                api_surface="messages",
            ),
        )

        assert usage.is_empty()
        assert usage.provider == "anthropic"
        assert usage.model == "claude-sonnet-4-5"
        assert usage.api_surface == "messages"

    def test_unknown_extractor_raises_controlled_error(self):
        with pytest.raises(UnsupportedUsageExtractorError):
            extract_usage(
                SimpleNamespace(usage=None),
                UsageExtractionTarget(
                    provider="unknown",
                    model="model-x",
                    api_surface="messages",
                ),
            )
