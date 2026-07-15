"""Tests for the shared 19-section reasoning-context composer.

These tests pin :func:`compose_shared_reasoning_sections` so that:

* It imports cleanly and returns a ``list[str]`` for empty state.
* It renders each of the 19 canonical headings only when the underlying
  body is non-empty (data-driven conditional rendering).
* It renders section-snapshot phase memory blocks from current-turn phase
  ledger records.
* It produces output that, when concatenated via ``"\\n\\n".join(...)``,
  is **byte-for-byte identical** to the inline 19-section bodies of
  :func:`build_think_more_prompt` and :func:`build_synthesis_prompt`
  (excluding intros and task tails / consumer-specific tails) for a
  representative populated state. This is the contract Phase 1 Task 1.2
  / 1.3 will rely on when delegating those builders to the composer.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from core.prompts.builders._reasoning_context import (
    compose_shared_reasoning_sections,
)
from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder
from core.prompts.builders.synthesis import build_synthesis_prompt


# ---------------------------------------------------------------------------
# Empty / smoke
# ---------------------------------------------------------------------------


def test_empty_state_returns_empty_list() -> None:
    """No facts/metadata/kwargs -> empty list (caller appends task tail)."""
    sections = compose_shared_reasoning_sections({})
    assert isinstance(sections, list)
    assert sections == []


def test_empty_state_with_kwargs_no_data_returns_empty_list() -> None:
    """Kwargs that produce no body content also collapse to empty list."""
    sections = compose_shared_reasoning_sections(
        {"facts": {"plan": [], "todo_list": []}},
        turn_sequence=None,
        current_phase_sequence=None,
        latest_recorded_phase_sequence=None,
        relevant_findings=None,
        environment_context="",
    )
    assert sections == []


# ---------------------------------------------------------------------------
# Headings: each canonical section renders when its body is non-empty
# ---------------------------------------------------------------------------


def _state_with(*, facts: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "facts": dict(facts) if facts else {"plan": [], "todo_list": []},
        "trace": {"observations": [], "executed_tools": []},
    }


def test_user_input_and_user_goal_render() -> None:
    state = _state_with(
        facts={
            "plan": [],
            "todo_list": [],
            "message": "Scan 10.0.0.5",
            "current_goal": "Identify open ports",
        }
    )
    sections = compose_shared_reasoning_sections(state)
    blob = "\n\n".join(sections)
    assert "## User Input" in blob
    assert "Scan 10.0.0.5" in blob


def test_current_focus_renders_from_current_goal() -> None:
    state = _state_with(
        facts={"plan": [], "todo_list": [], "current_goal": "Enumerate TLS"}
    )
    sections = compose_shared_reasoning_sections(state)
    blob = "\n\n".join(sections)
    assert "## Current Focus\nEnumerate TLS" in blob


def test_plan_and_todo_render_with_canonical_headings() -> None:
    state = _state_with(
        facts={
            "plan": ["Recon the host", "Enumerate services"],
            "todo_list": [{"text": "Check open ports"}],
        }
    )
    sections = compose_shared_reasoning_sections(state)
    blob = "\n\n".join(sections)
    assert "## Current Plan" in blob
    assert "## Todo List" in blob


def test_environment_context_renders() -> None:
    state = _state_with(facts={"plan": [], "todo_list": []})
    sections = compose_shared_reasoning_sections(
        state, environment_context="kali rolling 2024.3"
    )
    blob = "\n\n".join(sections)
    assert "## Container Environment" in blob
    assert "kali rolling 2024.3" in blob


def test_phase_memory_renders_from_metadata() -> None:
    """Phase memory renders tagged section-snapshot blocks."""
    state = _state_with(
        facts={
            "plan": [],
            "todo_list": [],
            "metadata": {
                "working_memory": {
                    "current_turn_phases": [
                        {
                            "turn_sequence": 1,
                            "phase_sequence": 1,
                            "source": "think_more",
                            "sections": [
                                {
                                    "heading": "Action Reasoning",
                                    "body": "Initial reasoning",
                                }
                            ],
                        }
                    ]
                }
            },
        }
    )
    sections = compose_shared_reasoning_sections(state, turn_sequence=1)
    blob = "\n\n".join(sections)
    assert "## Prior Current-Turn Phase Memory" in blob
    assert "<phase turn=1 phase=1 source=think_more>" in blob
    assert "## Action Reasoning\nInitial reasoning" in blob
    assert "</phase>" in blob
    assert "[turn=1 phase=1 source=think_more]" not in blob


# ---------------------------------------------------------------------------
# Pure-formatter contract: composer never mutates state and never injects
# placeholder strings.
# ---------------------------------------------------------------------------


def test_composer_does_not_mutate_state() -> None:
    state: Dict[str, Any] = {
        "facts": {
            "plan": ["a"],
            "todo_list": [{"text": "b"}],
            "current_goal": "c",
            "metadata": {"working_memory": {}},
        }
    }
    snapshot = {
        "plan": list(state["facts"]["plan"]),
        "todo_list": list(state["facts"]["todo_list"]),
        "current_goal": state["facts"]["current_goal"],
    }
    compose_shared_reasoning_sections(state)
    assert state["facts"]["plan"] == snapshot["plan"]
    assert state["facts"]["todo_list"] == snapshot["todo_list"]
    assert state["facts"]["current_goal"] == snapshot["current_goal"]


def test_composer_does_not_emit_legacy_placeholders_for_empty_state() -> None:
    sections = compose_shared_reasoning_sections({})
    blob = "\n\n".join(sections)
    assert "No plan" not in blob
    assert "No reasoning recorded" not in blob


# ---------------------------------------------------------------------------
# Byte-for-byte parity with current think_more / synthesis inline blocks
# ---------------------------------------------------------------------------


def _representative_state() -> Dict[str, Any]:
    """Populated state exercising several conditional sections at once."""
    return {
        "facts": {
            "message": "Probe the SMB service on 10.0.0.5",
            "current_goal": "Determine SMB version and shares",
            "plan": ["Run nmap -sV", "Enumerate shares"],
            "todo_list": [{"text": "Run nmap -sV 10.0.0.5"}, "Inspect SMB"],
            "metadata": {
                "working_memory": {
                    "current_turn_phases": [
                        {
                            "turn_sequence": 1,
                            "phase_sequence": 1,
                            "source": "think_more",
                            "sections": [
                                {
                                    "heading": "Action Reasoning",
                                    "body": "Decided to start with nmap",
                                }
                            ],
                        }
                    ],
                    "active_decision": {
                        "status": "active",
                        "decision": "Start with nmap recon",
                    },
                },
                "request_contract": {
                    "answer_style": "concise",
                    "terminal_when": "smb version known",
                },
                "scope": {"in_scope": ["10.0.0.5"]},
            },
        },
        "trace": {"observations": [], "executed_tools": []},
    }


# The intro and task-tail literals from ``build_think_more_prompt`` and
# ``build_synthesis_prompt``. These are the only bytes outside the shared
# 19-section bundle; if the composer's output equals what those builders
# emit minus these constants, parity holds.
_THINK_MORE_INTRO = (
    "Think deeply about the current situation and decide what to do next."
)
_THINK_MORE_TASK_TAIL = """## Your Task
1. Analyze what we've learned so far
2. Determine if the plan needs updating based on new information
3. Identify the most important next step
4. Surface key observations to remember

