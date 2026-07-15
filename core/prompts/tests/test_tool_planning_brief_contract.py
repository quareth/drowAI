"""Prompt-contract tests for brief-driven tool-planning prompts.

Phase 2 Task 2.2 narrowed the direct-executor's ``select_tools`` and
``tool_parameters`` prompts away from recent-transcript ingestion and
onto the classifier-derived ``intent_brief``. Phase 3 Task
3.2 removed the transitional ``conversation_history_text`` kwarg
entirely — passing it now raises ``TypeError``. These tests lock the
new contract on the ``ToolPlanningPromptBuilder`` seam so a future
change cannot silently reintroduce transcript fanout into either
prompt.

Coverage:

- Happy path: a populated brief renders intent / overall goal / next
  operational goal / success condition / constraints / target in both
  prompts without any transcript-style markers.
- Empty-brief path: both prompts render valid, stable text when the
  brief is an empty mapping (caller not yet plumbed or classifier
  ambiguous).
- ``next_tool_hint`` override: subordinate corrective signal remains
  surfaced in both prompts.
- Post-cutover ``conversation_history_text`` guard: builders reject
  the removed kwarg with ``TypeError`` instead of silently dropping
  it.
- Scope guard: even if the caller planted execution-strategy /
  tool ids / parameter payload shaped values in the brief, the
  renderer must not surface them as execution decisions — the brief
  carries intent, not execution.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _populated_brief() -> Dict[str, Any]:
    return {
        "resolved_user_intent": "Scan open ports on 10.0.0.5",
        "overall_goal": "Map exposed service surface on 10.0.0.5",
        "continuation_mode": "new_request",
        "resolved_step_title": "Port Scan",
        "resolved_step_detail": "Establish the exposed TCP surface before selecting follow-up tools.",
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


_CATALOG = [{"id": "nmap.scan", "name": "nmap", "description": "scan ports"}]
_RESOLVED_TOOLS = [{"id": "nmap.scan", "reason": "port scan"}]

_TRANSCRIPT_MARKERS = (
    "<turn",
    "</turn>",
    "Recent History",
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
            f"transcript marker {marker!r} leaked into narrowed tool-planning "
            "prompt"
        )


# ---------------------------------------------------------------------------
# Happy path: populated brief renders intent into both prompts.
# ---------------------------------------------------------------------------


def test_select_tools_prompt_renders_brief_fields_without_transcript() -> None:
    builder = ToolPlanningPromptBuilder()
    brief = _populated_brief()

    prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={"max_tool_calls": 3},
        intent_brief=brief,
    )

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
    assert "multi_step" in prompt
    assert "10.0.0.5" in prompt

    # Available tools block still rendered (planner-owned, not brief-owned).
    assert "Available Tools:" in prompt
    assert "nmap.scan" in prompt

    _assert_no_transcript_markers(prompt)


def test_tool_parameters_prompt_renders_brief_fields_without_transcript() -> None:
    builder = ToolPlanningPromptBuilder()
    brief = _populated_brief()

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={"max_tool_calls": 3},
        intent_brief=brief,
        execution_strategy="parallel",
    )

    assert "Turn Execution Brief" in prompt
    assert brief["resolved_user_intent"] in prompt
    assert brief["overall_goal"] in prompt
    assert brief["resolved_step_title"] in prompt
    assert brief["resolved_step_detail"] in prompt
    assert brief["next_operational_goal"] in prompt
    assert brief["success_condition"] in prompt
    assert "ready" in prompt
    assert "No UDP scan" in prompt
    assert "multi_step" in prompt
    assert "10.0.0.5" in prompt

    # Planner-owned structure preserved.
    # Phase 3 Task 3.1 renamed the planner-facing label from "Selected Tools"
    # to "Candidate Tools" — the planner now commits a subset, it does not
    # re-execute every selection candidate.
    assert "Candidate Tools:" in prompt
    assert "nmap.scan" in prompt
    assert "Selector Decision" in prompt
    assert '"parallel"' in prompt
    assert "Task Context:" in prompt

    _assert_no_transcript_markers(prompt)


def test_tool_parameters_prompt_preserves_long_todo_progress_text() -> None:
    builder = ToolPlanningPromptBuilder()
    long_todo = (
        "Preserve the complete current operational objective text, including all "
        "named inputs, expected evidence, success condition, and follow-up handling "
        "details, so downstream prompt builders receive the full todo description "
        "instead of a permanently shortened fragment."
    )

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["example.tool"],
        target="lab-target",
        phase="execution",
        constraints={},
        todo_list=[{"description": long_todo, "status": "in_progress"}],
    )

    assert "**Todo Progress**:" in prompt
    assert long_todo in prompt
    assert "Preserve the complete current operational objective text..." not in prompt


def test_tool_parameters_prompt_omits_legacy_json_envelope_contract() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["shell.exec"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={"max_tool_calls": 3},
        intent_brief=_populated_brief(),
    )

    assert '"tool_calls"' not in prompt
    assert "deferred_followups" not in prompt
    assert "selection_rationale" not in prompt
    assert "Return strict JSON" not in prompt


def test_tool_parameters_prompt_omits_native_builder_policy() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["shell.exec"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={"max_tool_calls": 3},
        intent_brief=_populated_brief(),
    )

    assert "Current Turn Input:" in prompt
    assert "Candidate Tools:" in prompt
    assert "You are the native tool-call builder" not in prompt
    assert "Commit rules:" not in prompt
    assert "Candidate Tools list above" not in prompt
    assert "Turn Execution Brief above" not in prompt


def test_tool_parameters_system_prompt_contains_native_builder_policy() -> None:
    builder = ToolPlanningPromptBuilder()

    system_prompt = builder.build_tool_parameters_system_prompt(
        max_committed_tools_per_batch=3,
    )

    assert "You are the native tool-call builder" in system_prompt
    assert "between 1 and 3 candidate tool function(s)" in system_prompt
    assert "Candidate Tools section of the current turn input" in system_prompt
    assert "POST-TOOL REASONING DIRECTIVE section" in system_prompt
    assert "Turn Execution Brief section" in system_prompt
    assert "Tool Runbooks explain how listed tools work and what their parameters mean" in system_prompt
    assert "not as the authority for what operation to build" in system_prompt
    assert "Task Context, Current Goal, and Todo Progress" in system_prompt
    assert "Execution strategy" in system_prompt
    assert "not a mandatory multi-tool todo list" in system_prompt
    assert "repeat the same tool with different concrete parameters" in system_prompt
    assert "Per-call intent (`_builder_intent`)" in system_prompt
    assert "positive confirmation and negative failure indicators" in system_prompt
    assert "Candidate Tools list above" not in system_prompt
    assert "Turn Execution Brief above" not in system_prompt


def test_tool_parameters_prompt_renders_selector_decision_and_multiple_targets() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["information_gathering.network_discovery.nmap"],
        target="127.0.0.1",
        targets=["127.0.0.1", "172.0.0.1"],
        execution_strategy="parallel",
        phase="enumeration",
        constraints={"ports": "80"},
        intent_brief=_populated_brief(),
    )

    assert "Selector Decision" in prompt
    assert '"parallel"' in prompt
    assert "All targets for this turn" in prompt
    assert "127.0.0.1" in prompt
    assert "172.0.0.1" in prompt
    assert "not a mandatory execution list" in prompt


# ---------------------------------------------------------------------------
# Empty-brief path: prompts still render with "(none)" placeholders.
# ---------------------------------------------------------------------------


def test_select_tools_prompt_handles_empty_brief_gracefully() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief={},
    )

    assert "Turn Execution Brief" in prompt
    assert "(none)" in prompt
    assert "Available Tools:" in prompt
    _assert_no_transcript_markers(prompt)


def test_tool_parameters_prompt_handles_empty_brief_gracefully() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief={},
    )

    assert "Turn Execution Brief" in prompt
    assert "(none)" in prompt
    assert "Task Context:" in prompt
    _assert_no_transcript_markers(prompt)


def test_prompts_render_when_brief_is_none() -> None:
    """Builder must tolerate ``intent_brief=None`` during phase-3 rollout."""
    builder = ToolPlanningPromptBuilder()

    select_prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=None,
    )
    params_prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=None,
    )
    assert "Turn Execution Brief" in select_prompt
    assert "Turn Execution Brief" in params_prompt
    _assert_no_transcript_markers(select_prompt)
    _assert_no_transcript_markers(params_prompt)


# ---------------------------------------------------------------------------
# next_tool_hint subordinate-corrective signal is preserved.
# ---------------------------------------------------------------------------


def test_select_tools_prompt_preserves_next_tool_hint_override() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=_populated_brief(),
        next_tool_hint="PostgreSQL enumeration follow-up",
    )

    assert "CURRENT INTENT" in prompt
    assert "PostgreSQL enumeration follow-up" in prompt


def test_select_tools_prompt_renders_latest_phase_and_capability_surface() -> None:
    builder = ToolPlanningPromptBuilder()
    long_latest_phase_tail = "latest-phase-tail-" + ("x" * 2200)

    prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=_populated_brief(),
        latest_phase_memory=(
            "## Latest Current-Turn Phase\n"
            "<phase turn=5 phase=4 source=reflect>\n"
            "## Reflection\nUse a different immediate direction.\n"
            f"{long_latest_phase_tail}\n"
            "</phase>"
        ),
        capability_surface=(
            "- network_discovery: Discover hosts and ports. Visible tools: nmap.scan"
        ),
    )

    assert "Latest Current-Turn Phase" in prompt
    assert "<phase turn=5 phase=4 source=reflect>" in prompt
    assert long_latest_phase_tail in prompt
    assert "freshest runtime steering signal" in prompt
    assert "Available Agent Capability Surface" in prompt
    assert "network_discovery" in prompt
    assert "nmap.scan" in prompt
    _assert_no_transcript_markers(prompt)


def test_select_tools_prompt_examples_do_not_force_sequential_strategy() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=_populated_brief(),
        next_tool_hint=(
            "Run two independent service checks against the same target using "
            "separate concrete tool calls."
        ),
        max_committed_tools_per_batch=3,
    )

    assert '"execution_strategy":"parallel" | "sequential"' in prompt
    assert "Choose execution_strategy for the final committed batch" in prompt
    assert "Strategy is partly an efficiency decision" in prompt
    assert "The final batch may repeat the same selected tool" in prompt
    assert "Closed-world selection rule" in prompt
    assert "information already present in this prompt" in prompt
    assert "Do not choose adjacent tools" in prompt
    assert "return \"unavailable_capability\" as the only selected tool" in prompt
    assert "Do not use \"unavailable_capability\" for bad parameters" in prompt
    assert "<examples>" in prompt
    assert "Pattern: parallel, same tool" in prompt
    assert "Pattern: sequential, same tool" in prompt
    assert "Pattern: dependency, not batchable" in prompt
    assert '"execution_strategy":"sequential"}' not in prompt


def test_tool_parameters_system_prompt_defines_strategy_without_dependency_chaining() -> None:
    builder = ToolPlanningPromptBuilder()

    system_prompt = builder.build_tool_parameters_system_prompt(
        max_committed_tools_per_batch=3,
    )

    assert "<execution_strategy_guidance>" in system_prompt
    assert "<examples>" in system_prompt
    assert "Execution strategy is partly an efficiency decision" in system_prompt
    assert (
        "Parallel calls may use different tools or repeat the same tool with "
        "different concrete parameters"
    ) in system_prompt
    assert (
        "Sequential execution means independent, fully parameterized calls run in order"
    ) in system_prompt
    assert (
        "Do not treat sequential calls as dependent steps"
    ) in system_prompt
    assert "Pattern: parallel, same tool" in system_prompt
    assert "Pattern: sequential, different tools" in system_prompt
    assert "Pattern: dependency, not batchable" in system_prompt
    assert "for example, separate nmap calls" not in system_prompt
    assert "Do not fold independent parallel work" not in system_prompt


def test_tool_parameters_prompt_preserves_next_tool_hint_directive() -> None:
    builder = ToolPlanningPromptBuilder()

    system_prompt = builder.build_tool_parameters_system_prompt()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=_populated_brief(),
        next_tool_hint="run nmap -sV -p- 10.0.0.5",
    )

    assert "POST-TOOL REASONING DIRECTIVE" in prompt
    assert "run nmap -sV -p- 10.0.0.5" in prompt
    assert "defines the pending work for this iteration" in prompt
    assert "narrows the original Turn Execution Brief" in prompt
    assert "do not recommit successful current-turn work" in prompt
    assert (
        "If a POST-TOOL REASONING DIRECTIVE section is present, treat it as the "
        "highest-priority narrowed pending work"
    ) in system_prompt


def test_tool_parameters_prompt_has_no_terminal_phase_memory_contract() -> None:
    builder = ToolPlanningPromptBuilder()

    system_prompt = builder.build_tool_parameters_system_prompt()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="172.17.0.0/24",
        phase="deep_scan",
        constraints={},
        intent_brief=_populated_brief(),
        next_tool_hint="run nmap host discovery on 172.17.0.0/24",
        previous_tool="netdiscover",
        previous_tool_output_summary="Command failed because netdiscover executable was not found.",
        working_memory_summary=(
            "## Prior Current-Turn Phase Memory\n"
            "<phase turn=2 phase=0 source=tool>\n"
            "## Tool Executed\n"
            "Tool: netdiscover\n"
            "</phase>"
        ),
    )

    assert "Prior Current-Turn Phase Memory" in prompt
    assert "terminal_for_hypothesis" not in system_prompt
    assert "already satisfied" not in system_prompt


# ---------------------------------------------------------------------------
# Post-cutover: conversation_history_text kwarg is removed; callers must fail.
# ---------------------------------------------------------------------------


_FORBIDDEN_TRANSCRIPT = (
    "<turn n=1 role=user latest=true>\n"
    "SHOULD_NOT_APPEAR_IN_TOOL_PLANNING_PROMPT\n"
    "</turn>"
)


def test_select_tools_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """Builder rejects the removed ``conversation_history_text`` kwarg.

    Phase 3 Task 3.2 completed the cutover by dropping the transitional
    kwarg from every public builder method. Callers that still pass it
    must receive a ``TypeError`` so transcript reintroductions fail
    loudly rather than silently.
    """
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_select_tools_prompt(  # type: ignore[call-arg]
            resolved_tools=_RESOLVED_TOOLS,
            catalog=_CATALOG,
            target="10.0.0.5",
            phase="enumeration",
            constraints={},
            intent_brief=_populated_brief(),
            conversation_history_text=_FORBIDDEN_TRANSCRIPT,
        )


def test_tool_parameters_prompt_rejects_removed_conversation_history_text_kwarg() -> None:
    """Parameter builder also rejects the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_tool_parameters_prompt(  # type: ignore[call-arg]
            selected_tools=["nmap.scan"],
            target="10.0.0.5",
            phase="enumeration",
            constraints={},
            intent_brief=_populated_brief(),
            conversation_history_text=_FORBIDDEN_TRANSCRIPT,
        )


