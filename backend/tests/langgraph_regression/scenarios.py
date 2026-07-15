"""Canonical scenarios for the LangGraph regression test stack."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class RegressionScenario:
    """Represents one deterministic scenario used by regression tests."""

    scenario_id: str
    layer: str
    category: str
    description: str
    message: str
    history: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    expected_branch: str = "normal_chat"
    expected_terminal_status: str = "completed"
    expected_key_decision: str = "finalize"
    expected_event_types: Tuple[str, ...] = field(default_factory=tuple)
    expected_interrupt: bool = False


def _history(*entries: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    return tuple(entries)


BASELINE_SCENARIOS: Tuple[RegressionScenario, ...] = (
    RegressionScenario(
        scenario_id="branch_normal_chat",
        layer="layer3",
        category="branch",
        description="Normal chat branch is selected for safe conversational input.",
        message="Summarize what we have done so far.",
        metadata={"forced_capability": "respond_only"},
        expected_branch="normal_chat",
        expected_event_types=("assistant_final",),
    ),
    RegressionScenario(
        scenario_id="branch_simple_tool",
        layer="layer3",
        category="branch",
        description="Simple tool branch is selected when tool execution is requested.",
        message="Run nmap against 10.10.10.10",
        metadata={"forced_capability": "simple_tool_execution"},
        expected_branch="simple_tool_execution",
        expected_event_types=("tool_start", "tool_delta", "tool_end", "assistant_final"),
    ),
    RegressionScenario(
        scenario_id="branch_deep_reasoning",
        layer="layer3",
        category="branch",
        description="Deep reasoning branch is selected for strategic planning prompts.",
        message="Design a multi-step strategy for assessing this target.",
        metadata={"forced_capability": "deep_reasoning"},
        expected_branch="deep_reasoning",
        expected_event_types=("assistant_final",),
    ),
    RegressionScenario(
        scenario_id="history_empty",
        layer="layer3",
        category="history",
        description="Empty history still produces a stable metadata contract.",
        message="Start a new task.",
        history=(),
        expected_branch="normal_chat",
    ),
    RegressionScenario(
        scenario_id="history_short",
        layer="layer3",
        category="history",
        description="Short history is forwarded to planner and prompt builders.",
        message="Continue with service enumeration.",
        history=_history(
            {"role": "user", "content": "Scan 10.0.0.5"},
            {"role": "assistant", "content": "Ports 22 and 80 were found."},
        ),
        expected_branch="normal_chat",
    ),
    RegressionScenario(
        scenario_id="history_truncated",
        layer="layer3",
        category="history",
        description="Long history is truncated deterministically before prompt injection.",
        message="Given the prior context, what is next?",
        history=_history(
            {"role": "user", "content": "x" * 1200},
            {"role": "assistant", "content": "y" * 1200},
            {"role": "user", "content": "z" * 1200},
        ),
        expected_branch="normal_chat",
    ),
    RegressionScenario(
        scenario_id="tool_success_finalize",
        layer="layer3",
        category="tool",
        description="Successful tool output finalizes without retry.",
        message="Check host exposure.",
        metadata={"decision_history": ("finalize: Goal achieved",)},
        expected_branch="simple_tool_execution",
        expected_key_decision="finalize",
    ),
    RegressionScenario(
        scenario_id="tool_retry_call_tool",
        layer="layer3",
        category="tool",
        description="Failure with retry suggestion routes back to category selection.",
        message="Retry with a safer scan.",
        metadata={
            "decision_history": ("call_tool: Retry with alternate parameters",),
            "failure_detected": True,
            "retry_suggested": True,
        },
        expected_branch="simple_tool_execution",
        expected_key_decision="call_tool",
    ),
    RegressionScenario(
        scenario_id="tool_failure_malformed",
        layer="layer3",
        category="tool",
        description="Malformed decision payload falls back to finalize.",
        message="Handle malformed tool result.",
        metadata={"decision_history": ("unparseable",)},
        expected_branch="simple_tool_execution",
        expected_key_decision="finalize",
    ),
    RegressionScenario(
        scenario_id="hitl_resume_approve",
        layer="layer3",
        category="hitl",
        description="HITL approve response keeps task-thread identity for resume continuity.",
        message="Approve execution and continue.",
        metadata={"resume_action": "approve"},
        expected_branch="simple_tool_execution",
        expected_interrupt=True,
    ),
    RegressionScenario(
        scenario_id="hitl_resume_edit",
        layer="layer3",
        category="hitl",
        description="HITL edit response keeps deterministic conversation threading contracts.",
        message="Edit command before execution.",
        metadata={"resume_action": "edit", "conversation_id": "conv-42"},
        expected_branch="normal_chat",
    ),
    RegressionScenario(
        scenario_id="hitl_resume_skip",
        layer="layer3",
        category="hitl",
        description="HITL skip response preserves anchor checkpoint conversion contract.",
        message="Skip this command and continue.",
        metadata={"resume_action": "skip", "anchor_sequence": 17},
        expected_branch="simple_tool_execution",
        expected_interrupt=True,
    ),
)


SCENARIOS_BY_ID: Dict[str, RegressionScenario] = {
    scenario.scenario_id: scenario for scenario in BASELINE_SCENARIOS
}

