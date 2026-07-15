"""Prompt-contract tests for ``build_tool_category_selection_prompt``.

Phase 2 Task 2.1 narrowed the category selector prompt away from full
recent-transcript ingestion toward the classifier-derived
``intent_brief``. Phase 3 Task 3.1 then removed the transitional
``history_text`` parameter entirely: no wired caller still forwards
transcript text, so the builder rejects it outright. These tests lock
the new contract so future work cannot silently reintroduce transcript
fanout into this seam.

Coverage:

- Happy path: a populated brief renders intent/goal text and avoids
  any transcript-style markers.
- Empty brief: the builder still renders a valid prompt with no
  crashes and uses ``(none)`` placeholders.
- ``next_tool_hint`` override: subordinate corrective signal remains
  surfaced in the prompt.
- Removed-parameter guard: passing ``history_text=...`` now raises
  ``TypeError`` on the unknown kwarg; this is the Phase 3 cutover
  guardrail that catches any regression that tries to reintroduce
  transcript plumbing.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from core.prompts.constants import build_tool_category_selection_prompt


_CATEGORIES_TEXT = (
    "  - information_gathering: Network recon.\n"
    "  - database_assessment: Database testing."
)


def _populated_brief() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 10.0.0.5",
        "overall_goal": "Map exposed service surface on 10.0.0.5",
        "continuation_mode": "new_request",
        "resolved_step_title": "Port Scan",
        "resolved_step_detail": "Establish the exposed TCP surface for the target host.",
        "next_operational_goal": "Run TCP port scan on 10.0.0.5",
        "success_condition": "Return list of open TCP ports with service banners",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "explicit_constraints": ["No UDP scan", "Avoid noisy syn-flood"],
        "suggested_category_focus": ["information_gathering"],
        "retrieval_hints": ["tcp scan", "service detection"],
        "relevant_memory_fragments": ["prior finding: 10.0.0.5 responds to ICMP"],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        "resolved_target": "10.0.0.5",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
    }


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_TRANSCRIPT_MARKERS = (
    "<turn",
    "</turn>",
    "Recent History",
    "recent_transcript",
    "assistant reply",
    "role=user",
    "role=assistant",
    "latest=true",
)


def _assert_no_transcript_markers(prompt: str) -> None:
    for marker in _TRANSCRIPT_MARKERS:
        assert marker not in prompt, (
            f"transcript marker {marker!r} leaked into the narrowed "
            "category-selector prompt"
        )


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_builder_renders_brief_fields_without_transcript() -> None:
    """Happy path: brief content appears; transcript markers do not."""
    brief = _populated_brief()

    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief=brief,
        next_tool_hint=None,
    )

    # Core brief fields rendered.
    assert "Turn Execution Brief" in prompt
    assert brief["resolved_user_intent"] in prompt
    assert brief["overall_goal"] in prompt
    assert brief["resolved_step_title"] in prompt
    assert brief["resolved_step_detail"] in prompt
    assert brief["next_operational_goal"] in prompt
    assert brief["success_condition"] in prompt
    assert "ready" in prompt
    assert "No UDP scan" in prompt
    assert "Avoid noisy syn-flood" in prompt
    assert "information_gathering" in prompt  # suggested_category_focus bullet
    assert "tcp scan" in prompt  # retrieval hint
    assert "multi_step" in prompt  # request_contract.question_type
    assert "10.0.0.5" in prompt  # resolved_target

    # No transcript surfaces.
    _assert_no_transcript_markers(prompt)


def test_builder_handles_empty_brief_gracefully() -> None:
    """Empty / missing brief still renders a valid prompt with placeholders."""
    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief={},
        next_tool_hint=None,
    )

    # Structural anchors remain intact.
    assert "Turn Execution Brief" in prompt
    assert "Available Tool Categories:" in prompt
    assert "Return ONLY valid JSON" in prompt

    # Empty-field placeholder appears.
    assert "(none)" in prompt

    # No transcript surfaces.
    _assert_no_transcript_markers(prompt)


def test_builder_preserves_next_tool_hint_override() -> None:
    """``next_tool_hint`` keeps its role as a subordinate corrective signal."""
    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief=_populated_brief(),
        next_tool_hint="PostgreSQL enumeration follow-up",
    )

    assert "CURRENT INTENT" in prompt
    assert "PostgreSQL enumeration follow-up" in prompt


def test_builder_renders_latest_phase_memory_with_precedence() -> None:
    """Latest phase memory is runtime steering, not full transcript context."""
    long_latest_phase_tail = "category-latest-phase-tail-" + ("x" * 2200)
    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief=_populated_brief(),
        latest_phase_memory=(
            "## Latest Current-Turn Phase\n"
            "<phase turn=2 phase=3 source=reflect>\n"
            "## Reflection\nChange direction locally.\n"
            f"{long_latest_phase_tail}\n"
            "</phase>"
        ),
    )

    assert "Latest Current-Turn Phase" in prompt
    assert "<phase turn=2 phase=3 source=reflect>" in prompt
    assert long_latest_phase_tail in prompt
    assert "freshest runtime steering signal" in prompt
    assert "Turn Execution Brief remains authoritative" in prompt
    assert "<phase turn=2 phase=2" not in prompt


def test_latest_phase_precedence_downgrades_stale_next_tool_hint() -> None:
    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief=_populated_brief(),
        next_tool_hint="Older PTR directive",
        latest_phase_memory=(
            "## Latest Current-Turn Phase\n"
            "<phase turn=2 phase=4 source=reflect>\n"
            "## Reflection\nNewest correction.\n"
            "</phase>"
        ),
    )

    assert prompt.index("Latest Current-Turn Phase") < prompt.index("CURRENT INTENT")
    assert "subordinate to Latest Current-Turn Phase" in prompt
    assert "HIGHEST PRIORITY" not in prompt


def test_builder_renders_relevant_memory_fragments_from_brief() -> None:
    """Fix 3: shared brief block renders ``relevant_memory_fragments``.

    Prior to the fix, only the DR planner's dedicated renderer surfaced
    memory fragments. The shared ``_render_brief_block`` that drives the
    category selector prompt now also renders the fragments so the
    selector sees the same classifier-derived memory grounding.
    """
    brief: Dict[str, Any] = dict(_populated_brief())
    brief["relevant_memory_fragments"] = [
        "mem-fragment-alpha",
        "mem-fragment-beta",
    ]

    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief=brief,
        next_tool_hint=None,
    )

    assert "Relevant memory fragments:" in prompt
    assert "mem-fragment-alpha" in prompt
    assert "mem-fragment-beta" in prompt


def test_builder_renders_placeholder_when_relevant_memory_fragments_missing() -> None:
    """Fix 3: missing ``relevant_memory_fragments`` renders ``(none)`` placeholder."""
    brief: Dict[str, Any] = dict(_populated_brief())
    brief.pop("relevant_memory_fragments", None)

    prompt = build_tool_category_selection_prompt(
        categories_text=_CATEGORIES_TEXT,
        intent_brief=brief,
        next_tool_hint=None,
    )

    assert "Relevant memory fragments:" in prompt
    # The immediate bullet under the label renders as "(none)" when empty.
    memory_index = prompt.index("Relevant memory fragments:")
    remainder = prompt[memory_index : memory_index + 200]
    assert "(none)" in remainder


def test_builder_rejects_removed_history_text_kwarg() -> None:
    """Phase 3 Task 3.1 removes the ``history_text`` parameter.

    After the cutover, the only way turn-interpretation reaches this
    builder is via ``intent_brief``. A caller that still tries
    to pass ``history_text=...`` indicates a stale wiring path and must
    fail fast so the regression is caught at call time rather than
    silently producing a transcript-contaminated prompt.
    """
    forbidden_transcript = (
        "<turn n=1 role=user latest=true>\n"
        "RESIDUAL_TRANSCRIPT_SHOULD_NOT_APPEAR\n"
        "</turn>"
    )

    with pytest.raises(TypeError):
        build_tool_category_selection_prompt(  # type: ignore[call-arg]
            categories_text=_CATEGORIES_TEXT,
            intent_brief=_populated_brief(),
            next_tool_hint=None,
            history_text=forbidden_transcript,
        )