def test_system_prompts_reject_removed_conversation_history_text_kwarg() -> None:
    """Both system-prompt builders reject the removed kwarg."""
    builder = ToolPlanningPromptBuilder()
    with pytest.raises(TypeError):
        builder.build_system_prompt(  # type: ignore[call-arg]
            conversation_history_text=_FORBIDDEN_TRANSCRIPT,
        )
    with pytest.raises(TypeError):
        builder.build_select_tools_system_prompt(  # type: ignore[call-arg]
            conversation_history_text=_FORBIDDEN_TRANSCRIPT,
        )
    with pytest.raises(TypeError):
        builder.build_tool_parameters_system_prompt(  # type: ignore[call-arg]
            conversation_history_text=_FORBIDDEN_TRANSCRIPT,
        )


# ---------------------------------------------------------------------------
# Fix 3: relevant_memory_fragments render in tool-planning prompts.
# ---------------------------------------------------------------------------


def test_select_tools_prompt_renders_relevant_memory_fragments() -> None:
    """The shared brief block now surfaces ``relevant_memory_fragments``."""
    builder = ToolPlanningPromptBuilder()
    brief: Dict[str, Any] = dict(_populated_brief())
    brief["relevant_memory_fragments"] = [
        "mem-fragment-alpha",
        "mem-fragment-beta",
    ]

    prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=brief,
    )

    assert "Relevant memory fragments:" in prompt
    assert "mem-fragment-alpha" in prompt
    assert "mem-fragment-beta" in prompt


