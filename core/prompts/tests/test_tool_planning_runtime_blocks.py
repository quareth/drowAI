"""Focused regression tests for planner runtime-context prompt blocks.

These tests lock the live tool-planning builder seam so the planner keeps
rendering the brief-driven prompt body while still surfacing bounded runtime
context blocks such as relevant findings and working-memory snapshots.
"""

from __future__ import annotations

from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder


def _brief() -> dict[str, object]:
    return {
        "resolved_user_intent": "Enumerate services on 10.0.0.1",
        "overall_goal": "Map exposed services on 10.0.0.1",
        "continuation_mode": "new_request",
        "resolved_step_title": "Service Enumeration",
        "resolved_step_detail": "Confirm open services before deeper follow-up.",
        "next_operational_goal": "Run version-aware enumeration on 10.0.0.1",
        "success_condition": "Return open ports with service banners",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "explicit_constraints": ["Avoid destructive actions"],
        "relevant_memory_fragments": ["prior scan saw HTTP on 80/tcp"],
        "suggested_category_focus": ["information_gathering"],
        "retrieval_hints": ["service detection"],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        "resolved_target": "10.0.0.1",
        "target_status": "resolved",
        "target_source": "explicit_current_message",
    }


def _relevant_findings() -> list[dict[str, object]]:
    return [
        {
            "kind": "port_open",
            "target": "10.0.0.1",
            "subject": "10.0.0.1:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": 1_713_870_000,
            "ttl_seconds": 600,
            "state": "fresh",
        }
    ]


def test_select_tools_prompt_renders_brief_findings_and_working_memory_snapshot() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_select_tools_prompt(
        resolved_tools=["nmap.scan"],
        catalog=[{"id": "nmap.scan", "name": "nmap.scan", "description": "scan ports"}],
        target="10.0.0.1",
        phase="enumeration",
        constraints={"max_tool_calls": 2},
        intent_brief=_brief(),
        relevant_findings=_relevant_findings(),
        working_memory_summary="stage: tool_selection\nopen_questions: specify target",
    )

    assert "Turn Execution Brief" in prompt
    assert "Relevant Prior Findings:" in prompt
    assert "[fresh] port_open 10.0.0.1:80/tcp" in prompt
    assert "Working Memory Snapshot:" in prompt
    assert "stage: tool_selection" in prompt


def test_select_tools_prompt_renders_descriptions_from_resolved_tool_entries() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_select_tools_prompt(
        resolved_tools=[
            {
                "id": "information_gathering.web_enumeration.http_request",
                "description": "Perform one HTTP request against a known URL.",
            },
            {
                "id": "web_applications.web_crawlers.ffuf",
                "description": "Enumerate paths with a /FUZZ URL template.",
            },
        ],
        target="http://10.129.45.6:80",
        phase="enumeration",
        constraints={},
    )

    assert (
        "- information_gathering.web_enumeration.http_request: "
        "Perform one HTTP request against a known URL."
    ) in prompt
    assert (
        "- web_applications.web_crawlers.ffuf: "
        "Enumerate paths with a /FUZZ URL template."
    ) in prompt


def test_select_tools_prompt_does_not_apply_second_description_cap() -> None:
    builder = ToolPlanningPromptBuilder()
    description = "x" * 180

    prompt = builder.build_select_tools_prompt(
        resolved_tools=["example.tool"],
        catalog=[
            {
                "id": "example.tool",
                "description": description,
            }
        ],
        target="example-target",
        phase="enumeration",
        constraints={},
    )

    assert f"- example.tool: {description}" in prompt


def test_tool_parameters_prompt_renders_brief_findings_and_working_memory_snapshot() -> None:
    builder = ToolPlanningPromptBuilder()

    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.1",
        phase="enumeration",
        constraints={"max_tool_calls": 2},
        intent_brief=_brief(),
        plan_text=["Run version detection", "Inspect HTTP service"],
        current_goal="Enumerate open services",
        relevant_findings=_relevant_findings(),
        working_memory_summary="stage: tool_parameterization\nlast_tool_run: nmap.scan: 80/tcp open",
    )

    assert "Turn Execution Brief" in prompt
    assert "Relevant Prior Findings:" in prompt
    assert "[fresh] port_open 10.0.0.1:80/tcp" in prompt
    assert "Working Memory Snapshot:" in prompt
    assert "stage: tool_parameterization" in prompt


def test_tool_planning_prompts_render_referenced_prior_turns_once() -> None:
    builder = ToolPlanningPromptBuilder()

    select_prompt = builder.build_select_tools_prompt(
        resolved_tools=["nmap.scan"],
        catalog=[{"id": "nmap.scan", "name": "nmap.scan", "description": "scan ports"}],
        target="",
        phase="enumeration",
        constraints={},
        referenced_prior_turns=(
            "Referenced Prior Turns:\n"
            "- Turn 2 (user): Run the service enumeration step."
        ),
    )
    params_prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="",
        phase="enumeration",
        constraints={},
        referenced_prior_turns=(
            "Referenced Prior Turns:\n"
            "- Turn 2 (user): Run the service enumeration step."
        ),
    )

    assert select_prompt.count("Referenced Prior Turns:") == 1
    assert params_prompt.count("Referenced Prior Turns:") == 1
    assert "Run the service enumeration step." in select_prompt
    assert "Run the service enumeration step." in params_prompt
