"""Token usage tracking service.

This module provides production-grade token tracking that captures actual
API usage (not estimates) from OpenAI responses, storing prompt and completion
token counts with accurate cost calculations.

Key components:
- UsageData: Immutable container for token counts from a single LLM call
- TaskUsageSummary: Aggregated usage statistics for a task
- UsageTrackingService: Central service for recording and querying usage
- Pricing: Model-specific cost calculations with cached input discounts

Usage:
    from backend.services.usage_tracking import UsageTrackingService, UsageData
    
    # In a request handler
    service = UsageTrackingService(db)
    
    # Record usage from LLM response
    usage = UsageData.from_openai_chat_response(response, model)
    service.record_usage(
        task_id=task_id,
        user_id=user_id,
        usage=usage,
        source="langgraph_normal",
    )
    
    # Query aggregated usage
    summary = service.get_task_usage(task_id)
    print(f"Cost: ${summary.total_cost_usd:.4f}")
"""

from .models import ProviderUsageComponents, UsageData, TaskUsageSummary
from .pricing import (
    OPENAI_PRICING,
    PRICING_AVAILABLE,
    PRICING_ESTIMATED,
    PRICING_PARTIAL,
    PRICING_UNAVAILABLE,
    calculate_cost,
    calculate_cost_breakdown,
    get_model_pricing,
    pricing_status_for_usage,
)
from .service import UsageTrackingService
from .cache import UsageCache, get_default_cache

__all__ = [
    # Core data types
    "ProviderUsageComponents",
    "UsageData",
    "TaskUsageSummary",
    # Service
    "UsageTrackingService",
    # Cache (optional)
    "UsageCache",
    "get_default_cache",
    # Pricing utilities
    "calculate_cost",
    "calculate_cost_breakdown",
    "get_model_pricing",
    "OPENAI_PRICING",
    "PRICING_AVAILABLE",
    "PRICING_ESTIMATED",
    "PRICING_PARTIAL",
    "PRICING_UNAVAILABLE",
    "pricing_status_for_usage",
]
