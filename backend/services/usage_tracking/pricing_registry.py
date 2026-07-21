"""Provider/model keyed pricing registry and quote contracts.

This module owns provider-aware price lookup. It deliberately returns an
explicit quote status for every lookup so callers can distinguish verified
costs, compatibility estimates, and unavailable pricing instead of treating a
zero dollar cost as free usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Literal, Mapping

from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    normalize_model_id,
    normalize_provider_id,
)

PricingStatus = Literal["available", "unavailable", "partial", "estimated"]

PRICING_AVAILABLE: PricingStatus = "available"
PRICING_UNAVAILABLE: PricingStatus = "unavailable"
PRICING_PARTIAL: PricingStatus = "partial"
PRICING_ESTIMATED: PricingStatus = "estimated"

INPUT_TOKENS = "input_tokens"
CACHED_INPUT_TOKENS = "cached_input_tokens"
CACHE_WRITE_TOKENS = "cache_write_tokens"
OUTPUT_TOKENS = "output_tokens"
ANTHROPIC_CACHE_CREATION_INPUT_TOKENS = "cache_creation_input_tokens"
ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H = "cache_creation_input_tokens_1h"
ANTHROPIC_CACHE_READ_INPUT_TOKENS = "cache_read_input_tokens"
OPENAI_PRICING_REVISION = "openai_text_pricing_v1"
OPENAI_ESTIMATE_PRICING_REVISION = "openai_text_estimate_v1"
ANTHROPIC_PRICING_REVISION = "anthropic_text_pricing_v1"


@dataclass(frozen=True, slots=True)
class ComponentPriceSchedule:
    """USD-per-million-token prices keyed by canonical usage component."""

    component_prices_per_million: Mapping[str, Decimal]

    def price_for(self, component: str) -> Decimal | None:
        """Return a component price when this schedule can price it."""
        return self.component_prices_per_million.get(component)


@dataclass(frozen=True, slots=True)
class PricingQuote:
    """Quote-level pricing availability for one provider/model request."""

    provider: str
    model: str
    status: PricingStatus
    schedule: ComponentPriceSchedule | None
    api_surface: str | None = None
    reason: str | None = None
    pricing_revision: str | None = None


def _schedule(
    *,
    input_per_million: str,
    cached_input_per_million: str,
    output_per_million: str,
    cache_write_per_million: str | None = None,
) -> ComponentPriceSchedule:
    prices = {
        INPUT_TOKENS: Decimal(input_per_million),
        CACHED_INPUT_TOKENS: Decimal(cached_input_per_million),
        OUTPUT_TOKENS: Decimal(output_per_million),
    }
    if cache_write_per_million is not None:
        prices[CACHE_WRITE_TOKENS] = Decimal(cache_write_per_million)
    return ComponentPriceSchedule(component_prices_per_million=prices)


# OpenAI text-token standard tier pricing, USD per 1M tokens. Pro models do not
# publish cached-input discounts, so cached input is priced at the normal input
# rate to avoid undercounting any noisy cached-token rows.
OPENAI_PRICE_SCHEDULES: dict[str, ComponentPriceSchedule] = {
    "gpt-4o": _schedule(
        input_per_million="2.50",
        cached_input_per_million="1.25",
        output_per_million="10.00",
    ),
    "gpt-4o-2024-11-20": _schedule(
        input_per_million="2.50",
        cached_input_per_million="1.25",
        output_per_million="10.00",
    ),
    "gpt-4o-2024-08-06": _schedule(
        input_per_million="2.50",
        cached_input_per_million="1.25",
        output_per_million="10.00",
    ),
    "gpt-4o-mini": _schedule(
        input_per_million="0.15",
        cached_input_per_million="0.075",
        output_per_million="0.60",
    ),
    "gpt-4o-mini-2024-07-18": _schedule(
        input_per_million="0.15",
        cached_input_per_million="0.075",
        output_per_million="0.60",
    ),
    "gpt-5": _schedule(
        input_per_million="1.25",
        cached_input_per_million="0.125",
        output_per_million="10.00",
    ),
    "gpt-5-mini": _schedule(
        input_per_million="0.25",
        cached_input_per_million="0.025",
        output_per_million="2.00",
    ),
    "gpt-5-nano": _schedule(
        input_per_million="0.05",
        cached_input_per_million="0.005",
        output_per_million="0.40",
    ),
    "gpt-5.1": _schedule(
        input_per_million="1.25",
        cached_input_per_million="0.125",
        output_per_million="10.00",
    ),
    "gpt-5.2": _schedule(
        input_per_million="1.75",
        cached_input_per_million="0.175",
        output_per_million="14.00",
    ),
    "gpt-5.4": _schedule(
        input_per_million="2.50",
        cached_input_per_million="0.25",
        output_per_million="15.00",
    ),
    "gpt-5.4-mini": _schedule(
        input_per_million="0.75",
        cached_input_per_million="0.075",
        output_per_million="4.50",
    ),
    "gpt-5.4-nano": _schedule(
        input_per_million="0.20",
        cached_input_per_million="0.02",
        output_per_million="1.25",
    ),
    "gpt-5.5": _schedule(
        input_per_million="5.00",
        cached_input_per_million="0.50",
        output_per_million="30.00",
    ),
    "gpt-5-pro": _schedule(
        input_per_million="15.00",
        cached_input_per_million="15.00",
        output_per_million="120.00",
    ),
    "gpt-5.2-pro": _schedule(
        input_per_million="21.00",
        cached_input_per_million="21.00",
        output_per_million="168.00",
    ),
    "gpt-5.6": _schedule(
        input_per_million="5.00",
        cached_input_per_million="0.50",
        cache_write_per_million="6.25",
        output_per_million="30.00",
    ),
    "gpt-5.6-sol": _schedule(
        input_per_million="5.00",
        cached_input_per_million="0.50",
        cache_write_per_million="6.25",
        output_per_million="30.00",
    ),
    "gpt-5.6-terra": _schedule(
        input_per_million="2.50",
        cached_input_per_million="0.25",
        cache_write_per_million="3.125",
        output_per_million="15.00",
    ),
    "gpt-5.6-luna": _schedule(
        input_per_million="1.00",
        cached_input_per_million="0.10",
        cache_write_per_million="1.25",
        output_per_million="6.00",
    ),
    "gpt-4-turbo": _schedule(
        input_per_million="10.00",
        cached_input_per_million="10.00",
        output_per_million="30.00",
    ),
    "gpt-4-turbo-2024-04-09": _schedule(
        input_per_million="10.00",
        cached_input_per_million="10.00",
        output_per_million="30.00",
    ),
    "gpt-4": _schedule(
        input_per_million="30.00",
        cached_input_per_million="30.00",
        output_per_million="60.00",
    ),
    "gpt-4-0613": _schedule(
        input_per_million="30.00",
        cached_input_per_million="30.00",
        output_per_million="60.00",
    ),
    "gpt-3.5-turbo": _schedule(
        input_per_million="0.50",
        cached_input_per_million="0.50",
        output_per_million="1.50",
    ),
    "gpt-3.5-turbo-0125": _schedule(
        input_per_million="0.50",
        cached_input_per_million="0.50",
        output_per_million="1.50",
    ),
}

OPENAI_DEFAULT_ESTIMATE = _schedule(
    input_per_million="2.50",
    cached_input_per_million="1.25",
    output_per_million="10.00",
)


ANTHROPIC_SONNET5_INTRO_PRICE_SCHEDULE = ComponentPriceSchedule(
    component_prices_per_million={
        INPUT_TOKENS: Decimal("2.00"),
        ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("2.50"),
        ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("4.00"),
        ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.20"),
        OUTPUT_TOKENS: Decimal("10.00"),
    }
)
ANTHROPIC_SONNET5_STANDARD_PRICE_SCHEDULE = ComponentPriceSchedule(
    component_prices_per_million={
        INPUT_TOKENS: Decimal("3.00"),
        ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("3.75"),
        ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("6.00"),
        ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.30"),
        OUTPUT_TOKENS: Decimal("15.00"),
    }
)
ANTHROPIC_SONNET5_STANDARD_PRICE_START = date(2026, 9, 1)


ANTHROPIC_PRICE_SCHEDULES: dict[str, ComponentPriceSchedule] = {
    "claude-fable-5": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("10.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("12.50"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("20.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("1.00"),
            OUTPUT_TOKENS: Decimal("50.00"),
        }
    ),
    "claude-mythos-5": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("10.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("12.50"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("20.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("1.00"),
            OUTPUT_TOKENS: Decimal("50.00"),
        }
    ),
    "claude-sonnet-5": ANTHROPIC_SONNET5_STANDARD_PRICE_SCHEDULE,
    "claude-opus-4-8": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("5.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("6.25"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("10.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.50"),
            OUTPUT_TOKENS: Decimal("25.00"),
        }
    ),
    "claude-opus-4-7": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("5.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("6.25"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("10.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.50"),
            OUTPUT_TOKENS: Decimal("25.00"),
        }
    ),
    "claude-opus-4-6": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("5.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("6.25"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("10.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.50"),
            OUTPUT_TOKENS: Decimal("25.00"),
        }
    ),
    "claude-sonnet-4-6": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("3.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("3.75"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("6.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.30"),
            OUTPUT_TOKENS: Decimal("15.00"),
        }
    ),
    "claude-haiku-4-5-20251001": ComponentPriceSchedule(
        component_prices_per_million={
            INPUT_TOKENS: Decimal("1.00"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: Decimal("1.25"),
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: Decimal("2.00"),
            ANTHROPIC_CACHE_READ_INPUT_TOKENS: Decimal("0.10"),
            OUTPUT_TOKENS: Decimal("5.00"),
        }
    ),
}


def get_pricing_quote(
    ref: ProviderModelRef,
    *,
    api_surface: str | None = None,
    provider_usage_components: Mapping[str, int] | None = None,
    effective_date: date | datetime | None = None,
) -> PricingQuote:
    """Return a quote for one explicit provider/model/API-surface tuple."""
    normalized = ref.normalized()
    provider = normalized.provider
    model = normalized.model
    surface = str(api_surface or "").strip().lower() or None

    if provider == OPENAI_PROVIDER_ID:
        if model == "gpt-oss-20b":
            return PricingQuote(
                provider=provider,
                model=model,
                status=PRICING_UNAVAILABLE,
                schedule=None,
                api_surface=surface,
                reason="openai_gpt_oss_pricing_not_registered",
                pricing_revision=None,
            )
        schedule = _resolve_openai_exact_schedule(model)
        if schedule is not None:
            return PricingQuote(
                provider=provider,
                model=model,
                status=PRICING_AVAILABLE,
                schedule=schedule,
                api_surface=surface,
                pricing_revision=OPENAI_PRICING_REVISION,
            )
        schedule = _resolve_openai_snapshot_estimate_schedule(model)
        if schedule is not None:
            return PricingQuote(
                provider=provider,
                model=model,
                status=PRICING_ESTIMATED,
                schedule=schedule,
                api_surface=surface,
                reason="openai_snapshot_compatibility_estimate",
                pricing_revision=OPENAI_ESTIMATE_PRICING_REVISION,
            )
        return PricingQuote(
            provider=provider,
            model=model,
            status=PRICING_ESTIMATED,
            schedule=OPENAI_DEFAULT_ESTIMATE,
            api_surface=surface,
            reason="openai_default_compatibility_estimate",
            pricing_revision=OPENAI_ESTIMATE_PRICING_REVISION,
        )

    if provider == ANTHROPIC_PROVIDER_ID:
        schedule = _resolve_anthropic_exact_schedule(
            model,
            effective_date=effective_date,
        )
        if schedule is not None:
            return PricingQuote(
                provider=provider,
                model=model,
                status=PRICING_AVAILABLE,
                schedule=schedule,
                api_surface=surface,
                pricing_revision=ANTHROPIC_PRICING_REVISION,
            )
        return PricingQuote(
            provider=provider,
            model=model,
            status=PRICING_UNAVAILABLE,
            schedule=None,
            api_surface=surface,
            reason="anthropic_model_pricing_not_registered",
            pricing_revision=None,
        )

    return PricingQuote(
        provider=provider,
        model=model,
        status=PRICING_UNAVAILABLE,
        schedule=None,
        api_surface=surface,
        reason="provider_pricing_not_registered",
        pricing_revision=None,
    )


def has_available_or_estimated_provider_pricing(provider: str) -> bool:
    """Compatibility provider-level check for legacy callers."""
    try:
        normalized = normalize_provider_id(provider)
    except (TypeError, ValueError):
        return False
    return normalized in {OPENAI_PROVIDER_ID, ANTHROPIC_PROVIDER_ID}


def aggregate_pricing_statuses(statuses: Mapping[str, int] | list[str] | tuple[str, ...]) -> PricingStatus:
    """Aggregate row-level pricing statuses into one response status."""
    if isinstance(statuses, Mapping):
        values = [status for status, count in statuses.items() if int(count or 0) > 0]
    else:
        values = [str(status) for status in statuses if str(status)]
    if not values:
        return PRICING_AVAILABLE
    unique = set(values)
    if unique == {PRICING_AVAILABLE}:
        return PRICING_AVAILABLE
    if unique == {PRICING_ESTIMATED}:
        return PRICING_ESTIMATED
    if unique == {PRICING_UNAVAILABLE}:
        return PRICING_UNAVAILABLE
    if unique == {PRICING_PARTIAL}:
        return PRICING_PARTIAL
    if PRICING_UNAVAILABLE in unique or PRICING_PARTIAL in unique:
        return PRICING_PARTIAL
    if PRICING_ESTIMATED in unique:
        return PRICING_ESTIMATED
    return PRICING_PARTIAL


def _resolve_openai_exact_schedule(model: str) -> ComponentPriceSchedule | None:
    """Resolve only explicitly registered OpenAI pricing entries."""
    normalized_model = normalize_model_id(model)
    return OPENAI_PRICE_SCHEDULES.get(normalized_model)


def _resolve_openai_snapshot_estimate_schedule(model: str) -> ComponentPriceSchedule | None:
    """Resolve a base-model estimate for unregistered dated OpenAI snapshots."""
    normalized_model = normalize_model_id(model)
    for known_model in sorted(OPENAI_PRICE_SCHEDULES, key=len, reverse=True):
        if _is_snapshot_for_model(normalized_model, known_model):
            return OPENAI_PRICE_SCHEDULES[known_model]
    return None


def _resolve_anthropic_exact_schedule(
    model: str,
    *,
    effective_date: date | datetime | None = None,
) -> ComponentPriceSchedule | None:
    """Resolve only explicitly registered Anthropic pricing entries."""
    normalized_model = normalize_model_id(model)
    if normalized_model == "claude-sonnet-5":
        if _coerce_pricing_date(effective_date) < ANTHROPIC_SONNET5_STANDARD_PRICE_START:
            return ANTHROPIC_SONNET5_INTRO_PRICE_SCHEDULE
        return ANTHROPIC_SONNET5_STANDARD_PRICE_SCHEDULE
    return ANTHROPIC_PRICE_SCHEDULES.get(normalized_model)


def _utc_today() -> date:
    """Return the UTC pricing date for scheduled price transitions."""
    return datetime.now(timezone.utc).date()


def _coerce_pricing_date(value: date | datetime | None) -> date:
    """Return an explicit usage date or the current UTC date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _utc_today()


