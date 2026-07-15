"""Characterization tests for existing prompt builders.

These snapshots lock in current prompt outputs before migrating prompt
infrastructure. Tests compare against golden files under `golden/`.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.prompts.tests._golden import assert_golden


def test_simple_tool_prompt_builder_outputs() -> None:
    from core.prompts.builders.simple_tool import SimpleToolPromptBuilder

    state: Dict[str, object] = {
        "facts": {
            "message": "Scan example.com for open ports.",
            "capability": "simple_tool_execution",
            "intent_hints": {"tool_hints": ["nmap"], "targets": ["example.com"]},
            "eligible_routes": ["simple_tool_execution"],
            "metadata": {
                "tool_catalog": {
                    "entries": [
                        {
                            "tool_id": "nmap.scan",
                            "name": "nmap",
                            "description": "Network mapper.",
                        }
                    ]
                }
            },
            "tool_candidates": ["nmap.scan"],
            "selected_tool": "nmap.scan",
        }
    }

    tool_result = {
        "summary": "Found 2 open ports.",
        "key_findings": ["Found 2 open ports."],
        "errors": [],
        "stderr_truncated": False,
        "status": "success",
    }

    builder = SimpleToolPromptBuilder()
    assert_golden("simple_tool__system.txt", builder.build_system_prompt(state))
    assert_golden("simple_tool__decision.txt", builder.build_decision_prompt(state))
    assert_golden("simple_tool__summary.txt", builder.build_tool_summary_prompt(tool_result))


def test_deep_reasoning_prompt_builder_outputs() -> None:
    from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder

    state: Dict[str, object] = {
        "facts": {
            "current_goal": "Enumerate services on 10.0.0.1",
            "iterations": 2,
            "plan": ["Run nmap scan", "Enumerate SMB if open"],
            "runtime_budgets": {
                "remaining_iterations": 3,
                "remaining_tool_calls": 5,
            },
            "tool_ids": ["shell.exec", "nmap.scan"],
            "todo_list": [{"text": "Identify open ports"}, {"text": "Check SMB"}],
        },
        "trace": {
            "scratchpad": "Need to confirm which ports are open before choosing next tool.",
            "observations": ["No prior tools run yet."],
            "executed_tools": [{"tool_id": "nmap.scan", "observation": "No response"}],
        },
    }

    tool_result = {
        "tool": "nmap.scan",
        "summary": "Port 80 is open.",
        "key_findings": ["80/tcp open http"],
        "errors": [],
        "observation": "Port 80 is open.",
    }

    builder = DeepReasoningPromptBuilder()
    assert_golden("deep_reasoning__system.txt", builder.build_system_prompt(state))
    assert_golden("deep_reasoning__decision.txt", builder.build_decision_prompt(state))
    assert_golden("deep_reasoning__summary.txt", builder.build_tool_summary_prompt(tool_result))

    think_more = builder.build_think_more_prompt(state)
    assert_golden("deep_reasoning__think_more.txt", think_more)


def test_tool_planning_prompt_builder_outputs() -> None:
    from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

    builder = ToolPlanningPromptBuilder()
    user_message = "Find the open ports on 10.0.0.1 and identify services."
    history: List[Dict[str, Any]] = [
        {"role": "user", "content": "scan 10.0.0.1"},
        {"role": "assistant", "content": "I will run a port scan."},
    ]
    relevant_findings = [
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

    assert_golden(
        "tool_planning__system.txt",
        builder.build_system_prompt(user_message=user_message, conversation_history=history),
    )

    resolve = builder.build_resolve_tools_prompt(
        user_message=user_message,
        conversation_history=history,
        target="10.0.0.1",
        phase="phase3",
        constraints={"max_tool_calls": 3},
        relevant_findings=relevant_findings,
    )
    assert_golden("tool_planning__resolve_tools.txt", resolve)

    select = builder.build_select_tools_prompt(
        user_message=user_message,
        conversation_history=history,
        resolved_tools=[{"id": "nmap.scan", "reason": "port scan"}],
        catalog=[{"id": "nmap.scan", "name": "nmap", "description": "scan ports"}],
        target="10.0.0.1",
        phase="phase3",
        constraints={"max_tool_calls": 3},
        next_tool_hint="run ip addr",
        relevant_findings=relevant_findings,
    )
    assert_golden("tool_planning__select_tools.txt", select)

    params = builder.build_tool_parameters_prompt(
        user_message=user_message,
        conversation_history=history,
        selected_tools=["nmap.scan"],
        target="10.0.0.1",
        phase="phase3",
        constraints={"max_tool_calls": 3},
        plan_text=["Run nmap -sV", "Check web server"],
        current_goal="Enumerate services",
        todo_list=[
            {"text": "Run nmap -sV", "status": "in_progress"},
            {"text": "Check web server", "status": "pending"},
        ],
        next_tool_hint="run nmap -sV -p- 10.0.0.1",
        previous_tool="nmap.scan",
        previous_tool_output_summary="Ports 22 and 80 open.",
        relevant_findings=relevant_findings,
    )
    assert_golden("tool_planning__tool_parameters.txt", params)


def test_post_tool_reasoning_prompt_builder_outputs() -> None:
    from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
    from agent.graph.state import FactsState, InteractiveState, TraceState

    facts = FactsState(
        task_id=123,
        message="Enumerate web server on 10.0.0.1",
        capability="deep_reasoning",
        selected_tool="nmap.scan",
        tool_parameters={"target": "10.0.0.1"},
        plan=["Run nmap", "Inspect HTTP service"],
        todo_list=["Scan ports", "Fingerprint web server"],
        metadata={
            "last_tool_result": {
                "parameters": {"target": "10.0.0.1"},
                "stdout_excerpt": "80/tcp open http\n",
                "stderr_excerpt": "",
                "was_truncated": False,
                "chars_truncated": 0,
                "suggest_file_reading": False,
            },
            "last_tool_result_compact": {
                "summary": "Nmap found HTTP on port 80.",
                "key_findings": ["80/tcp open http"],
                "errors": [],
                "report_recommendations": ["Try HTTP enumeration"],
            },
        },
    )
    interactive = InteractiveState(facts=facts, trace=TraceState())

    synthesized = {
        "tool": "nmap.scan",
        "summary": "Nmap found HTTP on port 80.",
        "key_findings": ["80/tcp open http"],
        "vulnerabilities": [],
        "next_actions": ["Try HTTP enumeration"],
    }
    relevant_findings = [
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

    builder = PostToolReasoningPromptBuilder()
    # ``post_tool_reasoning__system.txt`` is shared with
    # ``test_builders.py::test_post_tool_builder_renders_prompts`` because
    # ``build_system_prompt`` is input-independent. The user-prompt golden
    # is fixture-specific and must NOT be shared (this characterization
    # test omits ``metadata.working_memory.intent_brief``, which the other
    # test includes — both can't satisfy a single golden).
    assert_golden("post_tool_reasoning__system.txt", builder.build_system_prompt())

    user_prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized=synthesized,
        relevant_findings=relevant_findings,
        failure_context={
            "failure_detected": True,
            "failure_category": "empty_output",
            "retry_count": 0,
            "can_retry": True,
            "max_retries": 2,
        },
        environment_context="",
    )
    assert_golden("post_tool_reasoning__user_characterization.txt", user_prompt)
