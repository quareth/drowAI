"""Planner setup helpers for per-turn runtime inputs.

This module owns planner setup state: metadata normalization, environment
loading, target and scope extraction, request-contract inference, and the
setup-time tool-availability gate. It does not perform resume detection,
clarify lifecycle handling, LLM generation, or final graph-state application.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional

from ..context.runtime_state import sync_target_hint_from_plan_todo
from ..state import InteractiveState
from ..utils.environment_loader import get_environment_full, load_and_format_environment
from ..utils.scope_parser import UserScope, parse_user_scope
from ..utils.tool_availability import are_tools_available

logger = logging.getLogger(__name__)

_BINARY_CHECK_HINTS = (
    "determine if",
    "whether",
    "open or closed",
    "is port",
    "is the port",
)
_SHORT_STYLE_HINTS = (
    "short answer",
    "brief answer",
    "one line",
    "just answer",
    "yes/no",
    "yes or no",
)


@dataclass(frozen=True)
class PlannerSetup:
    """Per-turn planner setup values consumed by planner_node orchestration."""

    metadata: Dict[str, Any]
    env_info: Optional[Dict[str, Any]]
    env_prompt: str
    targets: List[str]
    user_message: str
    user_scope: UserScope
    request_contract: Dict[str, str]
    available_tools: List[str]
    tool_unavailable_update: Optional[Dict[str, Any]] = None


def infer_request_contract(
    user_message: str,
    classifier_raw_response: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Infer request contract used by routing/finalization policy."""
    contract: Dict[str, str] = {}
    parsed = classifier_raw_response if isinstance(classifier_raw_response, Mapping) else {}

    question_type = parsed.get("question_type")
    if isinstance(question_type, str) and question_type.strip().lower() in {
        "binary_check",
        "multi_step",
        "open_ended",
    }:
        contract["question_type"] = question_type.strip().lower()

    answer_style = parsed.get("answer_style")
    if isinstance(answer_style, str) and answer_style.strip().lower() in {"short", "normal"}:
        contract["answer_style"] = answer_style.strip().lower()

    terminal_when = parsed.get("terminal_when")
    if isinstance(terminal_when, str) and terminal_when.strip().lower() in {
        "determined",
        "all_steps_done",
    }:
        contract["terminal_when"] = terminal_when.strip().lower()

    lowered = user_message.lower()
    if "question_type" not in contract:
        if any(token in lowered for token in _BINARY_CHECK_HINTS) or re.search(
            r"\bis\s+.+\s+(?:open|closed|up|down)\b", lowered
        ):
            contract["question_type"] = "binary_check"
        else:
            contract["question_type"] = "multi_step"

    if "answer_style" not in contract:
        if any(token in lowered for token in _SHORT_STYLE_HINTS):
            contract["answer_style"] = "short"
        elif contract.get("question_type") == "binary_check":
            contract["answer_style"] = "short"
        else:
            contract["answer_style"] = "normal"

    if "terminal_when" not in contract:
        if contract.get("question_type") == "binary_check":
            contract["terminal_when"] = "determined"
        else:
            contract["terminal_when"] = "all_steps_done"

    return contract


def build_planner_setup(interactive: InteractiveState) -> PlannerSetup:
    """Build setup inputs needed before planner resume or generation decisions."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    sync_target_hint_from_plan_todo(
        metadata,
        todo_list=list(facts.safe_todo_list),
        plan=list(facts.plan or []),
        current_goal=facts.current_goal,
    )

    # Prefer shared turn metadata seeded before graph entry. Direct graph
    # invocations keep the provider-backed fallback for compatibility.
    env_info: Optional[Dict[str, Any]] = None
    env_prompt = ""
    existing_env_info = metadata.get("environment_info")
    if isinstance(existing_env_info, dict):
        env_info = existing_env_info
        env_prompt = get_environment_full(existing_env_info)
    else:
        env_info, env_prompt = load_and_format_environment(facts.task_id)
        if env_info:
            metadata["environment_info"] = env_info
            logger.info(f"[PLANNER] Loaded environment info for task {facts.task_id}")

    # Extract user message and context
    user_message = facts.message
    targets = list((facts.intent_hints or {}).get("targets", []))

    # DR.5.1: Parse user scope
    user_scope = parse_user_scope(user_message)
    request_contract = infer_request_contract(
        user_message,
        metadata.get("intent_classifier_raw_response"),
    )

    # Store scope in metadata and facts (both for compatibility)
    metadata["user_scope"] = user_scope.to_dict()
    metadata["request_contract"] = request_contract
    metadata["scope_goals"] = user_scope.goals  # For are_scope_goals_achieved compatibility
    metadata["scope_boundaries"] = user_scope.boundaries
    facts.scope_goals = user_scope.goals
    facts.scope_boundaries = user_scope.boundaries
    facts.metadata = metadata

    # The planner will determine what tools are needed by analyzing the user request
    # No need to check tool availability beforehand - let LLM planning handle this
    available_tools = []

    return PlannerSetup(
        metadata=metadata,
        env_info=env_info,
        env_prompt=env_prompt,
        targets=targets,
        user_message=user_message,
        user_scope=user_scope,
        request_contract=request_contract,
        available_tools=available_tools,
    )


def evaluate_tool_availability(
    interactive: InteractiveState,
    setup: PlannerSetup,
) -> PlannerSetup:
    """Apply the setup-time capability availability gate at the caller's original point."""
    facts = interactive.facts
    metadata = setup.metadata
    capability = str(facts.capability or "").strip().lower()
    if capability in {
        "vuln_scan",
        "vuln_exploit",
        "service_enum",
        "port_scan",
        "host_discovery",
    }:
        if not are_tools_available(capability):
            metadata.setdefault("tool_gaps", []).append(
                f"{capability} was requested but no tools available"
            )
            facts.ensure_decision_history().append("handle_unavailable_tools: no_tools_available")
            return replace(setup, tool_unavailable_update=interactive.as_graph_update())
    return setup


__all__ = [
    "PlannerSetup",
    "build_planner_setup",
    "evaluate_tool_availability",
    "infer_request_contract",
]
