"""Planner prompt assembly helpers.

This module prepares planner-specific prompt inputs from graph metadata,
scope, targets, and environment context. Versioned prompt templates remain
owned by ``core.prompts``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from ..utils.scope_parser import UserScope
from core.prompts.constants import (
    build_planner_scope_constraints,
    build_planner_system_prompt as build_core_planner_system_prompt,
    build_planner_tools_constraint,
    build_planning_prompt as build_core_planning_prompt,
    build_scope_boundary_warnings,
)

logger = logging.getLogger(__name__)


def build_clarify_contract_correction_prompt(planning_prompt: str) -> str:
    """Request a strict clarify contract when the first response is invalid."""
    return (
        f"{planning_prompt}\n\n"
        "Your previous clarify_request contract was invalid.\n"
        "Regenerate the full JSON response and strictly follow these rules:\n"
        "- Every clarify blocker must use input_type \"select\"\n"
        "- Each blocker must include 1-4 unique, non-empty options\n"
        "- Do not use text/freeform blockers\n"
        "- Keep only hard blockers\n"
        "- Keep blocker count at 1-2\n"
    )


def build_scope_validation_correction_prompt(
    planning_prompt: str,
    violations: List[str],
) -> str:
    """Request a corrected plan when deterministic scope validation fails."""
    violation_lines = "\n".join(f"- {item}" for item in violations) or "- Unknown validation error"
    return (
        f"{planning_prompt}\n\n"
        "Your previous plan failed deterministic scope validation with these violations:\n"
        f"{violation_lines}\n\n"
        "Regenerate the full JSON response and fix every violation.\n"
        "Requirements:\n"
        "- Plan must contain only concrete execution steps\n"
        "- Do not include administrative/compliance prerequisites (for example authorization paperwork)\n"
        "- Explicitly include host discovery and target host selection when identify_hosts is required\n"
        "- Explicitly include a port scan step when identify_open_ports is required\n"
    )


def build_planner_system_prompt(env_prompt: str) -> str:
    """Build system prompt for planner, including environment info if available.

    Args:
        env_prompt: Formatted environment info string (may be empty).

    Returns:
        System prompt with environment context.
    """
    return build_core_planner_system_prompt(env_prompt)


def build_planning_prompt(
    targets: List[str],
    metadata: Dict[str, Any],
    available_tools: Optional[List[str]] = None,
    user_scope: Optional[UserScope] = None,
    env_prompt: str = "",
) -> str:
    """Build prompt for initial planning.

    Args:
        targets: List of target IPs/hostnames
        metadata: State metadata. The in-flight user turn is surfaced
            through the shared ``ConversationContextBundle`` projection
            (tagged ``latest=true`` inside the rendered transcript), so
            the prompt no longer carries a separate ``user_message``
            argument.
        available_tools: Optional list of available tool IDs to constrain planning
        user_scope: Parsed scope constraints from user message
    """
    targets_str = ", ".join(targets) if targets else "not specified"

    network_discovery_section = ""
    if not targets:
        network_discovery_section = (
            "\n\n**Target Selection Guidance**:\n"
            "- Targets are not pre-specified for this request.\n"
            "- Start with concrete host discovery to identify reachable hosts.\n"
            "- Select one discovered host and then execute the requested port scan step.\n"
            "- Do NOT add administrative/compliance prerequisites (authorization/legal paperwork).\n"
            "- Assume this task is already authorized within the provided scope context.\n"
        )

    # DR planner prompt is sourced solely from the classifier-derived
    # ``working_memory.intent_brief`` folded at turn start by the
    # working-memory node.
    #
    # The planner does not read ``ConversationContextBundle`` transcript
    # text — only the intent classifier and the deep-reasoning finalizer
    # retain full-history authority (see docs/plans/intent_interpretation_wiring.md).
    intent_brief: Mapping[str, Any] = {}
    working_memory = metadata.get("working_memory")
    if isinstance(working_memory, Mapping):
        brief_candidate = working_memory.get("intent_brief")
        if isinstance(brief_candidate, Mapping):
            intent_brief = brief_candidate

    clarified_inputs_section = ""
    clarified_context = metadata.get("clarified_context")
    if isinstance(clarified_context, dict) and clarified_context:
        clarified_lines = [
            f"- {slot}: {value}"
            for slot, value in clarified_context.items()
            if str(slot).strip() and str(value).strip()
        ]
        if clarified_lines:
            clarified_inputs_section = (
                "\n\n**Clarified Required Inputs**:\n"
                + "\n".join(clarified_lines)
                + "\nUse these answers when forming the plan."
            )

    planner_environment_section = ""
    if env_prompt:
        planner_environment_section = (
            "\n\n**Environment Context**:\n"
            f"{env_prompt}\n"
            "Use this network configuration to inform your planning."
        )

    # Add available tools constraint if provided
    tools_constraint = ""
    if available_tools:
        tools_list = ", ".join(available_tools[:10])  # Limit to first 10 for prompt
        tools_constraint = build_planner_tools_constraint(tools_list)
        logger.debug(
            f"[PLANNER] Constraining plan to {len(available_tools)} available tools"
        )

    # DR.5.3: Add scope constraints if provided (emphasize boundaries to preempt invalid steps)
    scope_constraints = ""
    if user_scope:
        goals_str = ", ".join(user_scope.goals) if user_scope.goals else "None specified"
        boundaries_str = ", ".join(user_scope.boundaries) if user_scope.boundaries else "None"
        conditional_str = ", ".join(
            f"{k}: {v}" for k, v in user_scope.conditional_targets.items()
        ) if user_scope.conditional_targets else "None"
        explicit_tools_str = ", ".join(user_scope.explicit_tools) if user_scope.explicit_tools else "None"

        # Build boundary warnings section (emphasized)
        boundary_warnings = build_scope_boundary_warnings(user_scope.boundaries)

        scope_constraints = build_planner_scope_constraints(
            goals_str=goals_str,
            boundaries_str=boundaries_str,
            conditional_str=conditional_str,
            explicit_tools_str=explicit_tools_str,
            boundary_warnings=boundary_warnings,
        )

    return build_core_planning_prompt(
        targets_str=targets_str,
        network_discovery_section=network_discovery_section,
        tools_constraint=tools_constraint,
        scope_constraints=scope_constraints,
        intent_brief=intent_brief,
        clarified_inputs_section=clarified_inputs_section,
        planner_environment_section=planner_environment_section,
    )
