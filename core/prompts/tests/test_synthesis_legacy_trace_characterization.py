"""Cleanup characterization tests for ``build_synthesis_prompt``.

These tests pin down the *cleaned* (Phase 0) behavior of
``build_synthesis_prompt`` after the legacy trace/history/placeholder
reads were removed:

- ``format_plan([])`` placeholder ``"No plan"`` no longer appears.
- ``format_tool_attempts([])`` placeholder
  ``"No tools were successfully executed"`` no longer appears.
- ``format_observations([])`` placeholder ``"No observations recorded"``
  no longer appears.
- The hardcoded ``scratchpad_excerpt="No detailed reasoning recorded"``
  placeholder, the misleading ``"Reasoning History (last 500 chars of
  scratchpad)"`` heading, and any other scratchpad reference are gone.
- The ``current_goal`` literal ``"complete the task"`` fallback (and
  the ``Got stuck trying to:`` line that carried it) is gone.
- ``## Loop Details`` is rendered only when ``reflection_count > 0`` or
  ``iterations > 0`` (synthesis-specific signal); the always-on plain-
  text ``## Your Task`` tail is rendered unconditionally.

The shared ``node_utils.format_plan`` / ``node_utils.format_observations``
/ ``node_utils.format_tool_attempts`` helpers may still emit their
placeholder strings when called directly — they survive in
``agent/graph/nodes/node_utils.py`` because ``reflect_node`` still
depends on them. The synthesis cleanup is scoped to the synthesis
prompt path, not to those shared helpers.

Phase 1 of the synthesize-shared-context plan moved ``build_synthesis_prompt``
from ``core/prompts/constants.py`` to ``core/prompts/builders/synthesis.py``
and rebuilt it from the canonical projection helpers ``think_more``
already consumes. These placeholder-absence assertions remain valid
against the new builder; the section headers were updated from
``## Original User Request`` to the shared ``## User Input`` /
``## User Goal`` projections, and the Phase 0 ``**Your Task**:`` inline
label was promoted to the canonical ``## Your Task`` section header.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from core.prompts.builders.synthesis import build_synthesis_prompt


_DEFAULT_GOAL = "Run a network sweep on 10.0.0.0/24"


def _state_for_goal(goal: str) -> Dict[str, Any]:
    """Build the minimal ``state`` mapping consumed by ``build_synthesis_prompt``.

    The new builder reads ``state['facts']['message']`` (verbatim user
    input) via :func:`derive_user_input_and_goal`. The Phase 0
    characterization tests passed the goal through the now-removed
    ``original_goal`` kwarg; here we route the same value via
    ``facts.message`` so the legacy-placeholder absence and loop-detail
    conditional rendering assertions continue to hold against the
    canonical-projection builder.
    """
    return {
        "facts": {"message": goal, "plan": [], "todo_list": []},
        "trace": {"observations": [], "executed_tools": []},
    }


def _build(goal: str = _DEFAULT_GOAL, **kwargs: Any) -> str:
    return build_synthesis_prompt(_state_for_goal(goal), **kwargs)


def test_synthesis_does_not_render_no_plan_placeholder() -> None:
    """``"No plan"`` placeholder no longer leaks into the synthesis prompt."""

    prompt = _build()

    assert "No plan" not in prompt
    assert "**What I Attempted**" not in prompt


def test_synthesis_does_not_render_no_tools_placeholder() -> None:
    """``"No tools were successfully executed"`` placeholder is gone."""

    prompt = _build()

    assert "No tools were successfully executed" not in prompt
    assert "**Tools I Tried**" not in prompt


def test_synthesis_does_not_render_no_observations_placeholder() -> None:
    """``"No observations recorded"`` placeholder is gone."""

    prompt = _build()

    assert "No observations recorded" not in prompt
    assert "**Observations Made**" not in prompt


def test_synthesis_does_not_render_scratchpad_heading_or_placeholder() -> None:
    """Scratchpad heading and the hardcoded scratchpad placeholder are gone."""

    prompt = _build()

    assert "No detailed reasoning recorded" not in prompt
    assert "Reasoning History" not in prompt
    assert "scratchpad" not in prompt
    assert "scratchpad" not in prompt.lower()


def test_synthesis_does_not_render_complete_the_task_current_goal_fallback() -> None:
    """The ``current_goal`` literal ``"complete the task"`` fallback is gone."""

    prompt = _build()

    assert "complete the task" not in prompt
    assert "Got stuck trying to" not in prompt


def test_synthesis_renders_user_input_section_when_facts_message_present() -> None:
    """Phase 1: the verbatim user message renders under ``## User Input``."""

    goal = "Find every open SMB share on 10.0.0.0/24"
    prompt = _build(goal)

    assert "## User Input" in prompt
    assert goal in prompt


def test_synthesis_omits_loop_details_when_counters_are_zero() -> None:
    """``## Loop Details`` is fully omitted when both counters are zero."""

    prompt = _build(reflection_count=0, iterations=0)

    assert "## Loop Details" not in prompt
    assert "Reflection cycles" not in prompt
    assert "Total iterations" not in prompt


def test_synthesis_renders_loop_details_when_only_reflection_count_positive() -> None:
    """``## Loop Details`` renders only the reflection-cycles line when iterations==0."""

    prompt = _build(reflection_count=3, iterations=0)

    assert "## Loop Details" in prompt
    assert "Reflection cycles: 3" in prompt
    assert "Total iterations" not in prompt


def test_synthesis_renders_loop_details_when_only_iterations_positive() -> None:
    """``## Loop Details`` renders only the iterations line when reflection_count==0."""

    prompt = _build(reflection_count=0, iterations=7)

    assert "## Loop Details" in prompt
    assert "Total iterations: 7" in prompt
    assert "Reflection cycles" not in prompt


def test_synthesis_renders_loop_details_when_both_counters_positive() -> None:
    """``## Loop Details`` renders both counter lines when both are positive."""

    prompt = _build(reflection_count=3, iterations=7)

    assert "## Loop Details" in prompt
    assert "Reflection cycles: 3" in prompt
    assert "Total iterations: 7" in prompt


def test_synthesis_renders_only_required_sections_for_empty_state() -> None:
    """For a fully empty post-cleanup state, no legacy placeholder strings remain.

    This is the consolidated post-cleanup snapshot inverting the previous
    pre-cleanup characterization: the synthesis prompt body must keep
    none of the legacy placeholder strings.
    """

    prompt = _build()

    assert "No plan" not in prompt
    assert "No tools were successfully executed" not in prompt
    assert "No observations recorded" not in prompt
    assert "No detailed reasoning recorded" not in prompt
    assert "complete the task" not in prompt
    assert "Got stuck trying to" not in prompt
    assert "Reasoning History" not in prompt

    # The Phase 1 always-on user-input projection (sourced from
    # ``facts.message``) and the canonical ``## Your Task`` task tail
    # remain. ``## Loop Details`` is omitted because both counters are
    # zero.
    assert "## User Input" in prompt
    assert "## Loop Details" not in prompt
    assert "## Your Task" in prompt
