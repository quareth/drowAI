"""Prompt-contract tests for the brief-driven DR planner prompt.

Phase 2 Task 2.3 narrows the deep-reasoning planner prompt
(``build_planning_prompt`` in ``core/prompts/constants.py``) away from
recent-transcript ingestion and onto the classifier-derived
``intent_brief``. These tests lock the new contract at the
builder seam so a future change cannot silently reintroduce transcript
fanout into the DR planner prompt.

Coverage:

- Happy path: a populated brief renders intent / overall goal / next
  operational goal / success condition / constraints / target in the
  planner prompt without any transcript-style markers.
- Empty-brief path: the prompt renders valid, stable text when the
  brief is an empty mapping (caller not yet plumbed or classifier
  ambiguous).
- Clarified inputs / scope constraints / environment / available tools
  sections are preserved intact when passed non-trivially.
- Removed-kwarg guard: Phase 3 Task 3.3 deleted the transitional
  ``history_section`` kwarg entirely; passing it now raises
  ``TypeError`` so any caller that still plumbs transcript text into
  the DR planner breaks loudly at the seam.
- Scope guard: even if a caller plants tool-planning execution fields
  in the brief, they must not surface in the DR planner prompt body.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from core.prompts.constants import (
    build_planner_scope_constraints,
    build_planner_system_prompt,
    build_planner_tools_constraint,
    build_planning_prompt,
    build_scope_boundary_warnings,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _populated_brief() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 10.0.0.5",
        "overall_goal": "Map exposed service surface on 10.0.0.5",
        "continuation_mode": "new_request",
        "resolved_step_title": "Port Scan",
        "resolved_step_detail": "Establish the exposed TCP surface before deeper enumeration.",
        "next_operational_goal": "Run TCP port scan on 10.0.0.5",
        "success_condition": "Return list of open TCP ports with service banners",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "explicit_constraints": ["No UDP scan", "Avoid noisy syn-flood"],
        "relevant_memory_fragments": [
            "prior finding: 10.0.0.5 responds to ICMP",
            "prior finding: ssh banner observed on 22",
        ],
        "retrieval_hints": ["tcp scan", "service detection"],
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
            f"transcript marker {marker!r} leaked into narrowed DR planner prompt"
        )


def test_planner_system_prompt_prevents_unnecessary_validation_steps() -> None:
    prompt = build_planner_system_prompt("")

    assert "Plan only the work required to satisfy the current user request" in prompt
    assert "Do not add validation, confirmation, sanity-check" in prompt
    assert "Do not assume that a user-provided target" in prompt


# ---------------------------------------------------------------------------
# Happy path: populated brief renders intent into the DR planner prompt.
# ---------------------------------------------------------------------------


def test_dr_planner_prompt_renders_brief_fields_without_transcript() -> None:
    brief = _populated_brief()

    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief=brief,
    )

    assert "DR Planner Input Brief" in prompt
    assert brief["resolved_user_intent"] in prompt
    assert brief["overall_goal"] in prompt
    assert brief["resolved_step_title"] in prompt
    assert brief["resolved_step_detail"] in prompt
    assert brief["next_operational_goal"] in prompt
    assert brief["success_condition"] in prompt
    assert "ready" in prompt  # execution_readiness
    assert "No UDP scan" in prompt
    assert "Avoid noisy syn-flood" in prompt
    assert "prior finding: 10.0.0.5 responds to ICMP" in prompt
    assert "prior finding: ssh banner observed on 22" in prompt
    assert "multi_step" in prompt
    assert "all_steps_done" in prompt
    assert "10.0.0.5" in prompt  # target.resolved_target
    assert "explicit_current_message" in prompt
    assert "new_request" in prompt  # continuation_mode rendered
    # Planner-owned planning-task/contract structure preserved.
    assert '"mode": "plan_ready" | "clarify_required"' in prompt
    assert "missing mandatory user inputs" in prompt

    _assert_no_transcript_markers(prompt)


# ---------------------------------------------------------------------------
# Empty-brief path: the prompt still renders with "(none)" placeholders.
# ---------------------------------------------------------------------------


def test_dr_planner_prompt_handles_empty_brief_gracefully() -> None:
    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief={},
    )

    assert "DR Planner Input Brief" in prompt
    assert "(none)" in prompt
    # Task structure still rendered.
    assert '"mode": "plan_ready" | "clarify_required"' in prompt
    _assert_no_transcript_markers(prompt)


def test_dr_planner_prompt_handles_none_brief() -> None:
    """Builder must tolerate ``intent_brief=None`` during phase-3 rollout."""
    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief=None,
    )

    assert "DR Planner Input Brief" in prompt
    assert "(none)" in prompt
    _assert_no_transcript_markers(prompt)


# ---------------------------------------------------------------------------
# Clarified inputs / scope constraints / environment / available tools
# sections are preserved intact.
# ---------------------------------------------------------------------------


def test_dr_planner_prompt_preserves_clarified_inputs_section() -> None:
    clarified = (
        "\n\n**Clarified Required Inputs**:\n"
        "- target: 10.0.0.5\n"
        "Use these answers when forming the plan."
    )
    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief=_populated_brief(),
        clarified_inputs_section=clarified,
    )

    assert "Clarified Required Inputs" in prompt
    assert "- target: 10.0.0.5" in prompt
    _assert_no_transcript_markers(prompt)


def test_dr_planner_prompt_preserves_tools_constraint() -> None:
    tools_constraint = build_planner_tools_constraint("nmap, ncat, curl")

    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint=tools_constraint,
        scope_constraints="",
        intent_brief=_populated_brief(),
    )

    assert "**Available Tools**" in prompt
    assert "nmap, ncat, curl" in prompt
    assert "Only plan steps that can be executed with the available tools listed above" in prompt
    _assert_no_transcript_markers(prompt)


def test_dr_planner_prompt_preserves_scope_constraints() -> None:
    scope_constraints = build_planner_scope_constraints(
        goals_str="scan tcp ports",
        boundaries_str="no_exploitation",
        conditional_str="None",
        explicit_tools_str="nmap",
        boundary_warnings=build_scope_boundary_warnings(["no_exploitation"]),
    )

    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints=scope_constraints,
        intent_brief=_populated_brief(),
    )

    assert "**Scope Constraints**" in prompt
    assert "scan tcp ports" in prompt
    assert "no_exploitation" in prompt
    assert "CRITICAL RESTRICTIONS - DO NOT VIOLATE" in prompt
    _assert_no_transcript_markers(prompt)


def test_dr_planner_prompt_preserves_network_discovery_section() -> None:
    network_section = (
        "\n\n**Target Selection Guidance**:\n"
        "- Targets are not pre-specified for this request.\n"
        "- Start with concrete host discovery to identify reachable hosts.\n"
    )

    prompt = build_planning_prompt(
        targets_str="not specified",
        network_discovery_section=network_section,
        tools_constraint="",
        scope_constraints="",
        intent_brief=_populated_brief(),
    )

    assert "Target Selection Guidance" in prompt
    assert "Start with concrete host discovery" in prompt
    _assert_no_transcript_markers(prompt)


# ---------------------------------------------------------------------------
# Removed-kwarg guard: the transitional ``history_section`` kwarg is gone.
# ---------------------------------------------------------------------------


def test_build_planning_prompt_rejects_removed_history_section_kwarg() -> None:
    """Phase 3 Task 3.3 removed the transitional ``history_section`` kwarg.

    Passing it must now raise ``TypeError`` so any residual caller that
    tries to feed transcript text into the DR planner fails loudly at
    the builder seam instead of silently being dropped.
    """
    residual_transcript = (
        "\n\n**Conversation (oldest -> newest, act on the turn tagged "
        "latest=true)**:\n"
        "<turn n=1 role=user latest=true>\n"
        "RESIDUAL_TRANSCRIPT_SHOULD_NOT_APPEAR\n"
        "</turn>\n"
    )

    with pytest.raises(TypeError):
        build_planning_prompt(  # type: ignore[call-arg]
            targets_str="10.0.0.5",
            network_discovery_section="",
            tools_constraint="",
            scope_constraints="",
            intent_brief=_populated_brief(),
            history_section=residual_transcript,
        )


# ---------------------------------------------------------------------------
# Scope guard: brief must not carry tool ids / execution strategy / params.
# ---------------------------------------------------------------------------


def test_dr_planner_brief_block_rejects_out_of_scope_execution_fields() -> None:
    """Even if a caller plants execution fields in the brief, they must not
    appear in the DR planner prompt body as execution decisions.

    The brief carries intent / direction / constraints / target only.
    Tool ids, execution strategy, and parameter payloads are owned by
    downstream execution roles.
    """
    polluted_brief: Dict[str, Any] = dict(_populated_brief())
    polluted_brief["selected_tools"] = ["FORBIDDEN_TOOL_ID_IN_BRIEF"]
    polluted_brief["tool_ids"] = ["FORBIDDEN_TOOL_ID_IN_BRIEF"]
    polluted_brief["execution_strategy"] = "FORBIDDEN_STRATEGY_IN_BRIEF"
    polluted_brief["parameters"] = {
        "FORBIDDEN_TOOL_ID_IN_BRIEF": {"ports": "FORBIDDEN_PORTS_IN_BRIEF"}
    }

    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief=polluted_brief,
    )

    assert "FORBIDDEN_TOOL_ID_IN_BRIEF" not in prompt
    assert "FORBIDDEN_STRATEGY_IN_BRIEF" not in prompt
    assert "FORBIDDEN_PORTS_IN_BRIEF" not in prompt


# ---------------------------------------------------------------------------
# Structural: the renamed prompt header reflects the brief-driven contract.
# ---------------------------------------------------------------------------


def test_dr_planner_prompt_no_longer_references_conversation_section() -> None:
    """Structural guard: the legacy ``Conversation (oldest -> newest, ...)``
    header must not appear in the new DR planner prompt body."""
    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief=_populated_brief(),
    )

    assert "Conversation (oldest -> newest" not in prompt
    assert "Use this conversation context" not in prompt


def test_dr_planner_prompt_teaches_objective_level_planning() -> None:
    prompt = build_planning_prompt(
        targets_str="172.17.0.0/16",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
        intent_brief=_populated_brief(),
    )

    assert "Keep the plan objective-level rather than command-level." in prompt
    assert "Do not include exact commands, flags, file names" in prompt
    assert "Bad step style" in prompt
    assert "Run arp-scan on 172.17.0.0/16" in prompt


def test_dr_planner_system_prompt_keeps_stable_planner_authority_rules() -> None:
    system_prompt = build_planner_system_prompt(
        "Container Environment:\n- Interface eth0: 172.17.0.2/16"
    )

    assert "deep-reasoning planner" in system_prompt
    assert "Plan at the objective level, not the command level." in system_prompt
    assert "You are not responsible for:" in system_prompt
    assert "choosing exact commands" in system_prompt
    assert "Kali Linux container" in system_prompt
