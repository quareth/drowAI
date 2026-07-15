"""Utility helpers shared across LangGraph nodes.

Exports are resolved lazily so lightweight imports do not pull optional LLM
runtime dependencies when only catalog/validation helpers are needed.
"""

from __future__ import annotations

import importlib
from typing import Dict, Tuple

_EXPORT_MAP: Dict[str, Tuple[str, str]] = {
    "create_plan_context": ("agent.graph.utils.cache_invalidation", "create_plan_context"),
    "invalidate_plan": ("agent.graph.utils.cache_invalidation", "invalidate_plan"),
    "is_plan_degraded": ("agent.graph.utils.cache_invalidation", "is_plan_degraded"),
    "should_invalidate_plan": ("agent.graph.utils.cache_invalidation", "should_invalidate_plan"),
    "resolve_llm_client": ("agent.graph.utils.llm_resolver", "resolve_llm_client"),
    "merge_plans": ("agent.graph.utils.plan_validation", "merge_plans"),
    "should_reject_plan_update": ("agent.graph.utils.plan_validation", "should_reject_plan_update"),
    "validate_plan_quality": ("agent.graph.utils.plan_validation", "validate_plan_quality"),
    "are_scope_goals_achieved": ("agent.graph.utils.termination_guardrails", "are_scope_goals_achieved"),
    "calculate_termination_bias": ("agent.graph.utils.termination_guardrails", "calculate_termination_bias"),
    "check_goal_completion": ("agent.graph.utils.termination_guardrails", "check_goal_completion"),
    "check_iteration_budget_warnings": (
        "agent.graph.utils.termination_guardrails",
        "check_iteration_budget_warnings",
    ),
    "has_sufficient_findings": ("agent.graph.utils.termination_guardrails", "has_sufficient_findings"),
    "is_action_loop_detected": ("agent.graph.utils.termination_guardrails", "is_action_loop_detected"),
    "is_stuck_without_progress": ("agent.graph.utils.termination_guardrails", "is_stuck_without_progress"),
    "ToolCatalogEntry": ("agent.graph.utils.tool_catalog", "ToolCatalogEntry"),
    "ToolCatalogResult": ("agent.graph.utils.tool_catalog", "ToolCatalogResult"),
    "build_tool_catalog": ("agent.graph.utils.tool_catalog", "build_tool_catalog"),
}


def __getattr__(name: str):
    """Resolve utility exports lazily to reduce import fragility."""
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr_name = target
    module = importlib.import_module(module_path)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


__all__ = list(_EXPORT_MAP.keys())
