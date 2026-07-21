"""OpenAI usage extractors for Chat Completions and Responses API."""

from __future__ import annotations

from typing import Any

from backend.services.usage_tracking.extraction import (
    UsageExtractionTarget,
    response_usage_attribution,
)
from backend.services.usage_tracking.models import (
    ProviderUsageComponents,
    UsageData,
    _usage_token_attr,
    classify_cache_reporting,
)


class OpenAIChatCompletionsUsageExtractor:
    """Extract usage from OpenAI Chat Completions response objects."""

    def extract(self, response: Any, target: UsageExtractionTarget) -> UsageData:
        """Extract normalized usage from a Chat Completions response."""
        usage = getattr(response, "usage", None)
        if not usage:
            return UsageData.empty(
                target.model,
                provider=target.provider,
                api_surface=target.api_surface,
            )

        parser_provider = target.parser_provider or target.provider
        api_surface = target.api_surface
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _usage_token_attr(details, "cached_tokens") if details else 0
        cache_write_tokens = (
            _usage_token_attr(details, "cache_write_tokens") if details else 0
        )
        prompt = _usage_token_attr(usage, "prompt_tokens")
        completion = _usage_token_attr(usage, "completion_tokens")
        total = _usage_token_attr(usage, "total_tokens")
        if total < prompt + completion:
            total = prompt + completion

        provider_usage_components = None
        if cache_write_tokens > 0:
            provider_usage_components = ProviderUsageComponents(
                provider=target.provider,
                api_surface=api_surface,
                components={
                    "input_tokens": max(0, prompt - cached - cache_write_tokens),
                    "cached_input_tokens": cached,
                    "cache_write_tokens": cache_write_tokens,
                    "output_tokens": completion,
                },
            )

        return UsageData(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            model=target.model,
            provider=target.provider,
            cached_tokens=cached,
            reasoning_tokens=0,
            api_surface=api_surface,
            cache_reporting=classify_cache_reporting(parser_provider, api_surface),
            provider_usage_components=provider_usage_components,
            usage_attribution=response_usage_attribution(
                response,
                target,
                usage_completeness="actual",
            ),
        )


class OpenAIResponsesUsageExtractor:
    """Extract usage from OpenAI Responses API response objects."""

    def extract(self, response: Any, target: UsageExtractionTarget) -> UsageData:
        """Extract normalized usage from a Responses API response."""
        usage = getattr(response, "usage", None)
        if not usage:
            return UsageData.empty(
                target.model,
                provider=target.provider,
                api_surface=target.api_surface,
            )

        parser_provider = target.parser_provider or target.provider
        api_surface = target.api_surface
        input_tokens = _usage_token_attr(usage, "input_tokens")
        output_tokens = _usage_token_attr(usage, "output_tokens")
        total_tokens = _usage_token_attr(usage, "total_tokens")

        input_details = getattr(usage, "input_tokens_details", None)
        cached_tokens = (
            _usage_token_attr(input_details, "cached_tokens")
            if input_details is not None
            else 0
        )
        cache_write_tokens = (
            _usage_token_attr(input_details, "cache_write_tokens")
            if input_details is not None
            else 0
        )

        output_details = getattr(usage, "output_tokens_details", None)
        reasoning = (
            _usage_token_attr(output_details, "reasoning_tokens")
            if output_details is not None
            else 0
        )
        if reasoning == 0:
            reasoning = _usage_token_attr(usage, "reasoning_tokens")

        if total_tokens < input_tokens + output_tokens:
            total_tokens = input_tokens + output_tokens

        provider_usage_components = None
        if cache_write_tokens > 0:
            provider_usage_components = ProviderUsageComponents(
                provider=target.provider,
                api_surface=api_surface,
                components={
                    "input_tokens": max(
                        0,
                        input_tokens - cached_tokens - cache_write_tokens,
                    ),
                    "cached_input_tokens": cached_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "output_tokens": output_tokens,
                },
            )

        return UsageData(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
            model=target.model,
            provider=target.provider,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning,
            api_surface=api_surface,
            cache_reporting=classify_cache_reporting(parser_provider, api_surface),
            provider_usage_components=provider_usage_components,
            usage_attribution=response_usage_attribution(
                response,
                target,
                usage_completeness="actual",
            ),
        )


__all__ = [
    "OpenAIChatCompletionsUsageExtractor",
    "OpenAIResponsesUsageExtractor",
]
