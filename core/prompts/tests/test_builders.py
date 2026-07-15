"""Tests for core prompt builders, strict parity, and registry wiring."""

from __future__ import annotations

from typing import Any, Dict, List

from core.prompts.tests._golden import assert_golden


def test_derive_user_input_and_goal_prefers_original_goal() -> None:
    from core.prompts.builders._text import derive_user_input_and_goal

    facts = {
        "message": "ok, continue",
        "metadata": {
            "working_memory": {
                "intent_brief": {
                    "original_goal": "Map the lab subnet, then inspect PostgreSQL exposure",
                    "resolved_user_intent": "Inspect PostgreSQL exposure",
                    "overall_goal": "Build an attack surface inventory",
                }
            }
        },
    }

    user_input, user_goal = derive_user_input_and_goal(facts)

    assert user_input == "ok, continue"
    assert user_goal == "Map the lab subnet, then inspect PostgreSQL exposure"


def test_simple_tool_builder_renders_prompts() -> None:
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

    builder = SimpleToolPromptBuilder()
    system_prompt = builder.build_system_prompt(state)
    decision_prompt = builder.build_decision_prompt(state)
    summary_prompt = builder.build_tool_summary_prompt(
        {
            "summary": "Found 2 open ports.",
            "key_findings": ["Found 2 open ports."],
            "errors": [],
            "stderr_truncated": False,
            "status": "success",
        }
    )

    assert_golden("simple_tool__system.txt", system_prompt)
    assert_golden("simple_tool__decision.txt", decision_prompt)
    assert_golden("simple_tool__summary.txt", summary_prompt)



