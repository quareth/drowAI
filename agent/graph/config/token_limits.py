"""
Centralized token limit configuration for all LangGraph nodes.

This module provides a single source of truth for max_tokens values used
across the graph. Most node limits can still be overridden via environment
variables; intent-classifier and context-window ceilings live in their own
canonical modules instead.

Environment Variables:
    FINAL_ANSWER_MAX_TOKENS: User-facing final answer (default: 2000)
    DR_FINAL_MAX_TOKENS: Deep reasoning final answer (default: 2000)
    SYNTHESIS_MAX_TOKENS: Loop recovery synthesis (default: 1500)
    POST_TOOL_REASONING_MAX_TOKENS: Analysis between tool executions (default: 1500)
    THINK_MORE_MAX_TOKENS: Extended thinking node (default: 1000)
    REFLECT_MAX_TOKENS: Reflection node (default: 800)
    PLANNER_MAX_TOKENS: Planning node (default: 1000)
    DECISION_ROUTER_MAX_TOKENS: Routing decisions (default: 300)
    TOOL_SELECTION_MAX_TOKENS: Tool category selection (default: 200)
    TOOL_ARTICULATION_MAX_TOKENS: Tool intent articulation (default: 150)
    Intent classifier ceiling: fixed at 32_000 in LIMITS.intent_classifier (see this module)

Usage:
    from agent.graph.config.token_limits import LIMITS

    # In any node:
    response = await llm_client.chat_with_usage(
        system_prompt,
        user_prompt,
        max_tokens=LIMITS.final_answer,
    )
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(key: str, default: int) -> int:
    """Get integer from environment with fallback to default."""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class TokenLimits:
    """Centralized token limits grouped by purpose.
    
    All limits are configurable via environment variables. The defaults
    are tuned for detailed pentesting explanations while keeping routing
    decisions concise.
    
    Categories:
        - Final Answer: What users see - should be generous
        - Reasoning: Intermediate thinking between steps
        - Planning: Task decomposition and strategy
        - Routing/Utility: Quick decisions - keep small
    """
    
    # === FINAL ANSWER (user-facing output) ===
    # These should be generous - pentesting explanations need space
    final_answer: int = _env_int("FINAL_ANSWER_MAX_TOKENS", 5000)
    deep_reasoning_final: int = _env_int("DR_FINAL_MAX_TOKENS", 5000)
    synthesis: int = _env_int("SYNTHESIS_MAX_TOKENS", 1500)
    
    # === REASONING (intermediate thinking) ===
    post_tool_reasoning: int = _env_int("POST_TOOL_REASONING_MAX_TOKENS", 1500)
    think_more: int = _env_int("THINK_MORE_MAX_TOKENS", 1000)
    reflect: int = _env_int("REFLECT_MAX_TOKENS", 800)
    
    # === PLANNING ===
    planner: int = _env_int("PLANNER_MAX_TOKENS", 1000)
    
    # === ROUTING/UTILITY (keep concise - just decisions) ===
    decision_router: int = _env_int("DECISION_ROUTER_MAX_TOKENS", 300)
    tool_selection: int = _env_int("TOOL_SELECTION_MAX_TOKENS", 200)
    tool_articulation: int = _env_int("TOOL_ARTICULATION_MAX_TOKENS", 150)
    
    # === INTENT CLASSIFICATION ===
    # Oversized ceiling so structured classifier output is not truncated.
    intent_classifier: int = 32_000


# Singleton instance - import this in all nodes
LIMITS = TokenLimits()


# ============================================================================
# Legacy Compatibility Aliases (deprecated - use LIMITS.xxx instead)
# ============================================================================
# These maintain backward compatibility during migration. New code should
# import LIMITS directly.

MAX_REASONING_TOKENS = LIMITS.post_tool_reasoning
"""Deprecated: Use LIMITS.post_tool_reasoning instead."""

FINAL_ANSWER_TOKENS = LIMITS.final_answer
"""Deprecated: Use LIMITS.final_answer instead."""


__all__ = [
    "LIMITS",
    "TokenLimits",
    # Legacy aliases
    "MAX_REASONING_TOKENS",
    "FINAL_ANSWER_TOKENS",
]
