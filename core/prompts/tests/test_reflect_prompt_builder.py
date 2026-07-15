"""Builder-level smoke tests for ``core.prompts.builders.reflect``.

These tests exercise the new ``build_reflection_prompt`` module surface
in isolation (no graph node wiring). They verify:

- The module imports cleanly.
- The internal local-blocker intro framing renders.
- ``## Stuck Pattern`` and ``## Recent Decisions`` are conditional and
  omitted when their inputs are empty / ``None``.
- ``## Stuck Pattern`` renders the ``problem`` body verbatim.
- ``## Recent Decisions`` renders one ``- {entry}`` line per entry.
- ``## Relevant Prior Findings`` renders when ``relevant_findings`` is
  non-empty (delegated through the shared composer).
- The ``## Your Task`` JSON tail always renders and preserves the
  structured-output contract (``root_cause`` / ``alternative_approaches``).
- Top-level reflect section headings use ``## Header`` form, never the
  legacy ``**Header**:`` bold-line form. The inner task-tail subheading
  ``**Required Response Format**:`` is preserved as output-contract text.
- The legacy placeholder strings ``"No plan"``, ``"None"``, and
  ``"No reasoning recorded"`` never appear.
"""

from __future__ import annotations

import re

from core.prompts.builders.reflect import (
    build_reflection_fallback_guidance,
    build_reflection_prompt,
)


def _empty_state() -> dict:
    return {"facts": {}}


def test_module_imports_cleanly():
    # If we got here, ``from core.prompts.builders.reflect import
    # build_reflection_prompt`` succeeded at module load.
    assert callable(build_reflection_prompt)
    assert callable(build_reflection_fallback_guidance)


def test_empty_inputs_render_intro_and_task_tail_only():
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=None,
    )

    # Intro framing is local/internal rather than broad user-facing replanning.
    assert "Internal agent steering" in prompt
    assert "active todo/current blocker" in prompt
    assert (
        "NOTE: Tool failures are handled automatically by immediate retry logic. "
        "This reflection is about why the current path is not producing progress "
        "for the active blocker" in prompt
    )

    # Task tail always renders and preserves the JSON contract.
    assert "## Your Task" in prompt
    assert "**Required Response Format**:" in prompt
    assert "```json" in prompt
    assert "root_cause" in prompt
    assert "alternative_approaches" in prompt
    assert "updated_plan" not in prompt
    assert "not a full plan rewrite" in prompt

    # Conditional reflect-only sections must NOT render with empty inputs.
    assert "## Stuck Pattern" not in prompt
    assert "## Recent Decisions" not in prompt

    # Legacy placeholder strings must never appear.
    assert "No plan" not in prompt
    assert "No reasoning recorded" not in prompt
    assert "**Current Reasoning**" not in prompt
    # ``"None"`` (the literal placeholder, not the substring of "alternative" etc.).
    # Be defensive: ensure the legacy ``"\nNone\n"`` placeholder shape is absent.
    assert "\nNone\n" not in prompt


def test_problem_renders_stuck_pattern_section_verbatim():
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="Decision paralysis: same decision repeated 3+ times",
        recent_decisions=None,
    )

    assert "## Stuck Pattern\nDecision paralysis: same decision repeated 3+ times" in prompt


def test_empty_problem_string_omits_stuck_pattern():
    prompt_blank = build_reflection_prompt(
        _empty_state(),
        problem="   ",
        recent_decisions=None,
    )
    prompt_empty = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=None,
    )

    assert "## Stuck Pattern" not in prompt_blank
    assert "## Stuck Pattern" not in prompt_empty


def test_recent_decisions_renders_bulleted_list():
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=["a", "b"],
    )

    assert "## Recent Decisions\n- a\n- b" in prompt


def test_empty_recent_decisions_omits_section():
    prompt_none = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=None,
    )
    prompt_empty = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=[],
    )

    assert "## Recent Decisions" not in prompt_none
    assert "## Recent Decisions" not in prompt_empty


def test_builder_does_not_reslice_recent_decisions():
    decisions = [f"d{i}" for i in range(8)]
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=decisions,
    )

    # All 8 entries render — the builder is not allowed to slice.
    for entry in decisions:
        assert f"- {entry}" in prompt


def test_relevant_findings_renders_section_via_shared_composer():
    finding = {
        "subject": "10.0.0.5",
        "summary": "Open port 443/tcp",
        "tool": "nmap",
        "tags": ["service"],
    }
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=None,
        relevant_findings=[finding],
    )

    assert "## Relevant Prior Findings" in prompt


def test_capability_surface_renders_via_shared_composer():
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=None,
        capability_surface=(
            "- exploitation_framework: Use exploit frameworks. Visible tools: exploitation_tools.metasploit.run_exploit"
        ),
    )

    assert "## Agent Operational Capability Surface" in prompt
    assert "exploitation_framework" in prompt
    assert "exploitation_tools.metasploit.run_exploit" in prompt


def test_no_legacy_bold_line_top_level_section_headings():
    """Top-level reflect sections must use ``## Header`` not ``**Header**:``."""
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="Stuck in loop",
        recent_decisions=["a"],
    )

    # No legacy bold-line top-level reflect section headings.
    forbidden_top_level = [
        "**Stuck Pattern Identified**:",
        "**Current Plan**:",
        "**Todo List**:",
        "**Recent Decisions**:",
        "**Relevant Prior Findings**:",
        "**Current Reasoning** (scratchpad):",
        "**Your Task**:",
    ]
    for literal in forbidden_top_level:
        assert literal not in prompt, f"Legacy bold-line heading {literal!r} should not appear"

    # Inner task-tail subheading is preserved as output-contract text.
    assert "**Required Response Format**:" in prompt


def test_task_tail_always_renders_even_with_no_other_sections():
    prompt = build_reflection_prompt(
        _empty_state(),
        problem="",
        recent_decisions=None,
    )

    # The ``## Your Task`` heading must be present and the JSON code fence
    # must close.
    assert prompt.count("## Your Task") == 1
    json_fences = re.findall(r"```json", prompt)
    assert len(json_fences) == 1


def test_fallback_guidance_is_prompt_owned_and_avoids_tool_catalog_language():
    guidance = build_reflection_fallback_guidance(
        "Active todo stalled without meaningful progress"
    )

    assert "Reflection fallback triggered." in guidance
    assert "reflection LLM call failed" in guidance
    assert "Current stuck pattern:" in guidance
    assert "Active todo stalled without meaningful progress" in guidance
    assert "Assume the current direction is not working." in guidance
    assert "information currently available" in guidance
    assert "finalize/synthesize" in guidance
    assert "available tool" not in guidance
    assert "tool catalog" not in guidance
