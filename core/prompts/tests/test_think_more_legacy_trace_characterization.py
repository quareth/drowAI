"""Cleanup characterization tests for ``build_think_more_prompt``.

These tests pin down the *cleaned* (Phase 0) behavior of
``DeepReasoningPromptBuilder.build_think_more_prompt`` after the legacy
``trace.observations`` / ``trace.executed_tools`` reads were removed:

- ``trace.observations`` slices no longer leak into the prompt.
- ``trace.executed_tools[-1]`` observation slices no longer leak in.
- The legacy ``"No observations yet"`` / ``"No tools executed yet"`` /
  ``"unknown"`` placeholders no longer appear in this prompt.
- Plan and todo sections render conditionally and are omitted when empty.
- The shared ``_format_recent_tools`` helper is intentionally retained for
  ``build_decision_prompt`` and still emits its own placeholder when called
  directly.

Scope is intentionally narrow: these tests cover only
``build_think_more_prompt`` plus a single direct-helper assertion that
documents the helper survival contract for ``build_decision_prompt``. They
must not exercise ``build_system_prompt``, ``build_decision_prompt``, or
``build_tool_summary_prompt``.
"""

from __future__ import annotations

from typing import Dict

from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder


def _base_state(**overrides: object) -> Dict[str, object]:
    state: Dict[str, object] = {
        "facts": {
            "plan": [],
            "todo_list": [],
        },
        "trace": {
            "observations": [],
            "executed_tools": [],
        },
    }
    state.update(overrides)  # type: ignore[arg-type]
    return state


def test_think_more_does_not_render_trace_observations() -> None:
    """Trace observations must not leak into the cleaned think_more prompt."""

    state = _base_state(
        trace={
            "observations": [
                "obs-old-1",
                "obs-old-2",
                "obs-recent-A",
                "obs-recent-B",
                "obs-recent-C",
            ],
            "executed_tools": [],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "Recent Observations" not in prompt
    assert "obs-old-1" not in prompt
    assert "obs-old-2" not in prompt
    assert "obs-recent-A" not in prompt
    assert "obs-recent-B" not in prompt
    assert "obs-recent-C" not in prompt


def test_think_more_does_not_render_last_executed_tool_observation() -> None:
    """``trace.executed_tools[-1]`` observation slices must not leak in."""

    long_observation = "X" * 350
    state = _base_state(
        trace={
            "observations": [],
            "executed_tools": [
                {"tool_id": "older.tool", "observation": "older-result"},
                {"tool_id": "nmap.scan", "observation": long_observation},
            ],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "Last Tool Result" not in prompt
    assert "nmap.scan" not in prompt
    assert "older.tool" not in prompt
    assert "older-result" not in prompt
    # No truncated observation slice should be present either.
    assert "X" * 50 not in prompt


def test_think_more_omits_legacy_no_observations_placeholder_when_empty() -> None:
    """The legacy ``"No observations yet"`` placeholder is gone from think_more."""

    state = _base_state(
        trace={
            "observations": [],
            "executed_tools": [],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "Recent Observations" not in prompt
    assert "No observations yet" not in prompt
    # The legacy "No tools executed yet" placeholder must not appear either.
    assert "No tools executed yet" not in prompt


def test_think_more_omits_last_tool_section_when_no_executed_tools() -> None:
    """With no executed tools the cleaned builder still emits no last-tool block.

    This test survives Phase 0: today's empty-state shape carries no
    "Last Tool Result" header in either the legacy or the cleaned builder,
    so the assertion remains stable across the cleanup boundary.
    """

    state = _base_state(
        trace={
            "observations": [],
            "executed_tools": [],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "Last Tool Result" not in prompt


def test_think_more_does_not_render_unknown_placeholder_for_missing_tool_id() -> None:
    """The literal ``"unknown"`` placeholder is gone from think_more output."""

    state = _base_state(
        trace={
            "observations": [],
            "executed_tools": [
                {"observation": "some-output"},
            ],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "Last Tool Result" not in prompt
    assert "unknown" not in prompt
    assert "some-output" not in prompt


def test_think_more_renders_no_tools_executed_yet_placeholder_via_decision_prompt_helper() -> None:
    """The shared ``_format_recent_tools`` helper still emits ``"No tools executed yet"``.

    ``build_think_more_prompt`` does not call it after the Phase 0
    cleanup, but the helper is consumed by ``build_decision_prompt``.
    Phase 0 must keep this helper reachable for ``build_decision_prompt``.
    This test pins down that the placeholder string lives on the helper
    itself, so removing ``build_think_more_prompt``'s legacy reads cannot
    accidentally drop it from the decision prompt path.
    """

    builder = DeepReasoningPromptBuilder()

    assert builder._format_recent_tools([]) == "No tools executed yet"


def test_think_more_renders_only_task_tail_when_state_is_empty() -> None:
    """Empty plan/todo collapses to the always-on task tail with no legacy sections."""

    state = _base_state()

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    # Always-on task tail is present.
    assert "## Your Task" in prompt
    assert "Required Response Format" in prompt

    # No conditional sections rendered.
    assert "## Current Plan" not in prompt
    assert "## Todo List" not in prompt

    # No legacy headings or placeholders bleed through.
    assert "Recent Observations" not in prompt
    assert "Last Tool Result" not in prompt
    assert "No observations yet" not in prompt
    assert "No tools executed yet" not in prompt
    assert "unknown" not in prompt


def test_think_more_renders_plan_section_when_plan_is_present() -> None:
    """Non-empty plan renders the conditional ``## Current Plan`` section."""

    state = _base_state(
        facts={
            "plan": ["Run nmap scan", "Enumerate SMB if open"],
            "todo_list": [],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "## Current Plan" in prompt
    assert "1. Run nmap scan" in prompt
    assert "2. Enumerate SMB if open" in prompt
    # Empty todo list must remain conditional.
    assert "## Todo List" not in prompt


def test_think_more_renders_todo_section_when_todos_are_present() -> None:
    """Non-empty todo list renders the conditional ``## Todo List`` section."""

    state = _base_state(
        facts={
            "plan": [],
            "todo_list": [{"text": "Identify open ports"}, {"text": "Check SMB"}],
        }
    )

    prompt = DeepReasoningPromptBuilder().build_think_more_prompt(state)

    assert "## Todo List" in prompt
    assert "Identify open ports" in prompt
    assert "Check SMB" in prompt
    # Empty plan must remain conditional.
    assert "## Current Plan" not in prompt
