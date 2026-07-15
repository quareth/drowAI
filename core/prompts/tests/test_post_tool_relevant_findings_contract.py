"""Regression tests for relevant-findings injection on the live PTR builder."""

from __future__ import annotations

from agent.graph.state import FactsState, InteractiveState, TraceState
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder


def _interactive_state() -> InteractiveState:
    facts = FactsState(
        task_id=41,
        message="Enumerate the web service on 10.0.0.1",
        capability="deep_reasoning",
        selected_tool="nmap.scan",
        tool_parameters={"target": "10.0.0.1"},
        metadata={
            "last_tool_result": {
                "parameters": {"target": "10.0.0.1"},
                "was_truncated": False,
                "chars_truncated": 0,
                "suggest_file_reading": False,
            },
            "last_tool_result_compact": {
                "summary": "Port 80 is open and speaks HTTP.",
                "key_findings": ["80/tcp open http"],
                "errors": [],
            },
        },
    )
    return InteractiveState(facts=facts, trace=TraceState())


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


def test_build_user_prompt_accepts_and_renders_relevant_findings() -> None:
    builder = PostToolReasoningPromptBuilder()

    prompt = builder.build_user_prompt(
        interactive=_interactive_state(),
        synthesized={
            "tool": "nmap.scan",
            "summary": "Port 80 is open and speaks HTTP.",
            "key_findings": ["80/tcp open http"],
        },
        relevant_findings=_relevant_findings(),
        failure_context={},
    )

    assert "## Relevant Prior Findings" in prompt
    assert "[fresh] port_open 10.0.0.1:80/tcp" in prompt


def test_build_articulation_user_prompt_accepts_and_renders_relevant_findings() -> None:
    builder = PostToolReasoningPromptBuilder()

    prompt = builder.build_articulation_user_prompt(
        interactive=_interactive_state(),
        synthesized={
            "tool": "nmap.scan",
            "summary": "Port 80 is open and speaks HTTP.",
            "key_findings": ["80/tcp open http"],
        },
        decision_output={
            "next_action": "call_tool",
            "action_reasoning": "Need HTTP-focused follow-up.",
            "effective_next_goal": "Inspect the HTTP service.",
            "user_goal_achieved": False,
            "failure_detected": False,
            "failure_category": None,
            "retry_suggested": False,
        },
        relevant_findings=_relevant_findings(),
    )

    assert "## Relevant Prior Findings" in prompt
    assert "[fresh] port_open 10.0.0.1:80/tcp" in prompt