def test_tool_parameters_prompt_renders_relevant_memory_fragments() -> None:
    """Parameter-generation prompt also surfaces ``relevant_memory_fragments``."""
    builder = ToolPlanningPromptBuilder()
    brief: Dict[str, Any] = dict(_populated_brief())
    brief["relevant_memory_fragments"] = [
        "mem-fragment-alpha",
        "mem-fragment-beta",
    ]

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=brief,
    )

    assert "Relevant memory fragments:" in prompt
    assert "mem-fragment-alpha" in prompt
    assert "mem-fragment-beta" in prompt


# ---------------------------------------------------------------------------
# Scope guard: brief must not carry tool ids / execution strategy / params.
# ---------------------------------------------------------------------------


def test_brief_block_does_not_render_out_of_scope_execution_fields() -> None:
    """Even if a caller plants execution fields in the brief, they must not
    appear in the brief block as execution decisions.

    The brief carries intent / direction / constraints / target only.
    Tool ids, execution strategy, and parameter payloads are owned by
    downstream execution roles. A caller that accidentally planted them
    in the brief dict should not see them leak into the prompt body.
    """
    builder = ToolPlanningPromptBuilder()

    polluted_brief: Dict[str, Any] = dict(_populated_brief())
    polluted_brief["selected_tools"] = ["FORBIDDEN_TOOL_ID_IN_BRIEF"]
    polluted_brief["execution_strategy"] = "FORBIDDEN_STRATEGY_IN_BRIEF"
    polluted_brief["tool_parameters"] = {
        "FORBIDDEN_TOOL_ID_IN_BRIEF": {"ports": "FORBIDDEN_PORTS_IN_BRIEF"}
    }

    select_prompt = builder.build_select_tools_prompt(
        resolved_tools=_RESOLVED_TOOLS,
        catalog=_CATALOG,
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=polluted_brief,
    )
    params_prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
        intent_brief=polluted_brief,
    )

    for prompt in (select_prompt, params_prompt):
        assert "FORBIDDEN_TOOL_ID_IN_BRIEF" not in prompt
        assert "FORBIDDEN_STRATEGY_IN_BRIEF" not in prompt
        assert "FORBIDDEN_PORTS_IN_BRIEF" not in prompt
