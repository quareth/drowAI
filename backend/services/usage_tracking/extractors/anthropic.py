"""Anthropic Messages usage extractor."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

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


class AnthropicMessagesUsageExtractor:
    """Extract usage from Anthropic Messages response objects."""

    def extract(self, response: Any, target: UsageExtractionTarget) -> UsageData:
        """Extract normalized usage and Anthropic billing components."""
        usage = getattr(response, "usage", None)
        api_surface = target.api_surface
        if not usage:
            return UsageData.empty(
                target.model,
                provider=target.provider,
                api_surface=api_surface,
            )

        input_tokens = _usage_token_attr(usage, "input_tokens")
        cache_creation_tokens = _usage_token_attr(
            usage, "cache_creation_input_tokens"
        )
        cache_creation_5m_tokens, cache_creation_1h_tokens = (
            _cache_creation_token_split(usage, cache_creation_tokens)
        )
        cache_read_tokens = _usage_token_attr(usage, "cache_read_input_tokens")
        output_tokens = _usage_token_attr(usage, "output_tokens")
        output_details = _optional_usage_attr(usage, "output_tokens_details")
        reasoning_tokens = _usage_token_attr(output_details, "thinking_tokens")
        prompt_tokens = input_tokens + cache_creation_tokens + cache_read_tokens
        components = {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation_5m_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "output_tokens": output_tokens,
        }
        if cache_creation_1h_tokens > 0:
            components["cache_creation_input_tokens_1h"] = cache_creation_1h_tokens

        return UsageData(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=prompt_tokens + output_tokens,
            model=target.model,
            provider=target.provider,
            cached_tokens=0,
            reasoning_tokens=reasoning_tokens,
            api_surface=api_surface,
            cache_reporting=classify_cache_reporting(
                target.parser_provider or target.provider,
                api_surface,
            ),
            provider_usage_components=ProviderUsageComponents(
                provider=target.provider,
                api_surface=api_surface,
                components=components,
            ),
            usage_attribution=response_usage_attribution(
                response,
                target,
                usage_completeness="actual",
            ),
        )


def _cache_creation_token_split(
    usage: Any,
    total_cache_creation_tokens: int,
) -> tuple[int, int]:
    """Return 5-minute and 1-hour Anthropic cache-write token counts."""
    cache_creation = _optional_usage_attr(usage, "cache_creation")
    has_5m = _has_usage_field(cache_creation, "ephemeral_5m_input_tokens")
    has_1h = _has_usage_field(cache_creation, "ephemeral_1h_input_tokens")
    if not has_5m and not has_1h:
        return total_cache_creation_tokens, 0

    cache_creation_5m_tokens = (
        _usage_token_attr(cache_creation, "ephemeral_5m_input_tokens")
        if has_5m
        else 0
    )
    cache_creation_1h_tokens = (
        _usage_token_attr(cache_creation, "ephemeral_1h_input_tokens")
        if has_1h
        else 0
    )
    return cache_creation_5m_tokens, cache_creation_1h_tokens


def _optional_usage_attr(value: Any, attr: str) -> Any:
    if isinstance(value, dict):
        return value.get(attr)
    if isinstance(value, Mock):
        return value.__dict__.get(attr)
    return getattr(value, attr, None)


def _has_usage_field(value: Any, attr: str) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return attr in value
    if isinstance(value, Mock):
        return attr in value.__dict__
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict) and attr in value_dict:
        return True
    fields_set = getattr(value, "model_fields_set", None)
    return attr in fields_set if fields_set is not None else hasattr(value, attr)


__all__ = ["AnthropicMessagesUsageExtractor"]
