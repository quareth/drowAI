"""Tests for pricing configuration and cost calculation.

These tests verify that cost calculations are accurate for different
models and token configurations.
"""

import pytest
from datetime import date, datetime
from decimal import Decimal

from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
)
from agent.providers.llm.profiles import list_catalog_model_profiles
from backend.services.usage_tracking.models import ProviderUsageComponents, UsageData
import backend.services.usage_tracking.pricing as pricing_module
from backend.services.usage_tracking.pricing import (
    PRICING_AVAILABLE,
    PRICING_ESTIMATED,
    calculate_cost,
    calculate_cost_breakdown,
    calculate_cost_components,
    get_model_pricing,
    pricing_quote_for_usage,
    pricing_status_for_providers,
    usage_from_persisted_record,
    OPENAI_PRICING,
    DEFAULT_PRICING,
)
from backend.services.usage_tracking.pricing_registry import (
    ANTHROPIC_CACHE_CREATION_INPUT_TOKENS,
    ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H,
    ANTHROPIC_CACHE_READ_INPUT_TOKENS,
    ComponentPriceSchedule,
    CACHE_WRITE_TOKENS,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    PricingQuote,
    get_pricing_quote,
)


class TestGetModelPricing:
    """Tests for get_model_pricing()"""
    
    def test_exact_match(self):
        """Should return pricing for exact model match."""
        pricing = get_model_pricing("gpt-4o-mini")
        
        assert pricing["input_per_million"] == 0.15
        assert pricing["output_per_million"] == 0.60
        assert pricing["cached_input_per_million"] == 0.075


class TestPricingStatusForProviders:
    """Tests for aggregate provider pricing status."""

    def test_provider_only_openai_is_estimated(self):
        """Provider-only checks cannot claim exact model pricing is available."""
        assert pricing_status_for_providers(["openai"]) == "estimated"

    def test_provider_only_anthropic_is_estimated(self):
        assert pricing_status_for_providers(["anthropic"]) == "estimated"

    def test_openai_and_anthropic_provider_only_is_estimated(self):
        assert pricing_status_for_providers(["openai", "anthropic"]) == "estimated"


class TestUsageFromPersistedRecord:
    """Tests for rebuilding UsageData from persisted rows."""

    def test_api_surface_falls_back_to_provider_components(self):
        record = type(
            "Record",
            (),
            {
                "prompt_tokens": 150,
                "completion_tokens": 50,
                "total_tokens": 200,
                "model": "claude-sonnet-4-5",
                "provider": "anthropic",
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "request_metadata": {
                    "api_surface": "unknown",
                    "provider_usage_components": {
                        "provider": "anthropic",
                        "api_surface": "messages",
                        "components": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 30,
                            "cache_read_input_tokens": 20,
                            "output_tokens": 50,
                        },
                    },
                },
            },
        )()

        usage = usage_from_persisted_record(record)

        assert usage.api_surface == "messages"
        assert usage.provider_usage_components is not None
        assert usage.provider_usage_components.api_surface == "messages"

    def test_preserves_created_at_as_historical_pricing_date(self):
        record = type(
            "Record",
            (),
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "model": "claude-sonnet-5",
                "provider": "anthropic",
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "request_metadata": {"api_surface": "messages"},
                "created_at": datetime(2026, 8, 31, 23, 59),
            },
        )()

        usage = usage_from_persisted_record(record)

        assert usage.pricing_date == date(2026, 8, 31)
        assert calculate_cost(usage) == pytest.approx(0.000012)


