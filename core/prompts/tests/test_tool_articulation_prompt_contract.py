"""Prompt-contract tests for the brief-driven tool articulation prompt.

Phase 2 Task 2.4 narrowed the tool articulation prompt
(``build_tool_articulation_prompt`` in ``core/prompts/constants.py``)
away from recent-transcript ingestion and onto the classifier-derived
``intent_brief``. Phase 3 Task 3.4 finishes the cutover by
removing the transitional ``conversation_context`` kwarg entirely:
the builder now rejects it with ``TypeError``. These tests lock the
post-cutover contract at the builder seam so a future change cannot
silently reintroduce transcript fanout into the articulation prompt.

Coverage:

- Happy path: a populated brief renders intent / next operational
  goal / success condition / constraints / target in the articulation
  prompt without any transcript-style markers, and the selected tool
  and resolved parameters still appear.
- Empty-brief path: the prompt renders valid, stable text when the
  brief is an empty mapping (caller not yet plumbed or classifier
  ambiguous).
- Post-cutover ``conversation_context`` guard: the builder rejects
  the kwarg outright — no transcript text can reach the prompt even
  by misuse.
- Scope guard: even if a caller plants tool-planning execution
  fields in the brief, they must not surface in the articulation
  prompt body.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from core.prompts.constants import build_tool_articulation_prompt


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _populated_brief() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 10.0.0.5",
        "overall_goal": "Map exposed service surface on 10.0.0.5",
        "continuation_mode": "new_request",
        "resolved_step_title": "Port Scan",
        "resolved_step_detail": "Establish the exposed TCP surface for the active target.",
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


_TRANSCRIPT_MARKERS = (
    "<turn",
    "</turn>",
    "Recent conversation",
    "recent_transcript",
    "assistant reply",
    "role=user",
    "role=assistant",
    "latest=true",
    "Conversation (oldest -> newest",
)


def _assert_no_transcript_markers(prompt: str) -> None:
    for marker in _TRANSCRIPT_MARKERS:
        assert marker not in prompt, (
            f"transcript marker {marker!r} leaked into narrowed tool "
            "articulation prompt"
        )


# ---------------------------------------------------------------------------
# Happy path: populated brief renders intent into the articulation prompt.
# ---------------------------------------------------------------------------


def test_articulation_prompt_renders_brief_fields_without_transcript() -> None:
    brief = _populated_brief()

    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{'target': '10.0.0.5', 'ports': '1-1024'}",
        intent_brief=brief,
    )

    # Brief block is present and carries intent / direction / constraints.
    assert "Turn Execution Brief" in prompt
    assert brief["resolved_user_intent"] in prompt
    assert brief["resolved_step_title"] in prompt
    assert brief["resolved_step_detail"] in prompt
    assert brief["next_operational_goal"] in prompt
    assert brief["success_condition"] in prompt
    assert "No UDP scan" in prompt
    assert "Avoid noisy syn-flood" in prompt
    assert "multi_step" in prompt
    assert "all_steps_done" in prompt
    # Target fields surfaced.
    assert "10.0.0.5" in prompt
    assert "explicit_current_message" in prompt
    # Selected tool + resolved params rendered in decision section.
    assert "nmap.scan" in prompt
    assert "1-1024" in prompt
    # Articulation instructions preserved (wording unchanged).
    assert "To [achieve user's goal], I will..." in prompt
    assert "1-2 sentences" in prompt

    _assert_no_transcript_markers(prompt)


# ---------------------------------------------------------------------------
# Empty-brief path: the prompt still renders with "(none)" placeholders.
# ---------------------------------------------------------------------------


def test_articulation_prompt_handles_empty_brief_gracefully() -> None:
    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{}",
        intent_brief={},
    )

    assert "Turn Execution Brief" in prompt
    assert "(none)" in prompt
    # Selected tool still in the body.
    assert "nmap.scan" in prompt
    # Articulation wording preserved.
    assert "To [achieve user's goal], I will..." in prompt
    _assert_no_transcript_markers(prompt)


def test_articulation_prompt_handles_none_brief() -> None:
    """Builder must tolerate ``intent_brief=None`` during phase-3 rollout."""
    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{}",
        intent_brief=None,
    )

    assert "Turn Execution Brief" in prompt
    assert "(none)" in prompt
    _assert_no_transcript_markers(prompt)


# ---------------------------------------------------------------------------
# Post-cutover: the transitional conversation_context kwarg is rejected.
# ---------------------------------------------------------------------------


_RESIDUAL_TRANSCRIPT = (
    "<turn n=1 role=user latest=true>\n"
    "RESIDUAL_TRANSCRIPT_SHOULD_NOT_APPEAR\n"
    "</turn>\n"
)


def test_articulation_prompt_rejects_transitional_conversation_context() -> None:
    """Phase 3 Task 3.4: the builder must fail fast on transcript revival.

    After the cutover the builder no longer accepts the transitional
    ``conversation_context`` kwarg. Any caller attempting to hand in
    transcript text must receive a ``TypeError`` so the regression is
    loud, not silent.
    """
    with pytest.raises(TypeError):
        build_tool_articulation_prompt(  # type: ignore[call-arg]
            selected_tool="nmap.scan",
            tool_params="{}",
            intent_brief=_populated_brief(),
            conversation_context=_RESIDUAL_TRANSCRIPT,
        )


# ---------------------------------------------------------------------------
# Runtime-state slice (non-transcript) still threads through.
# ---------------------------------------------------------------------------


def test_articulation_prompt_preserves_runtime_state_block() -> None:
    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{}",
        intent_brief=_populated_brief(),
        runtime_state="active_target: {'value': '10.0.0.5', 'kind': 'ip'}",
    )

    assert "Runtime State:" in prompt
    assert "active_target" in prompt
    assert "10.0.0.5" in prompt
    _assert_no_transcript_markers(prompt)


# ---------------------------------------------------------------------------
# Scope guard: brief must not carry tool ids / execution strategy / params.
# ---------------------------------------------------------------------------


def test_articulation_brief_block_rejects_out_of_scope_execution_fields() -> None:
    """Even if a caller plants execution fields in the brief, they must not
    appear in the articulation prompt body as execution decisions.

    The brief carries intent / direction / constraints / target only.
    Tool ids, execution strategy, and parameter payloads are owned by
    downstream execution roles; the articulation prompt receives the
    real ``selected_tool`` + ``tool_params`` separately from the brief.
    """
    polluted_brief: Dict[str, Any] = dict(_populated_brief())
    polluted_brief["selected_tools"] = ["FORBIDDEN_TOOL_ID_IN_BRIEF"]
    polluted_brief["tool_ids"] = ["FORBIDDEN_TOOL_ID_IN_BRIEF"]
    polluted_brief["execution_strategy"] = "FORBIDDEN_STRATEGY_IN_BRIEF"
    polluted_brief["parameters"] = {
        "FORBIDDEN_TOOL_ID_IN_BRIEF": {"ports": "FORBIDDEN_PORTS_IN_BRIEF"}
    }

    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{'target': '10.0.0.5'}",
        intent_brief=polluted_brief,
    )

    assert "FORBIDDEN_TOOL_ID_IN_BRIEF" not in prompt
    assert "FORBIDDEN_STRATEGY_IN_BRIEF" not in prompt
    assert "FORBIDDEN_PORTS_IN_BRIEF" not in prompt


# ---------------------------------------------------------------------------
# Fix 3: relevant_memory_fragments render in the articulation prompt.
# ---------------------------------------------------------------------------


def test_articulation_prompt_renders_relevant_memory_fragments() -> None:
    """The shared brief block now surfaces ``relevant_memory_fragments``."""
    brief: Dict[str, Any] = dict(_populated_brief())
    brief["relevant_memory_fragments"] = [
        "mem-fragment-alpha",
        "mem-fragment-beta",
    ]

    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{}",
        intent_brief=brief,
    )

    assert "Relevant memory fragments:" in prompt
    assert "mem-fragment-alpha" in prompt
    assert "mem-fragment-beta" in prompt


# ---------------------------------------------------------------------------
# Structural: the renamed prompt header reflects the brief-driven contract.
# ---------------------------------------------------------------------------


def test_articulation_prompt_no_longer_references_conversation_section() -> None:
    """Structural guard: the legacy ``Conversation (oldest -> newest, ...)``
    header must not appear in the new articulation prompt body."""
    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params="{}",
        intent_brief=_populated_brief(),
    )

    assert "Conversation (oldest -> newest" not in prompt
    assert "grounded in the latest user turn" not in prompt
    # New header reflects the brief-driven contract.
    assert "grounded in the Turn Execution Brief below" in prompt