def test_tool_planning_builder_renders_prompts() -> None:
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

    system_prompt = builder.build_system_prompt(user_message=user_message, conversation_history=history)
    resolve_prompt = builder.build_resolve_tools_prompt(
        user_message=user_message,
        conversation_history=history,
        target="10.0.0.1",
        phase="phase3",
        constraints={"max_tool_calls": 3},
        relevant_findings=relevant_findings,
    )
    select_prompt = builder.build_select_tools_prompt(
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
    params_prompt = builder.build_tool_parameters_prompt(
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

    assert_golden("tool_planning__system.txt", system_prompt)
    assert_golden("tool_planning__resolve_tools.txt", resolve_prompt)
    assert_golden("tool_planning__select_tools.txt", select_prompt)
    assert_golden("tool_planning__tool_parameters.txt", params_prompt)



def test_tool_planning_builder_includes_working_memory_snapshot() -> None:
    from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder

    builder = ToolPlanningPromptBuilder()
    history = [{"role": "user", "content": "scan host"}]
    wm_summary = "stage: tool_selection\nopen_questions: specify target"

    select_prompt = builder.build_select_tools_prompt(
        user_message="scan host",
        conversation_history=history,
        resolved_tools=["shell.exec"],
        catalog=[{"id": "shell.exec", "name": "shell.exec", "description": "run shell"}],
        target="10.0.0.1",
        phase="enumeration",
        constraints={},
        working_memory_summary=wm_summary,
    )
    params_prompt = builder.build_tool_parameters_prompt(
        user_message="scan host",
        conversation_history=history,
        selected_tools=["shell.exec"],
        target="10.0.0.1",
        phase="enumeration",
        constraints={},
        working_memory_summary=wm_summary,
    )

    assert "Working Memory Snapshot:" in select_prompt
    assert wm_summary in select_prompt
    assert "Working Memory Snapshot:" in params_prompt
    assert wm_summary in params_prompt


def test_deep_reasoning_builder_renders_prompts() -> None:
    from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder

    builder = DeepReasoningPromptBuilder()
    state: Dict[str, object] = {
        "facts": {
            "current_goal": "Enumerate services on 10.0.0.1",
            "iterations": 2,
            "plan": ["Run nmap scan", "Enumerate SMB if open"],
            "runtime_budgets": {"remaining_iterations": 3, "remaining_tool_calls": 5},
            "tool_ids": ["shell.exec", "nmap.scan"],
            "todo_list": [{"text": "Identify open ports"}, {"text": "Check SMB"}],
        },
        "trace": {
            "scratchpad": "Need to confirm which ports are open before choosing next tool.",
            "observations": ["No prior tools run yet."],
            "executed_tools": [{"tool_id": "nmap.scan", "observation": "No response"}],
        },
    }

    system_prompt = builder.build_system_prompt(state)
    decision_prompt = builder.build_decision_prompt(state)
    summary_prompt = builder.build_tool_summary_prompt(
        {
            "tool": "nmap.scan",
            "summary": "Port 80 is open.",
            "key_findings": ["80/tcp open http"],
            "errors": [],
            "observation": "Port 80 is open.",
        }
    )

    think_more_prompt = builder.build_think_more_prompt(state)

    assert_golden("deep_reasoning__system.txt", system_prompt)
    assert_golden("deep_reasoning__decision.txt", decision_prompt)
    assert_golden("deep_reasoning__summary.txt", summary_prompt)
    assert_golden("deep_reasoning__think_more.txt", think_more_prompt)



def test_post_tool_builder_renders_prompts() -> None:
    from agent.graph.state import FactsState, InteractiveState, TraceState
    from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

    builder = PostToolReasoningPromptBuilder()

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
            "working_memory": {
                "intent_brief": {
                    "resolved_user_intent": "Enumerate the web server on 10.0.0.1",
                    "overall_goal": "Map externally reachable services on 10.0.0.1",
                },
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

    system_prompt = builder.build_system_prompt()
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

    assert_golden("post_tool_reasoning__system.txt", system_prompt)
    assert_golden("post_tool_reasoning__user.txt", user_prompt)


def test_post_tool_builder_renders_shared_last_tool_special_sections() -> None:
    from agent.graph.state import FactsState, InteractiveState, TraceState
    from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

    facts = FactsState(
        task_id=123,
        message="Scan 10.0.0.1",
        capability="deep_reasoning",
        selected_tool="nmap.scan",
        tool_parameters={"target": "10.0.0.1"},
        metadata={
            "last_artifact_path": "/workspace/.artifacts/nmap.txt",
            "last_tool_result": {
                "parameters": {"target": "10.0.0.1"},
                "was_truncated": True,
                "chars_truncated": 12345,
                "suggest_file_reading": True,
            },
            "last_tool_result_compact_batch": {
                "status": "completed_with_errors",
                "success": False,
                "results": [
                    {
                        "tool_id": "nmap.scan",
                        "intent": "discover exposed services",
                        "status": "success",
                        "compact_tool_result": {
                            "summary": "80/tcp open http",
                            "key_findings": ["80/tcp open http"],
                            "lossiness_risk": "low",
                        },
                    },
                    {
                        "tool_id": "http.probe",
                        "intent": "fingerprint web service",
                        "status": "failed",
                        "failure_category": "timeout",
                        "compact_tool_result": {"summary": "HTTP probe timed out"},
                    },
                ],
            },
        },
    )
    prompt = PostToolReasoningPromptBuilder().build_user_prompt(
        interactive=InteractiveState(facts=facts, trace=TraceState()),
        synthesized={"tool": "nmap.scan"},
    )

    batch_heading = "## Batch Tool Results"
    lossiness_heading = "## Compression Lossiness"
    output_heading = "## Output Info"

    assert batch_heading in prompt
    assert "batch_status: completed_with_errors" in prompt
    assert "- http.probe: failed; failure=timeout; intent=fingerprint web service" in prompt
    assert lossiness_heading in prompt
    assert "lossiness_risk: low" in prompt
    assert output_heading in prompt
    assert "Output condensed (12,345 chars omitted)." in prompt
    assert prompt.index(batch_heading) < prompt.index(lossiness_heading)
    assert prompt.index(lossiness_heading) < prompt.index(output_heading)


def test_post_tool_builder_includes_active_decision_advisory_context() -> None:
    from agent.graph.state import FactsState, InteractiveState, TraceState
    from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

    facts = FactsState(
        task_id=123,
        message="scan network and check postgres",
        capability="deep_reasoning",
        selected_tool="nmap.scan",
        tool_parameters={"target": "172.17.0.1", "ports": "5432"},
        plan=["Discover hosts", "Scan 5432 on one host"],
        todo_list=["Discover hosts", "Scan 5432"],
        metadata={
            "last_tool_result": {
                "parameters": {"target": "172.17.0.1", "ports": "5432"},
                "was_truncated": False,
                "chars_truncated": 0,
                "suggest_file_reading": False,
            },
            "last_tool_result_compact": {
                "summary": "Port 5432 is closed on 172.17.0.1.",
                "key_findings": ["172.17.0.1 up", "5432/tcp closed"],
                "errors": [],
            },
            "working_memory": {
                "active_decision": {
                    "source": "post_tool_reasoning",
                    "authority": "llm_proposal",
                    "status": "active",
                    "next_action": "call_tool",
                    "tool_intent": {
                        "description": "Scan PostgreSQL port 5432 on one discovered online host",
                        "target": "172.17.0.1",
                        "focus": "tcp/5432",
                    },
                    "effective_next_goal": "Determine whether TCP/5432 is open on an online host.",
                    "action_reasoning": "Only feasible host after exclusions was 172.17.0.1.",
                    "todo_delta": [{"index": 0, "status": "completed"}],
                }
            },
        },
    )
    interactive = InteractiveState(facts=facts, trace=TraceState())
    builder = PostToolReasoningPromptBuilder()
    prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={"tool": "nmap.scan"},
    )

    assert "## Prior Active Decision (Advisory)" in prompt
    assert "tool_intent.target: 172.17.0.1" in prompt
    assert "current todo/goal state are authoritative" in prompt


def test_post_tool_articulation_prompt_includes_structured_tool_intent() -> None:
    from agent.graph.state import FactsState, InteractiveState, TraceState
    from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder

    facts = FactsState(
        task_id=123,
        message="Enumerate web service on 10.129.45.6",
        capability="simple_tool_execution",
        selected_tool="information_gathering.web_enumeration.http_request",
        tool_parameters={"target": "http://10.129.45.6:80"},
        metadata={
            "last_tool_result_compact": {
                "summary": "Root page exposes navigation links to /capture and /ip.",
                "key_findings": ["Gunicorn server", "Link: /capture"],
                "errors": [],
            },
        },
    )
    interactive = InteractiveState(facts=facts, trace=TraceState())
    builder = PostToolReasoningPromptBuilder()

    prompt = builder.build_articulation_user_prompt(
        interactive=interactive,
        synthesized={
            "tool": "information_gathering.web_enumeration.http_request",
            "summary": "Root page exposes navigation links to /capture and /ip.",
            "key_findings": ["Gunicorn server", "Link: /capture"],
        },
        decision_output={
            "next_action": "call_tool",
            "action_reasoning": "The root page was inspected; fetch the linked route next.",
            "tool_intent": {
                "description": "Fetch the /capture endpoint",
                "target": "http://10.129.45.6:80/capture",
                "focus": "HTTP endpoint enumeration and stack fingerprinting",
            },
            "user_goal_achieved": False,
            "effective_next_goal": "Enumerate linked routes starting with /capture.",
            "failure_detected": False,
            "failure_category": None,
            "retry_suggested": False,
        },
    )

    assert "## Decision Context" in prompt
    assert "tool_intent.description: Fetch the /capture endpoint" in prompt
    assert "tool_intent.target: http://10.129.45.6:80/capture" in prompt
    assert (
        "tool_intent.focus: HTTP endpoint enumeration and stack fingerprinting"
        in prompt
    )



def test_prompt_registry_template_and_builder_access() -> None:
    from core.prompts.constants import CLASSIFIER_SYSTEM_PROMPT, SIMPLE_CHAT_DEFAULT_SYSTEM_PROMPT
    from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder
    from core.prompts.registry import PromptRegistry

    registry = PromptRegistry()

    # The "intent" prompt family evolves frequently; assert that the registry
    # reports a real version on the canonical ``vN`` form rather than pinning a
    # specific number, so prompt-text iteration doesn't churn this guardrail.
    intent_version = registry.get_latest_version("intent")
    assert isinstance(intent_version, str) and intent_version.startswith("v") and intent_version[1:].isdigit()
    assert registry.get_template("intent_classifier") == CLASSIFIER_SYSTEM_PROMPT
    assert registry.get_template("simple_chat_system") == SIMPLE_CHAT_DEFAULT_SYSTEM_PROMPT
    assert "tool-output compression and analysis assistant" in registry.get_template(
        "tool_output_processing_success"
    )
    assert "contains errors" in registry.get_template("tool_output_processing_failure")
    assert "low-authority pentest knowledge candidate extractor" in registry.get_template(
        "knowledge_candidate_extraction_system"
    )
    candidate_system_template = registry.get_template("knowledge_candidate_extraction_system")
    assert "Never invent vulnerability IDs, titles, severities, or claims" in candidate_system_template
    assert "If evidence does not support a vulnerability claim, set no_signal=true." in (
        candidate_system_template
    )
    assert "vulnerability_confidence is optional and valid only for vulnerability observations." in (
        candidate_system_template
    )
    candidate_user_template = registry.get_template("knowledge_candidate_extraction_user")
    assert "<EVIDENCE_DATA_START>" in candidate_user_template
    assert "<EVIDENCE_DATA_END>" in candidate_user_template
    assert "Never invent vulnerabilities to populate optional schema fields." in candidate_user_template
    assert (
        "If evidence does not support a vulnerability claim, set no_signal=true and return no candidate observations."
        in candidate_user_template
    )
    assert (
        "vulnerability_confidence is optional and only valid for vulnerability observations."
        in candidate_user_template
    )
    assert "include evidence excerpts that directly support the vulnerability claim." in (
        candidate_user_template
    )

    assert isinstance(registry.get_chat_builder("deep_reasoning"), DeepReasoningPromptBuilder)
    assert registry.get_tool_planning_builder("tool_planning") is not None
    assert registry.get_post_tool_builder("post_tool_reasoning") is not None
