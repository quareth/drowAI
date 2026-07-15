"""Pure execution-mode parsing and normalization policy for chat routing."""

from __future__ import annotations

from typing import Optional, Tuple

from backend.services.langgraph_chat.contracts import (
    AgentMode,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.exceptions import PlanModeUnavailableError


class ModePolicyError(ValueError):
    """Raised when mode input is invalid or combinatorially rejected."""


def parse_execution_mode(raw: Optional[str]) -> Optional[ExecutionMode]:
    """Map legacy mode strings to ExecutionMode values."""
    if not raw:
        return None
    normalized = raw.strip().lower()
    mapping = {
        "normal": ExecutionMode.NORMAL_CHAT,
        "normal_chat": ExecutionMode.NORMAL_CHAT,
        "default": ExecutionMode.NORMAL_CHAT,
        "deep": ExecutionMode.DEEP_REASONING,
        "deep_reasoning": ExecutionMode.DEEP_REASONING,
        "dr": ExecutionMode.DEEP_REASONING,
        "simple_tool": ExecutionMode.SIMPLE_TOOL,
        "simple_tool_execution": ExecutionMode.SIMPLE_TOOL,
        "quick_tool": ExecutionMode.SIMPLE_TOOL,
    }
    return mapping.get(normalized)


def parse_agent_mode(raw: Optional[str]) -> Optional[AgentMode]:
    """Map incoming agent mode strings to AgentMode values."""
    if not raw:
        return None
    normalized = raw.strip().lower()
    mapping = {
        "full_access": AgentMode.FULL_ACCESS,
        "agent_full": AgentMode.FULL_ACCESS,
        "full": AgentMode.FULL_ACCESS,
        "agent": AgentMode.AGENT,
        "plan": AgentMode.PLAN,
        "chat": AgentMode.CHAT,
    }
    return mapping.get(normalized)


def normalize_agent_plan_pair(
    *,
    agent_mode: Optional[AgentMode],
    plan_mode: Optional[bool],
) -> Tuple[Optional[AgentMode], bool]:
    """Normalize legacy agent/plan mode combinations to a single canonical pair."""
    normalized_plan_mode = bool(plan_mode)
    if agent_mode == AgentMode.PLAN:
        return AgentMode.AGENT, True
    if agent_mode == AgentMode.CHAT and normalized_plan_mode:
        raise ModePolicyError(
            "agent_mode=chat is mutually exclusive with plan_mode=true. "
            "Plan is a route overlay for agent / full_access; it cannot "
            "stack on top of chat."
        )
    return agent_mode, normalized_plan_mode


def enforce_plan_mode_availability(
    runtime_config: LangGraphRuntimeConfig,
    *,
    deep_reasoning_enabled_default: bool,
) -> None:
    """Fail closed when `agent_mode=plan` cannot be served.

    Args:
        runtime_config: Runtime config carrying execution route policy metadata.
        deep_reasoning_enabled_default: Fallback deep-reasoning flag value when
            runtime metadata does not include the feature flag.

    Raises:
        PlanModeUnavailableError: If plan mode forced deep reasoning but deep
            reasoning is disabled.
    """
    metadata = runtime_config.metadata
    policy = metadata.get("execution_route_policy")
    if not isinstance(policy, dict):
        return
    if policy.get("forced_execution_mode") != ExecutionMode.DEEP_REASONING.value:
        return
    feature_flags = metadata.get("feature_flags") or {}
    deep_reasoning_enabled = feature_flags.get(
        "deep_reasoning_enabled", deep_reasoning_enabled_default
    )
    if deep_reasoning_enabled:
        return
    metadata["plan_mode_rejected"] = True
    metadata["plan_mode_rejection_reason"] = "deep_reasoning_disabled"
    raise PlanModeUnavailableError(
        "Plan mode is not available: deep reasoning is disabled in this "
        "deployment. The classifier was not invoked to avoid emitting a "
        "conflicting audit trail. Re-enable LANGGRAPH_DEEP_REASONING or "
        "select a different agent_mode."
    )


__all__ = [
    "ModePolicyError",
    "enforce_plan_mode_availability",
    "normalize_agent_plan_pair",
    "parse_agent_mode",
    "parse_execution_mode",
]
