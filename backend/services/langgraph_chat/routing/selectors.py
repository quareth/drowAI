"""Helpers that determine which branch of the LangGraph facade to execute."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import logging

from agent.graph import InteractiveState
from agent.subagents.registry import SubagentRegistry
from backend.services.agent_runs.dispatch_plan import (
    routing_metadata_from_decision,
    runtime_config_with_subagent_routing,
)
from backend.services.agent_runs.ownership_policy import resolve_subagent_handoff
from backend.services.agent_runs.parent_control import parse_parent_control_outcome
from backend.services.langgraph_chat.contracts import (
    ExecutionMode,
    LangGraphRuntimeConfig,
)


class ChatBranch(str, Enum):
    """High-level branch identifiers for the facade."""

    NORMAL_CHAT = "normal_chat"
    DEEP_REASONING = "deep_reasoning"
    SIMPLE_TOOL = "simple_tool_execution"
    SUBAGENT = "subagent"


def select_branch(config: LangGraphRuntimeConfig) -> ChatBranch:
    """Select the branch that should handle the turn."""

    mapping = {
        ExecutionMode.NORMAL_CHAT: ChatBranch.NORMAL_CHAT,
        ExecutionMode.DEEP_REASONING: ChatBranch.DEEP_REASONING,
        ExecutionMode.SIMPLE_TOOL: ChatBranch.SIMPLE_TOOL,
    }
    return mapping.get(config.execution_mode, ChatBranch.NORMAL_CHAT)


def resolve_branch(
    runtime_config: LangGraphRuntimeConfig,
    *,
    deep_reasoning_enabled: bool,
    simple_tool_enabled: bool,
    active_subagent_run_counts: Mapping[str, int] | None = None,
    subagent_registry: SubagentRegistry | None = None,
) -> ChatBranch:
    """Resolve which branch handles this turn.

    Args:
        runtime_config: Runtime config for the current chat turn.
        deep_reasoning_enabled: Whether the deep-reasoning handler is enabled.
        simple_tool_enabled: Whether the simple-tool handler is enabled.
        active_subagent_run_counts: Optional task-local active counts by
            registered definition-owned agent id.
        subagent_registry: Registry used for deterministic handoff validation.

    Returns:
        The chat branch that should execute the turn.
    """
    logger = logging.getLogger("backend.services.langgraph_chat.facade")
    deterministic_mode = bool(runtime_config.metadata.get("deterministic_mode"))
    if deterministic_mode:
        requested_mode = runtime_config.chat_inputs.requested_mode
        if requested_mode == ExecutionMode.DEEP_REASONING:
            branch = ChatBranch.DEEP_REASONING
        elif requested_mode == ExecutionMode.SIMPLE_TOOL:
            branch = ChatBranch.SIMPLE_TOOL
        else:
            # Deterministic tests default to tool-path scenario if caller omits mode.
            branch = ChatBranch.SIMPLE_TOOL
    else:
        branch = select_branch(runtime_config)

    if branch is ChatBranch.DEEP_REASONING and not deep_reasoning_enabled:
        logger.warning("Deep reasoning disabled, falling back to normal chat")
        branch = ChatBranch.NORMAL_CHAT
    if branch is ChatBranch.SIMPLE_TOOL and not simple_tool_enabled:
        logger.warning("Simple tool disabled, falling back to normal chat")
        branch = ChatBranch.NORMAL_CHAT
    if branch is ChatBranch.SIMPLE_TOOL:
        active_counts = dict(active_subagent_run_counts or {})
        decision = resolve_subagent_handoff(
            runtime_config.metadata,
            registry=subagent_registry,
            active_runs_by_agent_id=active_counts,
        )
        runtime_config.metadata["subagent_routing"] = (
            routing_metadata_from_decision(decision)
        )
        if decision.should_delegate:
            try:
                branch = ChatBranch(str(decision.dispatch_branch))
            except ValueError as exc:
                raise RuntimeError(
                    "registered subagent dispatch branch has no facade handler"
                ) from exc

    return branch


def resolve_late_subagent_handoff(
    runtime_config: LangGraphRuntimeConfig,
    continuation_state: InteractiveState,
    *,
    active_subagent_run_counts: Mapping[str, int] | None = None,
    subagent_registry: SubagentRegistry | None = None,
) -> LangGraphRuntimeConfig | None:
    """Convert a valid PTR delegation into the canonical subagent route."""
    metadata = continuation_state.facts.safe_metadata
    turn_id = str(runtime_config.metadata.get("turn_id") or "").strip()
    control = parse_parent_control_outcome(
        metadata,
        parent_turn_id=turn_id,
        claimed_agent_run_ids=(),
    )
    if control.action != "delegate_subagent":
        return None
    if not turn_id:
        raise RuntimeError("Late subagent handoff requires a parent turn id")

    decision = resolve_subagent_handoff(
        metadata,
        registry=subagent_registry,
        active_runs_by_agent_id=dict(active_subagent_run_counts or {}),
        handoff_entries=control.agent_handoff,
    )
    if not decision.should_delegate:
        raise RuntimeError(
            f"Late subagent handoff rejected: {decision.reason}"
        )

    routed = runtime_config_with_subagent_routing(
        runtime_config,
        routing_metadata_from_decision(
            decision,
            delegation_source="ptr",
            delegation_decision_id=control.decision_id,
        ),
    )
    runtime_budgets = metadata.get("runtime_budgets")
    if isinstance(runtime_budgets, Mapping):
        routed.metadata["runtime_budgets"] = dict(runtime_budgets)
    return routed


__all__ = [
    "ChatBranch",
    "resolve_branch",
    "resolve_late_subagent_handoff",
    "select_branch",
]
