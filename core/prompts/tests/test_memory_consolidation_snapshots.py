"""Phase 0 characterization snapshots for memory-consolidation work.

These tests lock current prompt outputs for memory-relevant builders so
Phase 1+ can prove structure-only refactors do not change prompt text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils import iteration_memory as _iteration_memory
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder
from core.prompts.constants import (
    build_planning_prompt,
    build_tool_articulation_prompt,
    build_tool_category_selection_prompt,
)
from core.prompts.tests._golden import assert_golden


_UNIFIED_INTENT_BRIEF_KEYS = {
    "blocking_reason",
    "continuation_mode",
    "execution_readiness",
    "explicit_constraints",
    "next_operational_goal",
    "overall_goal",
    "relevant_memory_fragments",
    "request_contract",
    "resolved_step_detail",
    "resolved_step_title",
    "resolved_target",
    "resolved_user_intent",
    "retrieval_hints",
    "success_condition",
    "suggested_category_focus",
    "target_source",
    "target_status",
}


def _intent_brief() -> Dict[str, Any]:
    """Return a deterministic classifier-brief fixture."""
    return {
        "resolved_user_intent": "Validate exposed services on 10.0.0.5",
        "overall_goal": "Map externally reachable services",
        "continuation_mode": "new_request",
        "resolved_step_title": "Service enumeration",
        "resolved_step_detail": "Collect open ports and banners",
        "next_operational_goal": "Run a TCP scan with service detection",
        "success_condition": "Open ports and service names captured",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "resolved_target": "10.0.0.5",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
        "explicit_constraints": ["No UDP", "Keep command runtime under 60s"],
        "suggested_category_focus": ["network_recon"],
        "retrieval_hints": ["nmap scan", "service banners"],
        "relevant_memory_fragments": ["Host already responds to ping"],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
    }


def _dr_planner_brief() -> Dict[str, Any]:
    """Return the planner surface of the brief fixture."""
    brief = dict(_intent_brief())
    brief.pop("suggested_category_focus", None)
    return brief


def _relevant_findings() -> List[Dict[str, Any]]:
    """Return deterministic relevant-findings fixture data."""
    return [
        {
            "kind": "port_open",
            "target": "10.0.0.5",
            "subject": "10.0.0.5:443/tcp",
            "details": {"service": "https"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_713_870_000,
            "ttl_seconds": 600,
            "state": "fresh",
        },
        {
            "kind": "service_banner",
            "target": "10.0.0.5",
            "subject": "nginx/1.22",
            "details": {"port": 443},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_713_870_123,
            "ttl_seconds": 600,
            "state": "fresh",
        },
    ]


def _build_ptr_interactive_state(*, with_ledger: bool) -> InteractiveState:
    """Build PTR prompt state fixture with optional phase-ledger records."""
    metadata: Dict[str, Any] = {
        "last_tool_result": {
            "parameters": {"target": "10.0.0.5", "ports": "1-1000"},
            "stdout_excerpt": "443/tcp open https\n",
            "stderr_excerpt": "",
            "was_truncated": False,
            "chars_truncated": 0,
            "suggest_file_reading": False,
        },
        "last_tool_result_compact": {
            "summary": "Nmap reported 443/tcp open on the target.",
            "key_findings": ["443/tcp open https"],
            "errors": [],
            "report_recommendations": ["Enumerate TLS configuration"],
        },
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        # Realistic agent state: the working-memory node folds the
        # classifier-derived intent brief into ``working_memory.intent_brief``.
        # The PTR builder consumes it to render ``## User Goal``.
        "working_memory": {
            "intent_brief": _intent_brief(),
        },
    }
    if with_ledger:
        _iteration_memory.append(
            metadata,
            turn_sequence=9,
            source="tool",
            payload={
                "sections": [
                    {
                        "heading": "Tool Output Summary",
                        "body": "Nmap reported 443/tcp open on the target.",
                    },
                    {
                        "heading": "Key Findings",
                        "body": "443/tcp open https",
                    },
                ]
            },
        )

    facts = FactsState(
        task_id=42,
        message="Enumerate 10.0.0.5 web exposure",
        capability="deep_reasoning",
        selected_tool="nmap.scan",
        tool_parameters={"target": "10.0.0.5"},
        plan=["Run nmap", "Inspect TLS service"],
        todo_list=["Scan ports", "Review TLS metadata"],
        current_goal="Validate externally reachable services",
        metadata=metadata,
    )
    return InteractiveState(facts=facts, trace=TraceState())


def test_intent_brief_equivalence_fixture_shape_is_stable() -> None:
    """Fixtures must preserve neutral keys and cross-surface equivalence."""
    fixture_dir = (
        Path(__file__).resolve().parent / "fixtures" / "intent_brief_equivalence"
    )
    fixture_paths = sorted(fixture_dir.glob("*.json"))
    assert len(fixture_paths) == 12
    for fixture_path in fixture_paths:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert sorted(data.keys()) == ["planner_surface", "ptr_surface", "tool_surface"]
        planner_surface = data["planner_surface"]
        ptr_surface = data["ptr_surface"]
        tool_surface = data["tool_surface"]

        for key in set(planner_surface).intersection(ptr_surface):
            assert planner_surface[key] == ptr_surface[key]
        for key in set(tool_surface).intersection(planner_surface):
            assert tool_surface[key] == planner_surface[key]
        for key in set(tool_surface).intersection(ptr_surface):
            assert tool_surface[key] == ptr_surface[key]

        union_surface = dict(ptr_surface)
        union_surface.update(planner_surface)
        union_surface.update(tool_surface)
        assert set(union_surface.keys()) == _UNIFIED_INTENT_BRIEF_KEYS


def test_memory_consolidation_snapshot_tool_planning_select_full() -> None:
    """Snapshot select-tools prompt with populated brief/findings context."""
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_select_tools_prompt(
        user_message="Find exposed services on 10.0.0.5",
        conversation_history=[{"role": "user", "content": "scan host"}],
        resolved_tools=[{"id": "nmap.scan", "reason": "port scan"}],
        catalog=[{"id": "nmap.scan", "name": "nmap", "description": "scan ports"}],
        target="10.0.0.5",
        phase="phase3",
        constraints={"max_tool_calls": 3},
        intent_brief=_intent_brief(),
        next_tool_hint="run nmap service scan",
        working_memory_summary="active target 10.0.0.5; objective service mapping",
        relevant_findings=_relevant_findings(),
    )
    assert_golden("memory_consolidation__tool_planning_select_full.txt", prompt)


def test_memory_consolidation_snapshot_tool_planning_select_sparse() -> None:
    """Snapshot select-tools prompt with empty brief/findings context."""
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_select_tools_prompt(
        user_message="Find exposed services on 10.0.0.5",
        conversation_history=[],
        resolved_tools=[],
        catalog=[],
        target="10.0.0.5",
        phase="phase3",
        constraints={},
        intent_brief={},
        next_tool_hint=None,
        working_memory_summary=None,
        relevant_findings=None,
    )
    assert_golden("memory_consolidation__tool_planning_select_sparse.txt", prompt)


def test_memory_consolidation_snapshot_tool_planning_select_rejects_ltm_kwarg() -> None:
    """Select-tools builder must reject removed long_term_memory_summary kwarg."""
    builder = ToolPlanningPromptBuilder()
    kwargs: Dict[str, Any] = {
        "user_message": "Find exposed services on 10.0.0.5",
        "conversation_history": [],
        "resolved_tools": [{"id": "nmap.scan"}],
        "catalog": [{"id": "nmap.scan", "name": "nmap", "description": "scan ports"}],
        "target": "10.0.0.5",
        "phase": "phase3",
        "constraints": {"max_tool_calls": 2},
        "intent_brief": _intent_brief(),
        "working_memory_summary": "summary",
        "relevant_findings": _relevant_findings(),
    }
    with pytest.raises(TypeError):
        builder.build_select_tools_prompt(  # type: ignore[call-arg]
            **kwargs,
            long_term_memory_summary="removed kwarg",
        )


def test_memory_consolidation_snapshot_tool_planning_params_full() -> None:
    """Snapshot tool-parameters prompt with populated memory context."""
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        user_message="Find exposed services on 10.0.0.5",
        conversation_history=[],
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="phase3",
        constraints={"max_tool_calls": 3},
        intent_brief=_intent_brief(),
        plan_text=["Run nmap -sV -p- 10.0.0.5", "Enumerate TLS settings"],
        current_goal="Service enumeration",
        todo_list=[{"text": "Run nmap", "status": "in_progress"}],
        next_tool_hint="run nmap -sV -p- 10.0.0.5",
        previous_tool="nmap.scan",
        previous_tool_output_summary="443/tcp open https",
        working_memory_summary="active target 10.0.0.5",
        relevant_findings=_relevant_findings(),
    )
    assert_golden("memory_consolidation__tool_planning_parameters_full.txt", prompt)


def test_memory_consolidation_snapshot_tool_planning_params_sparse() -> None:
    """Snapshot tool-parameters prompt with minimal memory context."""
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        user_message="Find exposed services on 10.0.0.5",
        conversation_history=[],
        selected_tools=[],
        target="10.0.0.5",
        phase="phase3",
        constraints={},
        intent_brief={},
        plan_text=None,
        current_goal=None,
        todo_list=None,
        next_tool_hint=None,
        previous_tool=None,
        previous_tool_output_summary=None,
        working_memory_summary=None,
        relevant_findings=None,
    )
    assert_golden("memory_consolidation__tool_planning_parameters_sparse.txt", prompt)


def test_memory_consolidation_snapshot_tool_planning_params_rejects_ltm_kwarg() -> None:
    """Tool-parameters builder must reject removed long_term_memory_summary kwarg."""
    builder = ToolPlanningPromptBuilder()
    kwargs: Dict[str, Any] = {
        "user_message": "Find exposed services on 10.0.0.5",
        "conversation_history": [],
        "selected_tools": ["nmap.scan"],
        "target": "10.0.0.5",
        "phase": "phase3",
        "constraints": {"max_tool_calls": 2},
        "intent_brief": _intent_brief(),
        "plan_text": ["Run nmap -sV -p- 10.0.0.5"],
        "current_goal": "Service enumeration",
        "working_memory_summary": "summary",
        "relevant_findings": _relevant_findings(),
    }
    with pytest.raises(TypeError):
        builder.build_tool_parameters_prompt(  # type: ignore[call-arg]
            **kwargs,
            long_term_memory_summary="removed kwarg",
        )


def test_memory_consolidation_snapshot_tool_category_prompt_full() -> None:
    """Snapshot category-selection prompt with populated brief."""
    prompt = build_tool_category_selection_prompt(
        categories_text="- network_recon: recon tools\n- web_assessment: web tools",
        intent_brief=_intent_brief(),
        next_tool_hint="scan tcp ports on 10.0.0.5",
    )
    assert_golden("memory_consolidation__category_selection_full.txt", prompt)


def test_memory_consolidation_snapshot_tool_category_prompt_sparse() -> None:
    """Snapshot category-selection prompt with missing brief and hint."""
    prompt = build_tool_category_selection_prompt(
        categories_text="- network_recon: recon tools\n- web_assessment: web tools",
        intent_brief={},
        next_tool_hint=None,
    )
    assert_golden("memory_consolidation__category_selection_sparse.txt", prompt)


def test_memory_consolidation_snapshot_tool_articulation_prompt_full() -> None:
    """Snapshot articulation prompt with populated brief."""
    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params='{"target":"10.0.0.5","ports":"1-1000"}',
        intent_brief=_intent_brief(),
    )
    assert_golden("memory_consolidation__tool_articulation_full.txt", prompt)


def test_memory_consolidation_snapshot_tool_articulation_prompt_sparse() -> None:
    """Snapshot articulation prompt with missing brief."""
    prompt = build_tool_articulation_prompt(
        selected_tool="nmap.scan",
        tool_params='{"target":"10.0.0.5"}',
        intent_brief={},
    )
    assert_golden("memory_consolidation__tool_articulation_sparse.txt", prompt)


def test_memory_consolidation_snapshot_dr_planner_prompt_full() -> None:
    """Snapshot DR planner user prompt with populated brief."""
    prompt = build_planning_prompt(
        targets_str="10.0.0.5",
        network_discovery_section="",
        tools_constraint="- nmap\n- sslscan",
        scope_constraints="- no exploitation",
        intent_brief=_dr_planner_brief(),
        clarified_inputs_section="",
        planner_environment_section="",
    )
    assert_golden("memory_consolidation__dr_planner_full.txt", prompt)


def test_memory_consolidation_snapshot_dr_planner_prompt_sparse() -> None:
    """Snapshot DR planner user prompt with missing brief fields."""
    prompt = build_planning_prompt(
        targets_str="(none)",
        network_discovery_section="",
        tools_constraint="- nmap",
        scope_constraints="- no exploitation",
        intent_brief={},
        clarified_inputs_section="",
        planner_environment_section="",
    )
    assert_golden("memory_consolidation__dr_planner_sparse.txt", prompt)


def test_memory_consolidation_snapshot_ptr_prompt_with_ledger() -> None:
    """Snapshot PTR prompt when phase ledger has current-turn records."""
    builder = PostToolReasoningPromptBuilder()
    interactive = _build_ptr_interactive_state(with_ledger=True)
    prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={
            "tool": "nmap.scan",
            "summary": "443/tcp open https",
            "key_findings": ["443/tcp open https"],
            "vulnerabilities": [],
            "next_actions": ["Inspect TLS config"],
        },
        relevant_findings=_relevant_findings(),
        failure_context={
            "failure_detected": False,
            "failure_category": None,
            "retry_count": 0,
            "can_retry": False,
            "max_retries": 2,
        },
        environment_context="",
        turn_sequence=9,
        current_ptr_phase_sequence=1,
        latest_recorded_phase_sequence=0,
    )
    assert_golden("memory_consolidation__ptr_prompt_with_ledger.txt", prompt)


def test_memory_consolidation_snapshot_ptr_prompt_without_ledger() -> None:
    """Snapshot PTR prompt when phase ledger is absent."""
    builder = PostToolReasoningPromptBuilder()
    interactive = _build_ptr_interactive_state(with_ledger=False)
    prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={
            "tool": "nmap.scan",
            "summary": "443/tcp open https",
            "key_findings": ["443/tcp open https"],
            "vulnerabilities": [],
            "next_actions": ["Inspect TLS config"],
        },
        relevant_findings=_relevant_findings(),
        failure_context={
            "failure_detected": False,
            "failure_category": None,
            "retry_count": 0,
            "can_retry": False,
            "max_retries": 2,
        },
        environment_context="",
        turn_sequence=9,
        current_ptr_phase_sequence=0,
        latest_recorded_phase_sequence=None,
    )
    assert_golden("memory_consolidation__ptr_prompt_without_ledger.txt", prompt)