class TestGetModelPricingCompatibility:
    """Tests for model pricing lookup compatibility behavior."""
    
    def test_versioned_model_match(self):
        """Should match versioned model names."""
        pricing = get_model_pricing("gpt-4o-mini-2024-07-18")
        
        assert pricing["input_per_million"] == 0.15
        assert pricing["output_per_million"] == 0.60
    
    def test_unknown_model_returns_default(self):
        """Compatibility lookup still returns default pricing for unknown models."""
        pricing = get_model_pricing("unknown-model-xyz")
        
        assert pricing == DEFAULT_PRICING
    
    def test_gpt5_pricing(self):
        """Should return correct pricing for GPT-5 models."""
        pricing = get_model_pricing("gpt-5")
        
        assert pricing["input_per_million"] == 1.25
        assert pricing["cached_input_per_million"] == 0.125
        assert pricing["output_per_million"] == 10.00
    
    def test_gpt5_pro_pricing(self):
        """Should return correct pricing for GPT-5-pro."""
        pricing = get_model_pricing("gpt-5-pro")
        
        assert pricing["input_per_million"] == 15.00
        assert pricing["cached_input_per_million"] == 15.00
        assert pricing["output_per_million"] == 120.00


class TestCalculateCost:
    """Tests for calculate_cost()"""
    
    def test_basic_cost_calculation(self):
        """Should calculate cost correctly for basic usage."""
        usage = UsageData(
            prompt_tokens=1_000_000,  # 1M input tokens
            completion_tokens=500_000,  # 500K output tokens
            total_tokens=1_500_000,
            model="gpt-4o-mini",
        )
        
        cost = calculate_cost(usage)
        
        # Expected: $0.15 (input) + $0.30 (output) = $0.45
        assert cost == pytest.approx(0.45, rel=1e-6)
    
    def test_cost_with_cached_tokens(self):
        """Should apply cached token discount."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=0,
            total_tokens=1_000_000,
            model="gpt-4o-mini",
            cached_tokens=500_000,  # Half cached
        )
        
        cost = calculate_cost(usage)
        
        # Expected: 500K uncached @ $0.15/M = $0.075
        #           500K cached @ $0.075/M = $0.0375
        # Total: $0.1125
        assert cost == pytest.approx(0.1125, rel=1e-6)
    
    def test_gpt4o_cost_calculation(self):
        """Should calculate cost correctly for gpt-4o."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="gpt-4o",
        )
        
        cost = calculate_cost(usage)
        
        # Expected: $2.50 (input) + $10.00 (output) = $12.50
        assert cost == pytest.approx(12.50, rel=1e-6)
    
    def test_gpt5_cost_calculation(self):
        """Should calculate cost correctly for gpt-5."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="gpt-5",
        )
        
        cost = calculate_cost(usage)
        
        # Expected: $1.25 (input) + $10.00 (output) = $11.25
        assert cost == pytest.approx(11.25, rel=1e-6)

    @pytest.mark.parametrize(
        ("model", "expected_cost"),
        [
            ("gpt-5.4", 17.50),
            ("gpt-5.4-mini", 5.25),
            ("gpt-5.4-nano", 1.45),
            ("gpt-5.5", 35.00),
        ],
    )
    def test_new_visible_openai_models_use_exact_pricing(self, model, expected_cost):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model=model,
            provider="openai",
        )

        quote = pricing_quote_for_usage(usage)

        assert quote.status == PRICING_AVAILABLE
        assert quote.reason is None
        assert calculate_cost(usage) == pytest.approx(expected_cost, rel=1e-6)
    
    def test_zero_tokens_zero_cost(self):
        """Should return zero cost for zero tokens."""
        usage = UsageData.empty("gpt-4o")
        
        cost = calculate_cost(usage)
        
        assert cost == 0.0
    
    def test_small_token_count(self):
        """Should handle small token counts accurately."""
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        
        cost = calculate_cost(usage)
        
        # Expected: (100/1M * $0.15) + (50/1M * $0.60)
        # = $0.000015 + $0.00003 = $0.000045
        assert cost == pytest.approx(0.000045, rel=1e-6)
    
    def test_unknown_model_uses_default(self):
        """Should use explicit estimated default pricing for unknown OpenAI models."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="unknown-model",
        )
        
        cost = calculate_cost(usage)
        
        # Default: $2.50 (input) + $10.00 (output) = $12.50
        assert cost == pytest.approx(12.50, rel=1e-6)
        assert pricing_quote_for_usage(usage).status == PRICING_ESTIMATED

    def test_gpt5_nano_uses_exact_pricing_not_gpt5_prefix(self):
        """gpt-5-nano must not inherit the parent gpt-5 price schedule."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="gpt-5-nano",
            provider="openai",
        )

        assert pricing_quote_for_usage(usage).status == PRICING_AVAILABLE
        assert calculate_cost(usage) == pytest.approx(0.45, rel=1e-6)

    def test_unregistered_snapshot_uses_estimated_family_pricing(self):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="gpt-5-nano-2025-08-07",
            provider="openai",
        )

        quote = pricing_quote_for_usage(usage)

        assert quote.status == PRICING_ESTIMATED
        assert quote.reason == "openai_snapshot_compatibility_estimate"
        assert calculate_cost(usage) == pytest.approx(0.45, rel=1e-6)

    def test_registered_snapshot_uses_available_verified_pricing(self):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="gpt-4o-2024-11-20",
            provider="openai",
        )

        assert pricing_quote_for_usage(usage).status == PRICING_AVAILABLE
        assert calculate_cost(usage) == pytest.approx(12.50, rel=1e-6)

    def test_unregistered_snapshot_is_not_marked_available(self):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="gpt-4o-2024-05-13",
            provider="openai",
        )

        quote = pricing_quote_for_usage(usage)

        assert quote.status == PRICING_ESTIMATED
        assert quote.reason == "openai_snapshot_compatibility_estimate"

    def test_known_anthropic_provider_uses_registered_pricing(self):
        """Anthropic rows use exact Anthropic pricing, not OpenAI defaults."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="claude-sonnet-4-6",
            provider="anthropic",
            api_surface="messages",
        )

        assert pricing_quote_for_usage(usage).status == PRICING_AVAILABLE
        assert calculate_cost(usage) == pytest.approx(18.00, rel=1e-6)

    def test_claude_opus_4_8_prices_all_anthropic_cache_components(self):
        usage = UsageData(
            prompt_tokens=4_000_000,
            completion_tokens=1_000_000,
            total_tokens=5_000_000,
            model="claude-opus-4-8",
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={
                    INPUT_TOKENS: 1_000_000,
                    ANTHROPIC_CACHE_CREATION_INPUT_TOKENS: 1_000_000,
                    ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H: 1_000_000,
                    ANTHROPIC_CACHE_READ_INPUT_TOKENS: 1_000_000,
                    OUTPUT_TOKENS: 1_000_000,
                },
            ),
        )

        quote = pricing_quote_for_usage(usage)

        assert quote.status == PRICING_AVAILABLE
        assert quote.reason is None
        assert calculate_cost(usage) == pytest.approx(46.75, rel=1e-6)

    def test_unknown_anthropic_provider_model_is_unavailable(self):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
            model="claude-future-9",
            provider="anthropic",
            api_surface="messages",
        )

        quote = pricing_quote_for_usage(usage)

        assert quote.status == "unavailable"
        assert quote.reason == "anthropic_model_pricing_not_registered"
        assert calculate_cost(usage) == 0.0