def _is_snapshot_for_model(model: str, base_model: str) -> bool:
    """Return True only for dated snapshots of an exact priced model."""
    suffix_prefix = f"{base_model}-"
    if not model.startswith(suffix_prefix):
        return False
    suffix = model[len(suffix_prefix) :]
    return re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-.+)?", suffix) is not None


__all__ = [
    "CACHED_INPUT_TOKENS",
    "ANTHROPIC_CACHE_CREATION_INPUT_TOKENS",
    "ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H",
    "ANTHROPIC_CACHE_READ_INPUT_TOKENS",
    "ANTHROPIC_PRICE_SCHEDULES",
    "ANTHROPIC_SONNET5_INTRO_PRICE_SCHEDULE",
    "ANTHROPIC_SONNET5_STANDARD_PRICE_SCHEDULE",
    "ANTHROPIC_SONNET5_STANDARD_PRICE_START",
    "ComponentPriceSchedule",
    "INPUT_TOKENS",
    "OPENAI_DEFAULT_ESTIMATE",
    "OPENAI_PRICE_SCHEDULES",
    "OPENAI_PRICING_REVISION",
    "OUTPUT_TOKENS",
    "PRICING_AVAILABLE",
    "PRICING_ESTIMATED",
    "PRICING_PARTIAL",
    "PRICING_UNAVAILABLE",
    "PricingQuote",
    "PricingStatus",
    "aggregate_pricing_statuses",
    "get_pricing_quote",
    "has_available_or_estimated_provider_pricing",
]