**Guiding Questions**:
- What have we discovered?
- Does this change our approach?
- What's the logical next step?
- Are we making progress toward the goal?

**Required Response Format**:
```json
{
  "reasoning": "Your detailed analysis of the situation",
  "updated_plan": ["step 1", "step 2", ...],  // Updated plan if needed, or keep current plan
  "next_goal": "The immediate next objective",
  "key_observations": ["observation 1", "observation 2", ...]  // Key facts to remember
}
```

Provide your analysis as valid JSON."""

_SYNTHESIS_INTRO = (
    "I have detected that I'm in a reasoning loop and need to provide a "
    "final response."
)
_SYNTHESIS_TASK_TAIL = """## Your Task
Generate a graceful final response that:
1. Acknowledges you got stuck in a loop (be honest and transparent)
2. Summarizes what you discovered and attempted
3. Explains any partial findings or observations (even if incomplete)
4. Identifies what prevented you from completing the task
5. Suggests concrete alternative approaches the user could try

**Format**:
Write a natural, conversational response (not JSON). Be professional, helpful, and self-aware.
Start with an acknowledgment like \"I apologize, but I've detected I'm stuck in a reasoning loop...\"

**Remember**: Users appreciate honesty and partial value over incomplete results."""


def test_byte_for_byte_parity_with_think_more_inline() -> None:
    """Composer + intro + task tail reproduces ``build_think_more_prompt``."""
    state = _representative_state()
    kwargs: Dict[str, Any] = {
        "turn_sequence": 1,
        "current_phase_sequence": 2,
        "latest_recorded_phase_sequence": 1,
        "relevant_findings": None,
        "environment_context": "kali rolling 2024.3",
    }
    composer_sections = compose_shared_reasoning_sections(state, **kwargs)
    expected = "\n\n".join(
        [_THINK_MORE_INTRO, *composer_sections, _THINK_MORE_TASK_TAIL]
    )
    actual = DeepReasoningPromptBuilder().build_think_more_prompt(state, **kwargs)
    assert actual == expected


def test_byte_for_byte_parity_with_synthesis_inline() -> None:
    """Composer + intro + task tail reproduces ``build_synthesis_prompt``.

    ``reflection_count=0`` and ``iterations=0`` keep ``## Loop Details``
    out of the synthesis output so the comparison is exactly the shared
    19 sections.
    """
    state = _representative_state()
    kwargs: Dict[str, Any] = {
        "turn_sequence": 1,
        "current_phase_sequence": 2,
        "latest_recorded_phase_sequence": 1,
        "relevant_findings": None,
        "environment_context": "kali rolling 2024.3",
    }
    composer_sections = compose_shared_reasoning_sections(state, **kwargs)
    expected = "\n\n".join(
        [_SYNTHESIS_INTRO, *composer_sections, _SYNTHESIS_TASK_TAIL]
    )
    actual = build_synthesis_prompt(
        state, reflection_count=0, iterations=0, **kwargs
    )
    assert actual == expected


def test_byte_for_byte_parity_minimal_state() -> None:
    """Empty state collapses to intro + task tail only for both builders."""
    state: Dict[str, Any] = {"facts": {"plan": [], "todo_list": []}}
    composer_sections = compose_shared_reasoning_sections(state)
    assert composer_sections == []

    expected_think_more = "\n\n".join(
        [_THINK_MORE_INTRO, _THINK_MORE_TASK_TAIL]
    )
    actual_think_more = (
        DeepReasoningPromptBuilder().build_think_more_prompt(state)
    )
    assert actual_think_more == expected_think_more

    expected_synthesis = "\n\n".join(
        [_SYNTHESIS_INTRO, _SYNTHESIS_TASK_TAIL]
    )
    actual_synthesis = build_synthesis_prompt(
        state, reflection_count=0, iterations=0
    )
    assert actual_synthesis == expected_synthesis