class TestCalculateCostBreakdown:
    """Tests for calculate_cost_breakdown()"""
    
    def test_returns_detailed_breakdown(self):
        """Should return detailed cost breakdown."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            total_tokens=1_500_000,
            model="gpt-4o-mini",
            cached_tokens=200_000,
        )
        
        breakdown = calculate_cost_breakdown(usage)
        
        assert breakdown["input_tokens"] == 800_000  # Uncached
        assert breakdown["cached_tokens"] == 200_000
        assert breakdown["output_tokens"] == 500_000
        assert breakdown["model"] == "gpt-4o-mini"
        assert "input_cost" in breakdown
        assert "cached_cost" in breakdown
        assert "output_cost" in breakdown
        assert "total_cost" in breakdown
        assert "pricing_used" in breakdown
    
    def test_total_matches_calculate_cost(self):
        """Breakdown total should match calculate_cost result."""
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            total_tokens=1_500_000,
            model="gpt-4o",
            cached_tokens=100_000,
        )
        
        breakdown = calculate_cost_breakdown(usage)
        direct_cost = calculate_cost(usage)
        
        assert breakdown["total_cost"] == pytest.approx(direct_cost, rel=1e-9)

    def test_anthropic_breakdown_uses_registered_pricing(self):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            total_tokens=1_500_000,
            model="claude-sonnet-4-6",
            provider="anthropic",
            api_surface="messages",
        )

        breakdown = calculate_cost_breakdown(usage)

        assert breakdown["total_cost"] == pytest.approx(10.50, rel=1e-6)
        assert breakdown["pricing_used"] is None
        assert breakdown["pricing_status"] == "available"

    def test_unknown_openai_breakdown_marks_pricing_estimated(self):
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            total_tokens=1_500_000,
            model="future-openai-model",
            provider="openai",
        )

        breakdown = calculate_cost_breakdown(usage)

        assert breakdown["total_cost"] > 0.0
        assert breakdown["pricing_status"] == "estimated"


class TestCalculateCostComponents:
    """Tests for calculate_cost_components() used by the insights query layer."""

    @pytest.mark.parametrize("model", ("claude-fable-5", "claude-mythos-5"))
    def test_anthropic_fable_family_prices_all_cache_components(self, model):
        quote = get_pricing_quote(ProviderModelRef("anthropic", model))

        assert quote.status == PRICING_AVAILABLE
        assert quote.schedule is not None
        assert quote.schedule.price_for(INPUT_TOKENS) == Decimal("10.00")
        assert quote.schedule.price_for(
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS
        ) == Decimal("12.50")
        assert quote.schedule.price_for(
            ANTHROPIC_CACHE_CREATION_INPUT_TOKENS_1H
        ) == Decimal("20.00")
        assert quote.schedule.price_for(
            ANTHROPIC_CACHE_READ_INPUT_TOKENS
        ) == Decimal("1.00")
        assert quote.schedule.price_for(OUTPUT_TOKENS) == Decimal("50.00")

    @pytest.mark.parametrize(
        ("pricing_date", "input_price", "output_price"),
        (
            (date(2026, 8, 31), Decimal("2.00"), Decimal("10.00")),
            (date(2026, 9, 1), Decimal("3.00"), Decimal("15.00")),
        ),
    )
    def test_anthropic_sonnet5_scheduled_price_transition(
        self,
        pricing_date,
        input_price,
        output_price,
    ):
        quote = get_pricing_quote(
            ProviderModelRef("anthropic", "claude-sonnet-5"),
            effective_date=pricing_date,
        )

        assert quote.status == PRICING_AVAILABLE
        assert quote.schedule is not None
        assert quote.schedule.price_for(INPUT_TOKENS) == input_price
        assert quote.schedule.price_for(OUTPUT_TOKENS) == output_price

    @pytest.mark.parametrize(
        ("model", "input_price", "cached_price", "write_price", "output_price"),
        (
            ("gpt-5.6", 5.0, 0.5, 6.25, 30.0),
            ("gpt-5.6-sol", 5.0, 0.5, 6.25, 30.0),
            ("gpt-5.6-terra", 2.5, 0.25, 3.125, 15.0),
            ("gpt-5.6-luna", 1.0, 0.1, 1.25, 6.0),
        ),
    )
    def test_gpt56_prices_all_input_components(
        self,
        model,
        input_price,
        cached_price,
        write_price,
        output_price,
    ):
        quote = get_pricing_quote(ProviderModelRef("openai", model))
        assert quote.status == PRICING_AVAILABLE
        assert quote.schedule is not None
        assert float(quote.schedule.price_for(INPUT_TOKENS)) == input_price
        assert float(quote.schedule.price_for("cached_input_tokens")) == cached_price
        assert float(quote.schedule.price_for(CACHE_WRITE_TOKENS)) == write_price
        assert float(quote.schedule.price_for(OUTPUT_TOKENS)) == output_price

    def test_gpt56_cache_write_and_long_context_multipliers(self):
        components = ProviderUsageComponents(
            provider="openai",
            api_surface="responses",
            components={
                "input_tokens": 200_000,
                "cached_input_tokens": 50_000,
                "cache_write_tokens": 50_000,
                "output_tokens": 100_000,
            },
        )

        priced = calculate_cost_components(
            "gpt-5.6-sol",
            prompt_tokens=300_000,
            completion_tokens=100_000,
            cached_tokens=50_000,
            provider="openai",
            api_surface="responses",
            provider_usage_components=components,
        )

        assert priced["uncached_input_cost_usd"] == pytest.approx(2.625)
        assert priced["cached_input_cost_usd"] == pytest.approx(0.05)
        assert priced["output_cost_usd"] == pytest.approx(4.5)

    def test_components_match_calculate_cost_for_reported_row(self):
        """cached+uncached+output must equal calculate_cost for the same inputs."""
        # gpt-4o: input 2.50, cached 1.25, output 10.00 per million
        usage = UsageData(
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            total_tokens=1_500_000,
            model="gpt-4o",
            cached_tokens=400_000,
        )
        components = calculate_cost_components(
            "gpt-4o",
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            cached_tokens=400_000,
        )
        total = (
            components["cached_input_cost_usd"]
            + components["uncached_input_cost_usd"]
            + components["output_cost_usd"]
        )
        assert total == pytest.approx(calculate_cost(usage), rel=1e-9)

    def test_components_known_values_gpt4o(self):
        """Split should match hand-calculated values for gpt-4o."""
        components = calculate_cost_components(
            "gpt-4o",
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            cached_tokens=400_000,
        )
        # uncached 600K @ $2.50/M = $1.50
        # cached   400K @ $1.25/M = $0.50
        # output   500K @ $10.00/M = $5.00
        assert components["uncached_input_cost_usd"] == pytest.approx(1.50)
        assert components["cached_input_cost_usd"] == pytest.approx(0.50)
        assert components["output_cost_usd"] == pytest.approx(5.00)

    def test_components_clamp_cached_above_prompt(self):
        """cached_tokens > prompt_tokens must not produce negative uncached cost."""
        components = calculate_cost_components(
            "gpt-4o",
            prompt_tokens=100,
            completion_tokens=0,
            cached_tokens=500,  # intentionally noisy
        )
        # uncached is clamped to zero, cached capped at prompt_tokens.
        assert components["uncached_input_cost_usd"] == pytest.approx(0.0)
        assert components["cached_input_cost_usd"] >= 0.0

    def test_components_zero_inputs(self):
        components = calculate_cost_components(
            "gpt-4o",
            prompt_tokens=0,
            completion_tokens=0,
            cached_tokens=0,
        )
        assert components == {
            "cached_input_cost_usd": 0.0,
            "uncached_input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
        }

    def test_components_unknown_model_uses_default_pricing(self):
        components = calculate_cost_components(
            "mystery-model-v7",
            prompt_tokens=1_000_000,
            completion_tokens=0,
            cached_tokens=0,
        )
        # DEFAULT_PRICING mirrors gpt-4o inputs ($2.50/M).
        assert components["uncached_input_cost_usd"] == pytest.approx(2.50)

    def test_components_unknown_anthropic_model_are_zeroed(self):
        components = calculate_cost_components(
            "claude-future-9",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            cached_tokens=0,
            provider="anthropic",
        )
        assert components == {
            "cached_input_cost_usd": 0.0,
            "uncached_input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
        }

    def test_components_price_registered_anthropic_usage_components(self):
        components = calculate_cost_components(
            "claude-haiku-4-5-20251001",
            prompt_tokens=4_000_000,
            completion_tokens=1_000_000,
            cached_tokens=0,
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={
                    "input_tokens": 1_000_000,
                    "cache_creation_input_tokens": 1_000_000,
                    "cache_creation_input_tokens_1h": 1_000_000,
                    "cache_read_input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                },
            ),
        )

        assert components["uncached_input_cost_usd"] == pytest.approx(4.25)
        assert components["cached_input_cost_usd"] == pytest.approx(0.10)
        assert components["output_cost_usd"] == pytest.approx(5.00)

    def test_components_price_provider_specific_schedule_keys(self, monkeypatch):
        """Provider component keys must not be silently narrowed to OpenAI fields."""

        def fake_quote(
            _ref,
            *,
            api_surface=None,
            provider_usage_components=None,
            effective_date=None,
        ):
            return PricingQuote(
                provider="anthropic",
                model="claude-test",
                status=PRICING_AVAILABLE,
                api_surface=api_surface,
                schedule=ComponentPriceSchedule(
                    component_prices_per_million={
                        "input_tokens": Decimal("3.00"),
                        "cache_creation_input_tokens": Decimal("3.75"),
                        "cache_read_input_tokens": Decimal("0.30"),
                        "output_tokens": Decimal("15.00"),
                    }
                ),
            )

        monkeypatch.setattr(pricing_module, "get_pricing_quote", fake_quote)

        components = calculate_cost_components(
            "claude-test",
            prompt_tokens=3_000_000,
            completion_tokens=1_000_000,
            cached_tokens=0,
            provider="anthropic",
            api_surface="messages",
            provider_usage_components=ProviderUsageComponents(
                provider="anthropic",
                api_surface="messages",
                components={
                    "input_tokens": 1_000_000,
                    "cache_creation_input_tokens": 1_000_000,
                    "cache_read_input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                },
            ),
        )

        assert components["uncached_input_cost_usd"] == pytest.approx(6.75)
        assert components["cached_input_cost_usd"] == pytest.approx(0.30)
        assert components["output_cost_usd"] == pytest.approx(15.00)


class TestPricingDataIntegrity:
    """Tests for pricing data integrity."""
    
    def test_all_models_have_required_fields(self):
        """All models should have input, output, and cached pricing."""
        for model, pricing in OPENAI_PRICING.items():
            assert "input_per_million" in pricing, f"{model} missing input_per_million"
            assert "output_per_million" in pricing, f"{model} missing output_per_million"
            assert "cached_input_per_million" in pricing, f"{model} missing cached_input_per_million"
    
    def test_cached_not_more_than_regular(self):
        """Cached input price should not exceed regular input price."""
        for model, pricing in OPENAI_PRICING.items():
            assert pricing["cached_input_per_million"] <= pricing["input_per_million"], \
                f"{model} cached price exceeds regular price"
    
    def test_default_pricing_has_required_fields(self):
        """Default pricing should have all required fields."""
        assert "input_per_million" in DEFAULT_PRICING
        assert "output_per_million" in DEFAULT_PRICING
        assert "cached_input_per_million" in DEFAULT_PRICING

    @pytest.mark.parametrize("provider", [OPENAI_PROVIDER_ID, ANTHROPIC_PROVIDER_ID])
    def test_visible_catalog_models_have_exact_available_pricing(self, provider):
        """App-visible catalog models must not rely on estimates or unavailable pricing."""
        for profile in list_catalog_model_profiles(provider):
            quote = get_pricing_quote(ProviderModelRef(provider, profile.ref.model))

            assert quote.status == PRICING_AVAILABLE, profile.ref.model
            assert quote.reason is None, profile.ref.model
            assert quote.schedule is not None, profile.ref.model
    
    def test_gpt4o_mini_is_cheapest_current(self):
        """GPT-4o-mini should be the cheapest current model."""
        mini_pricing = OPENAI_PRICING["gpt-4o-mini"]
        
        for model in ["gpt-4o", "gpt-5", "gpt-5-mini", "gpt-5.1", "gpt-5-pro"]:
            if model in OPENAI_PRICING:
                other_pricing = OPENAI_PRICING[model]
                assert mini_pricing["input_per_million"] <= other_pricing["input_per_million"], \
                    f"gpt-4o-mini should be cheaper than {model}"
