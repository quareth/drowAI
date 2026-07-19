"""Compatibility facade for provider-aware usage pricing.

The public functions in this module keep the historical usage-tracking API
stable while delegating provider/model lookup to ``pricing_registry``. Cost
math is quote-level: callers can still ask for a float cost, but richer paths
also receive explicit pricing status so unavailable pricing is never mistaken
for free usage.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Mapping

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef

from .models import ProviderUsageComponents
from .pricing_registry import (
    CACHED_INPUT_TOKENS,
    INPUT_TOKENS,
    OPENAI_DEFAULT_ESTIMATE,
    OPENAI_PRICE_SCHEDULES,
    OUTPUT_TOKENS,
    PRICING_AVAILABLE,
    PRICING_ESTIMATED,
    PRICING_PARTIAL,
    PRICING_UNAVAILABLE,
    PricingQuote,
    aggregate_pricing_statuses,
    get_pricing_quote,
    has_available_or_estimated_provider_pricing,
)

_OPENAI_LONG_CONTEXT_MODELS = frozenset(
    {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
)
_OPENAI_LONG_CONTEXT_THRESHOLD_TOKENS = 272_000

if TYPE_CHECKING:
    from .models import UsageData


def _legacy_openai_pricing_dict(schedule: Any) -> Dict[str, float]:
    """Convert a component schedule into the legacy OpenAI dict shape."""
    prices = schedule.component_prices_per_million
    return {
        "input_per_million": float(prices[INPUT_TOKENS]),
        "output_per_million": float(prices[OUTPUT_TOKENS]),
        "cached_input_per_million": float(prices[CACHED_INPUT_TOKENS]),
    }


OPENAI_PRICING: Dict[str, Dict[str, float]] = {
    model: _legacy_openai_pricing_dict(schedule)
    for model, schedule in OPENAI_PRICE_SCHEDULES.items()
}
DEFAULT_PRICING: Dict[str, float] = _legacy_openai_pricing_dict(
    OPENAI_DEFAULT_ESTIMATE
)


def has_provider_pricing(provider: str) -> bool:
    """Return whether legacy provider-level pricing can produce a quote."""
    return has_available_or_estimated_provider_pricing(provider)


def pricing_status_for_providers(providers: Any) -> str:
    """Compatibility aggregate status for provider-only legacy callers.

    Provider-only OpenAI checks cannot prove exact model pricing, so they are
    treated as estimates instead of fully available quotes.
    """
    provider_values = [
        str(provider or "").strip().lower()
        for provider in providers
        if str(provider or "").strip()
    ]
    if not provider_values:
        return PRICING_AVAILABLE
    statuses = [
        PRICING_ESTIMATED if has_provider_pricing(provider) else PRICING_UNAVAILABLE
        for provider in provider_values
    ]
    return aggregate_pricing_statuses(statuses)


def get_model_pricing(model: str) -> Dict[str, float]:
    """Return OpenAI-compatible model pricing in the historical dict shape.

    Unknown OpenAI-compatible model names still return ``DEFAULT_PRICING`` for
    compatibility. Use ``pricing_quote_for_usage`` when status matters.
    """
    quote = get_pricing_quote(ProviderModelRef(OPENAI_PROVIDER_ID, model))
    if quote.schedule is None:
        return DEFAULT_PRICING
    return _legacy_openai_pricing_dict(quote.schedule)


def pricing_quote_for_usage(usage: "UsageData") -> PricingQuote:
    """Return the quote used to price a normalized usage object."""
    components = _provider_components_mapping(
        getattr(usage, "provider_usage_components", None)
    )
    provider, model = _pricing_provider_model_for_usage(usage)
    return get_pricing_quote(
        ProviderModelRef(provider, model),
        api_surface=str(getattr(usage, "api_surface", "") or "") or None,
        provider_usage_components=components,
        effective_date=getattr(usage, "pricing_date", None),
    )


def pricing_status_for_usage(usage: "UsageData") -> str:
    """Return the quote status for a normalized usage object."""
    return pricing_quote_for_usage(usage).status


def calculate_cost(usage: "UsageData") -> float:
    """Calculate USD cost for usage while preserving float compatibility."""
    components = calculate_cost_components(
        _pricing_provider_model_for_usage(usage)[1],
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=usage.cached_tokens,
        provider=_pricing_provider_model_for_usage(usage)[0],
        api_surface=getattr(usage, "api_surface", None),
        provider_usage_components=getattr(usage, "provider_usage_components", None),
        effective_date=getattr(usage, "pricing_date", None),
    )
    return (
        components["uncached_input_cost_usd"]
        + components["cached_input_cost_usd"]
        + components["output_cost_usd"]
    )


def calculate_cost_breakdown(usage: "UsageData") -> Dict[str, Any]:
    """Calculate detailed cost breakdown and quote status for usage data."""
    quote = pricing_quote_for_usage(usage)
    components = calculate_cost_components(
        _pricing_provider_model_for_usage(usage)[1],
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=usage.cached_tokens,
        provider=_pricing_provider_model_for_usage(usage)[0],
        api_surface=getattr(usage, "api_surface", None),
        provider_usage_components=getattr(usage, "provider_usage_components", None),
        effective_date=getattr(usage, "pricing_date", None),
    )
    canonical_components = canonical_usage_components(
        provider=_pricing_provider_model_for_usage(usage)[0],
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=usage.cached_tokens,
        provider_usage_components=getattr(usage, "provider_usage_components", None),
    )
    total_cost = (
        components["uncached_input_cost_usd"]
        + components["cached_input_cost_usd"]
        + components["output_cost_usd"]
    )
    return {
        "input_tokens": canonical_components.get(INPUT_TOKENS, 0),
        "cached_tokens": canonical_components.get(CACHED_INPUT_TOKENS, 0),
        "output_tokens": canonical_components.get(OUTPUT_TOKENS, 0),
        "input_cost": components["uncached_input_cost_usd"],
        "cached_cost": components["cached_input_cost_usd"],
        "output_cost": components["output_cost_usd"],
        "total_cost": total_cost,
        "pricing_used": (
            _legacy_openai_pricing_dict(quote.schedule)
            if quote.schedule is not None and quote.provider == OPENAI_PROVIDER_ID
            else None
        ),
        "pricing_status": quote.status,
        "pricing_reason": quote.reason,
        "pricing_revision": quote.pricing_revision,
        "model": usage.model,
        "provider": usage.provider,
    }


def calculate_cost_components(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    provider: str = OPENAI_PROVIDER_ID,
    *,
    api_surface: str | None = None,
    provider_usage_components: ProviderUsageComponents | Mapping[str, Any] | None = None,
    effective_date: date | datetime | None = None,
) -> Dict[str, float]:
    """Return the per-row input/cached/output USD cost split for usage."""
    quote = get_pricing_quote(
        ProviderModelRef(str(provider or OPENAI_PROVIDER_ID), str(model or "unknown")),
        api_surface=api_surface,
        provider_usage_components=_provider_components_mapping(provider_usage_components),
        effective_date=effective_date,
    )
    if quote.schedule is None:
        return {
            "uncached_input_cost_usd": 0.0,
            "cached_input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
        }

    components = canonical_usage_components(
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        provider_usage_components=provider_usage_components,
    )
    uncached_cost = 0.0
    cached_cost = 0.0
    output_cost = 0.0
    input_multiplier, output_multiplier = _long_context_multipliers(
        provider=quote.provider,
        model=quote.model,
        prompt_tokens=prompt_tokens,
    )
    for component, tokens in components.items():
        component_cost = _component_cost(quote, component, tokens)
        category = _component_cost_category(component)
        if category == "output":
            output_cost += component_cost * output_multiplier
        elif category == "cached_input":
            cached_cost += component_cost * input_multiplier
        else:
            uncached_cost += component_cost * input_multiplier
    return {
        "uncached_input_cost_usd": uncached_cost,
        "cached_input_cost_usd": cached_cost,
        "output_cost_usd": output_cost,
    }


def canonical_usage_components(
    *,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    provider_usage_components: ProviderUsageComponents | Mapping[str, Any] | None = None,
) -> Dict[str, int]:
    """Normalize usage into canonical price component token counts."""
    provider_id = str(provider or OPENAI_PROVIDER_ID).strip().lower() or OPENAI_PROVIDER_ID
    component_mapping = _provider_components_mapping(provider_usage_components)
    if component_mapping is not None:
        return {
            str(key): _safe_int(value)
            for key, value in component_mapping.items()
        }

    safe_prompt = max(0, int(prompt_tokens or 0))
    safe_completion = max(0, int(completion_tokens or 0))
    safe_cached = max(0, min(int(cached_tokens or 0), safe_prompt))
    return {
        INPUT_TOKENS: safe_prompt - safe_cached,
        CACHED_INPUT_TOKENS: safe_cached,
        OUTPUT_TOKENS: safe_completion,
    }


def usage_from_persisted_record(record: Any) -> "UsageData":
    """Rebuild ``UsageData`` from a persisted usage row-like object."""
    from .models import UsageData

    metadata = getattr(record, "request_metadata", None)
    metadata_dict = metadata if isinstance(metadata, Mapping) else {}
    provider_components = ProviderUsageComponents.from_mapping(
        metadata_dict.get("provider_usage_components")
    )
    from .models import UsageAttributionContext

    usage_attribution = UsageAttributionContext.from_mapping(
        metadata_dict.get("usage_attribution")
    )
    api_surface = str(metadata_dict.get("api_surface") or "").strip().lower()
    if api_surface in {"", "unknown"} and provider_components is not None:
        api_surface = provider_components.api_surface
    created_at = getattr(record, "created_at", None)
    pricing_date = (
        created_at.date()
        if isinstance(created_at, datetime)
        else created_at
        if isinstance(created_at, date)
        else None
    )
    return UsageData(
        prompt_tokens=int(getattr(record, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(record, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(record, "total_tokens", 0) or 0),
        model=str(getattr(record, "model", None) or "unknown"),
        provider=str(getattr(record, "provider", None) or OPENAI_PROVIDER_ID),
        cached_tokens=int(getattr(record, "cached_tokens", 0) or 0),
        reasoning_tokens=int(getattr(record, "reasoning_tokens", 0) or 0),
        api_surface=api_surface or "unknown",
        provider_usage_components=provider_components,
        pricing_date=pricing_date,
        usage_attribution=usage_attribution,
    )


def _pricing_provider_model_for_usage(usage: "UsageData") -> tuple[str, str]:
    attribution = getattr(usage, "usage_attribution", None)
    billing_provider = getattr(attribution, "billing_provider_id", None)
    canonical_model = getattr(attribution, "canonical_model_id", None)
    requested_model = getattr(attribution, "requested_model_id", None)
    provider = str(
        billing_provider
        or getattr(usage, "provider", OPENAI_PROVIDER_ID)
        or OPENAI_PROVIDER_ID
    )
    model = str(
        canonical_model
        or requested_model
        or getattr(usage, "model", "unknown")
        or "unknown"
    )
    return provider, model


def _component_cost(quote: PricingQuote, component: str, tokens: int) -> float:
    if quote.schedule is None:
        return 0.0
    price = quote.schedule.price_for(component)
    if price is None:
        return 0.0
    cost = (Decimal(max(0, int(tokens))) / Decimal(1_000_000)) * price
    return float(cost)


def _component_cost_category(component: str) -> str:
    """Map provider component names into the legacy cost split buckets."""
    component_name = str(component or "").strip().lower()
    if component_name == OUTPUT_TOKENS or component_name.endswith("_output_tokens"):
        return "output"
    if component_name in {CACHED_INPUT_TOKENS, "cache_read_input_tokens"}:
        return "cached_input"
    if component_name.startswith("cached_") or "cache_read" in component_name:
        return "cached_input"
    return "uncached_input"


def _long_context_multipliers(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
) -> tuple[float, float]:
    """Return documented GPT-5.6 input/output multipliers for long requests."""
    if (
        provider == OPENAI_PROVIDER_ID
        and model in _OPENAI_LONG_CONTEXT_MODELS
        and max(0, int(prompt_tokens or 0)) > _OPENAI_LONG_CONTEXT_THRESHOLD_TOKENS
    ):
        return 2.0, 1.5
    return 1.0, 1.0


def _provider_components_mapping(
    value: ProviderUsageComponents | Mapping[str, Any] | None,
) -> Dict[str, int] | None:
    components = ProviderUsageComponents.from_mapping(value)
    if components is not None:
        return dict(components.components)
    if isinstance(value, Mapping):
        raw_components = value.get("components")
        if isinstance(raw_components, Mapping):
            return {
                str(key): _safe_int(component_value)
                for key, component_value in raw_components.items()
            }
        return {str(key): _safe_int(component_value) for key, component_value in value.items()}
    return None


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DEFAULT_PRICING",
    "OPENAI_PRICING",
    "OPENAI_PROVIDER_ID",
    "PRICING_AVAILABLE",
    "PRICING_ESTIMATED",
    "PRICING_PARTIAL",
    "PRICING_UNAVAILABLE",
    "aggregate_pricing_statuses",
    "calculate_cost",
    "calculate_cost_breakdown",
    "calculate_cost_components",
    "canonical_usage_components",
    "get_model_pricing",
    "get_pricing_quote",
    "pricing_quote_for_usage",
    "pricing_status_for_usage",
    "usage_from_persisted_record",
]
