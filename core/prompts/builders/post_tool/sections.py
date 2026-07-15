"""Shared section renderers for prompt builders using post-tool context.

This module owns focused formatting helpers first introduced for
post-tool reasoning and now reused by ``think_more`` for canonical runtime
context projection. Helpers here stay side-effect free and prompt-facing.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence

from core.prompts.constants import POST_TOOL_MAX_DECISION_RATIONALE_CHARS

from ._formatting import as_mapping, as_sequence, truncate


def format_current_execution_context(
    *,
    turn_sequence: Optional[int],
    current_phase_sequence: Optional[int],
    latest_recorded_phase_sequence: Optional[int],
) -> str:
    """Render runtime-supplied turn/phase identity for prompt context."""
    lines: List[str] = []
    if isinstance(turn_sequence, int):
        lines.append(f"turn_sequence: {turn_sequence}")
    if isinstance(current_phase_sequence, int):
        lines.append(f"current_phase_sequence: {current_phase_sequence}")
    if isinstance(latest_recorded_phase_sequence, int):
        lines.append(
            f"latest_recorded_phase_sequence: {latest_recorded_phase_sequence}"
        )
    return "\n".join(lines)


def format_active_decision_hint(metadata: Mapping[str, Any]) -> str:
    """Format advisory active decision from canonical working memory."""
    working_memory = as_mapping(metadata.get("working_memory"))
    active_decision = as_mapping(working_memory.get("active_decision"))
    if not active_decision:
        return ""
    if str(active_decision.get("status") or "").strip().lower() != "active":
        return ""

    lines: List[str] = [
        (
            "Use this only as continuity context. "
            "Current tool output and current todo/goal state are authoritative."
        )
    ]

    next_action = str(active_decision.get("next_action") or "").strip()
    if next_action:
        lines.append(f"next_action: {next_action}")

    tool_intent = as_mapping(active_decision.get("tool_intent"))
    if tool_intent:
        description = str(tool_intent.get("description") or "").strip()
        target = tool_intent.get("target")
        focus = tool_intent.get("focus")
        if description:
            lines.append(f"tool_intent.description: {description}")
        if target not in (None, ""):
            lines.append(f"tool_intent.target: {target}")
        if focus not in (None, ""):
            lines.append(f"tool_intent.focus: {focus}")

    effective_next_goal = active_decision.get("effective_next_goal")
    if effective_next_goal not in (None, ""):
        lines.append(f"effective_next_goal: {effective_next_goal}")

    action_reasoning = str(active_decision.get("action_reasoning") or "").strip()
    if action_reasoning:
        lines.append(
            f"decision_rationale: {truncate(action_reasoning, POST_TOOL_MAX_DECISION_RATIONALE_CHARS)}"
        )

    return "\n".join(lines)


def format_intent_contract(contract: Any) -> str:
    """Format intent contract evaluation for prompt context."""
    if not isinstance(contract, Mapping):
        return ""
    if not contract.get("applicable"):
        return ""

    status = "SATISFIED" if contract.get("satisfied") else "NOT SATISFIED"
    expected_tools = ", ".join(contract.get("expected_tools") or []) or "none"
    expected_targets = ", ".join(contract.get("expected_targets") or []) or "none"
    expected_ports = ", ".join(contract.get("expected_ports") or []) or "none"
    executed_tool = str(contract.get("executed_tool") or "unknown")
    executed_targets = ", ".join(contract.get("executed_targets") or []) or "none"
    executed_ports = ", ".join(contract.get("executed_ports") or []) or "none"
    mismatches = ", ".join(contract.get("mismatches") or []) or "none"
    matched_via = str(contract.get("matched_via") or "").strip()

    rendered = (
        f"Status: {status}\n"
        f"Expected tool(s): {expected_tools}\n"
        f"Expected target(s): {expected_targets}\n"
        f"Expected port(s): {expected_ports}\n"
        f"Executed tool: {executed_tool}\n"
        f"Executed target(s): {executed_targets}\n"
        f"Executed port(s): {executed_ports}\n"
        f"Mismatches: {mismatches}"
    )
    if matched_via:
        rendered += f"\nMatched via: {matched_via}"
    return rendered


def format_request_contract(contract: Any) -> str:
    """Format parsed request contract for decision context."""
    if not isinstance(contract, Mapping):
        return ""
    question_type = str(contract.get("question_type") or "").strip()
    answer_style = str(contract.get("answer_style") or "").strip()
    terminal_when = str(contract.get("terminal_when") or "").strip()
    if not (question_type or answer_style or terminal_when):
        return ""
    lines: List[str] = []
    if question_type:
        lines.append(f"question_type: {question_type}")
    if answer_style:
        lines.append(f"answer_style: {answer_style}")
    if terminal_when:
        lines.append(f"terminal_when: {terminal_when}")
    return "\n".join(lines)


def extract_scope_hint(metadata: Mapping[str, Any]) -> str:
    """Extract scope hints from metadata."""
    hints: List[str] = []

    scope = metadata.get("user_scope")
    if isinstance(scope, Mapping):
        conditional_targets = scope.get("conditional_targets")
        if isinstance(conditional_targets, Mapping):
            fallback = conditional_targets.get("fallback_host")
            if fallback:
                hints.append(f"Fallback host: {fallback}")

        boundaries = scope.get("boundaries")
        if isinstance(boundaries, Sequence) and not isinstance(boundaries, str):
            preview = ", ".join(str(item) for item in list(boundaries)[:3])
            if preview:
                hints.append(f"Boundaries: {preview}")

        targets = scope.get("targets")
        if isinstance(targets, Sequence) and not isinstance(targets, str):
            preview = ", ".join(str(item) for item in list(targets)[:3])
            if preview:
                hints.append(f"Targets: {preview}")

    return "; ".join(hints) if hints else ""


def format_environment_context(environment_context: str) -> str:
    """Return preformatted environment context provided by caller."""
    return environment_context.strip()


def is_tool_visible(metadata: Mapping[str, Any], tool_id: str) -> bool:
    """Return true when tool catalog metadata advertises a tool id."""
    expected = str(tool_id or "").strip()
    if not expected:
        return False

    direct_ids = as_sequence(metadata.get("available_tools"))
    for item in direct_ids:
        if str(item or "").strip() == expected:
            return True

    catalog = as_mapping(metadata.get("tool_catalog"))
    entries = as_sequence(catalog.get("entries"))
    for entry in entries:
        entry_mapping = as_mapping(entry)
        candidate = str(entry_mapping.get("tool_id") or entry_mapping.get("id") or "").strip()
        if candidate == expected:
            return True
    return False


__all__ = [
    "extract_scope_hint",
    "format_active_decision_hint",
    "format_current_execution_context",
    "format_environment_context",
    "format_intent_contract",
    "format_request_contract",
    "is_tool_visible",
]
