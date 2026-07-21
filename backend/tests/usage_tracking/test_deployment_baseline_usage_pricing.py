"""Baseline tests for usage provenance, cache labels, and pricing status semantics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.context.context_window_policy import estimate_chat_history_tokens
from agent.providers.llm.core.identity import ProviderModelRef
from backend.services.usage_tracking.extraction import (
    UsageExtractionTarget,
    extract_usage,
)
from backend.services.usage_tracking.models import (
    CACHE_REPORTING_REPORTED,
    CACHE_REPORTING_UNKNOWN,
    UsageData,
)
from backend.services.usage_tracking.pricing import pricing_quote_for_usage
from backend.services.usage_tracking.pricing_registry import (
    PRICING_AVAILABLE,
    PRICING_ESTIMATED,
    PRICING_PARTIAL,
    PRICING_UNAVAILABLE,
    aggregate_pricing_statuses,
    get_pricing_quote,
)
from backend.services.usage_tracking.service import UsageTrackingService


def test_context_estimates_are_not_persisted_as_actual_usage_records() -> None:
    estimate = estimate_chat_history_tokens(
        provider="openai",
        model="gpt-5.2",
        history=[{"role": "user", "content": "estimate-only context"}],
    )
    mock_db = MagicMock()

    with patch("backend.services.usage_tracking.service.LLMUsageRecord") as record_cls:
        service = UsageTrackingService(mock_db)
        actual_usage = UsageData(
            prompt_tokens=123,
            completion_tokens=45,
            total_tokens=168,
            provider="openai",
            model="gpt-5.2",
            api_surface="responses",
        )

        service.record_usage(
            task_id=7,
            user_id=1,
            usage=actual_usage,
            source="deployment_baseline",
        )

    record_cls.assert_called_once()
    call_kwargs = record_cls.call_args.kwargs
    assert call_kwargs["prompt_tokens"] == actual_usage.prompt_tokens
    assert call_kwargs["completion_tokens"] == actual_usage.completion_tokens
    assert call_kwargs["total_tokens"] == actual_usage.total_tokens
    assert call_kwargs["total_tokens"] != estimate.tokens
    assert call_kwargs["request_metadata"] is None
    assert mock_db.add.called
    assert mock_db.commit.called


def test_openai_responses_usage_preserves_cache_reporting_and_component_labels() -> None:
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

    assert usage.cache_reporting == CACHE_REPORTING_REPORTED
    assert usage.provider_usage_components is not None
    assert usage.provider_usage_components.components == {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "cache_write_tokens": 60,
        "output_tokens": 100,
    }


def test_anthropic_messages_usage_preserves_unknown_cache_reporting_and_components() -> None:
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
            model="claude-sonnet-5",
            api_surface="messages",
        ),
    )

    assert usage.cache_reporting == CACHE_REPORTING_UNKNOWN
    assert usage.prompt_tokens == 150
    assert usage.completion_tokens == 50
    assert usage.total_tokens == 200
    assert usage.provider_usage_components is not None
    assert usage.provider_usage_components.components == {
        "input_tokens": 100,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 20,
        "output_tokens": 50,
    }


def test_unknown_pricing_is_explicitly_unavailable_or_estimated_never_available() -> None:
    unknown_provider_quote = get_pricing_quote(ProviderModelRef("self-hosted", "gpt-oss-20b"))
    unknown_anthropic_quote = get_pricing_quote(
        ProviderModelRef("anthropic", "claude-unregistered")
    )
    unknown_openai_quote = get_pricing_quote(ProviderModelRef("openai", "gpt-new-snapshot"))

    assert unknown_provider_quote.status == PRICING_UNAVAILABLE
    assert unknown_provider_quote.schedule is None
    assert unknown_provider_quote.reason == "provider_pricing_not_registered"
    assert unknown_anthropic_quote.status == PRICING_UNAVAILABLE
    assert unknown_anthropic_quote.schedule is None
    assert unknown_openai_quote.status == PRICING_ESTIMATED
    assert unknown_openai_quote.schedule is not None
    assert unknown_openai_quote.reason == "openai_default_compatibility_estimate"
    assert PRICING_AVAILABLE not in {
        unknown_provider_quote.status,
        unknown_anthropic_quote.status,
        unknown_openai_quote.status,
    }


def test_pricing_status_aggregation_and_usage_quotes_surface_partial_or_unavailable() -> None:
    unavailable_usage = UsageData(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        provider="self-hosted",
        model="gpt-oss-20b",
    )
    estimated_usage = UsageData(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        provider="openai",
        model="gpt-new-snapshot",
    )

    assert pricing_quote_for_usage(unavailable_usage).status == PRICING_UNAVAILABLE
    assert pricing_quote_for_usage(estimated_usage).status == PRICING_ESTIMATED
    assert (
        aggregate_pricing_statuses([PRICING_AVAILABLE, PRICING_UNAVAILABLE])
        == PRICING_PARTIAL
    )
