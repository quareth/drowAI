"""Focused tests for planner-context working-memory payload in tool execution."""

from __future__ import annotations

import agent.graph.nodes  # noqa: F401  # Prime node package to avoid import-cycle test collection.

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
    update_prior_turn_references,
)
from agent.graph.context.contracts import RuntimeStateSnapshot
from agent.graph.memory.findings import select_relevant_findings_for_prompt
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution import (
    _build_action_for_planner,
    _build_planner_context,
)
from agent.graph.subgraphs.tool_execution_runtime.planner_service import (
    apply_unavailable_capability_to_state,
)
from agent.graph.utils.iteration_memory import get_ledger, render_phase_memory_section
from agent.models import ActionPlan, ActionType, ExecutionStrategy
from agent.reasoning.tool_selection_sentinel import (
    UNAVAILABLE_CAPABILITY_METADATA_KEY,
    UNAVAILABLE_CAPABILITY_TOOL,
)
from agent.tool_runtime.coordinator import ToolExecutionRequest


def test_planner_context_includes_bounded_working_memory_payload() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="Scan target",
            capability="simple_tool_execution",
            metadata={
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id="conv-planner-wm",
                    turn_id="turn-planner-wm",
                    turn_sequence=0,
                    messages=[{"role": "user", "content": "Scan target"}],
                    runtime_state=RuntimeStateSnapshot(
                        active_target={"value": "10.0.0.1", "kind": "ip"},
                        current_goal={"text": "Investigate exposed services"},
                        current_decision=None,
                        in_flight_tool=None,
                        handles={"target_id": "entity:host:10.0.0.1"},
                        active_todo=None,
                    ),
                ),
                "working_memory": {
                    "stage": "tool_parameterization",
                    "objective": {"text": "Investigate exposed services with api_key=secret-value"},
                    "active": {"target_id": "entity:host:10.0.0.1"},
                    "constraints": {"preferences": ["fast", "non-destructive"]},
                    "required_inputs": [{"code": "need_target"}],
                    "validation": {"is_ready": True, "missing": [], "errors": []},
                    "open_questions": [
                        {"code": "q1", "message": "Question 1"},
                        {"code": "q2", "message": "Question 2"},
                        {"code": "q3", "message": "Question 3"},
                        {"code": "q4", "message": "Question 4"},
                    ],
                    "recent_turns": [
                        {"role": "user", "content": "turn 1"},
                        {"role": "assistant", "content": "turn 2"},
                        {"role": "user", "content": "turn 3"},
                    ],
                    "available_findings": [
                        {
                            "kind": "port_open",
                            "target": "10.0.0.1",
                            "subject": "10.0.0.1:80/tcp",
                            "details": {"service": "http"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                        {
                            "kind": "port_open",
                            "target": "10.0.0.9",
                            "subject": "10.0.0.9:22/tcp",
                            "details": {},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_869_000,
                            "ttl_seconds": 600,
                        },
                    ],
                }
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=["10.0.0.1"],
        message="scan target",
        task_id=1,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert "working_memory" in planner_context
    assert "working_memory_summary" in planner_context
    assert planner_context["working_memory"]["active_target"] == {
        "value": "10.0.0.1",
        "kind": "ip",
    }
    assert planner_context["working_memory"]["current_goal"] == {
        "text": "Investigate exposed services",
    }
    assert planner_context["working_memory"]["handles"] == {
        "target_id": "entity:host:10.0.0.1",
    }
    assert planner_context["relevant_findings"]
    assert planner_context["relevant_findings"][0]["target"] == "10.0.0.1"
    assert planner_context["known_open_port_findings_count"] == 1
    assert planner_context["working_memory_summary"]
    assert len(planner_context["working_memory_summary"]) <= 900
    assert "10.0.0.1" in planner_context["working_memory_summary"]
    assert "long_term_memory_summary" not in planner_context


def test_planner_context_includes_materialized_prior_turn_references() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conv-prior",
        turn_id="turn-prior",
        turn_sequence=3,
        messages=[{"role": "user", "content": "continue prior request"}],
        runtime_state=RuntimeStateSnapshot(
            active_target=None,
            current_goal=None,
            current_decision=None,
            in_flight_tool=None,
            handles={},
            active_todo=None,
        ),
    )
    update_prior_turn_references(
        bundle,
        {
            "operation": "continuation",
            "status": "ok",
            "materialized_turns": [
                {
                    "turn_number": 2,
                    "speaker": "user",
                    "message_id": 44,
                    "text": "Run the service enumeration step.",
                }
            ],
            "unresolved_hints": [{"anchor_text": "MODEL ONLY"}],
        },
    )
    state = InteractiveState(
        facts=FactsState(
            task_id=2,
            message="continue that",
            capability="simple_tool_execution",
            metadata={METADATA_CONTEXT_BUNDLE_KEY: bundle},
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue that",
        task_id=2,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert "Run the service enumeration step." in planner_context["referenced_prior_turns"]
    assert "MODEL ONLY" not in planner_context["referenced_prior_turns"]


def test_planner_context_projects_current_turn_phase_memory() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=3,
            message="continue discovery",
            capability="simple_tool_execution",
            metadata={
                "turn_sequence": 12,
                "current_turn_runtime_controls": {
                    "turn_sequence": 12,
                    "unavailable_tools": [
                        "information_gathering.network_discovery.netdiscover"
                    ],
                },
                "working_memory": {
                    "current_turn_phases": [
                        {
                            "turn_sequence": 12,
                            "phase_sequence": 0,
                            "source": "tool",
                            "sections": [
                                {
                                    "heading": "Tool Executed",
                                    "body": (
                                        "information_gathering.network_discovery."
                                        "netdiscover(target='172.17.0.0/24')"
                                    ),
                                },
                                {
                                    "heading": "Tool Output Summary",
                                    "body": "bash: netdiscover: command not found",
                                },
                            ],
                        }
                    ],
                    "current_turn_phase_counter": 1,
                    "current_turn_phase_turn": 12,
                },
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=["172.17.0.0/24"],
        message="continue discovery",
        task_id=3,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    phase_records = planner_context["working_memory"]["current_turn_phases"]
    assert phase_records[0]["sections"][0]["heading"] == "Tool Executed"
    assert planner_context["current_turn_unavailable_tools"] == [
        "information_gathering.network_discovery.netdiscover"
    ]
    working_memory_summary = planner_context["working_memory_summary"]
    assert "Prior Current-Turn Phase Memory" in working_memory_summary
    assert "<phase turn=12 phase=0 source=tool>" in working_memory_summary
    assert "## Tool Executed" in working_memory_summary
    assert "## Tool Output Summary" in working_memory_summary
    assert "netdiscover" in working_memory_summary
    assert "tool_unavailable" not in working_memory_summary


def test_unavailable_capability_projection_writes_ptr_readable_phase_memory() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=33,
            message="resolve hostname with a missing resolver",
            capability="deep_reasoning",
            current_goal="Confirm target DNS resolution",
            metadata={
                "turn_sequence": 4,
                "tool_intent": {
                    "description": "Resolve target with unavailable DNS tooling",
                    "target": "cve-2018-7600-web-1",
                    "focus": "DNS",
                },
            },
        )
    )
    plan = ActionPlan(
        type=ActionType.GATHER_INFO,
        target="cve-2018-7600-web-1",
        selected_tools=[UNAVAILABLE_CAPABILITY_TOOL],
        tool_parameters={},
        execution_strategy=ExecutionStrategy.SEQUENTIAL,
        reasoning="No exposed DNS lookup tool can satisfy the intent.",
        expected_outcome="Return unavailable capability to PTR.",
        candidate_tools=[UNAVAILABLE_CAPABILITY_TOOL],
        tool_batch=None,
    )

    apply_unavailable_capability_to_state(state, plan)

    metadata = state.facts.metadata
    assert metadata[UNAVAILABLE_CAPABILITY_METADATA_KEY]["active"] is True
    assert "planner_plan" not in metadata
    assert metadata["synthesized_output"]["status"] == "unavailable_capability"
    assert metadata["last_tool_result_compact"]["status"] == "unavailable_capability"
    assert metadata["last_tool_result"]["success"] is False

    ledger = get_ledger(metadata)
    assert ledger[-1]["source"] == "tool"
    assert ledger[-1]["sections"][0]["heading"] == "Tool Selection"
    rendered = render_phase_memory_section(metadata, turn_sequence=4)
    assert "<phase turn=4 phase=0 source=tool>" in rendered
    assert "## Tool Selection" in rendered
    assert "selected_tools: unavailable_capability" in rendered
    assert "## Tool Output Summary" in rendered


def test_planner_resolves_target_from_working_memory_for_vague_followup() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=7,
            message="so scan it then",
            capability="simple_tool_execution",
            metadata={
                "working_memory": {
                    "stage": "tool_selection",
                    "active": {"target_id": "target:intent:target"},
                    "referents": {"intent:target": {"value": "172.17.0.1"}},
                    "recent_turns": [
                        {
                            "role": "assistant",
                            "content_excerpt": "To find open services on 172.17.0.1, run nmap --top-ports 1000.",
                        },
                        {"role": "user", "content_excerpt": "so scan it then"},
                    ],
                }
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="so scan it then",
        task_id=7,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)
    action = _build_action_for_planner(state, request)

    assert planner_context["targets"] == ["172.17.0.1"]
    assert action.target == "172.17.0.1"


def test_planner_resolves_target_from_recent_history_when_referent_missing() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=8,
            message="so scan it then",
            capability="simple_tool_execution",
            metadata={
                "working_memory": {
                    "stage": "tool_selection",
                    "active": {"target_id": None},
                    "referents": {},
                    "recent_turns": [
                        {
                            "role": "assistant",
                            "content_excerpt": "To find open services on 172.17.0.1, run nmap --top-ports 1000.",
                        },
                        {"role": "user", "content_excerpt": "so scan it then"},
                    ],
                }
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="so scan it then",
        task_id=8,
        history=[
            {"role": "assistant", "content": "Run nmap --top-ports 1000 172.17.0.1"},
            {"role": "user", "content": "so scan it then"},
        ],
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)
    action = _build_action_for_planner(state, request)

    assert planner_context["targets"] == ["172.17.0.1"]
    assert action.target == "172.17.0.1"


def test_relevant_findings_helper_matches_planner_behavior() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=13,
            message="continue http work",
            current_goal="Enumerate web services",
            capability="simple_tool_execution",
            metadata={
                "next_tool_hint": "focus on https service",
                "tool_intent": {"focus": "https"},
                "last_tool_result": {
                    "parameters": {"target": "10.0.0.44", "ports": "80,443"},
                },
                "working_memory": {
                    "active": {"target_id": None},
                    "referents": {},
                    "available_findings": [
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.44",
                            "subject": "10.0.0.44:443/tcp",
                            "details": {"service": "https"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.9",
                            "subject": "10.0.0.9:22/tcp",
                            "details": {"service": "ssh"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                    ],
                },
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=["10.0.0.44"],
        message="continue http work",
        task_id=13,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)
    working_memory = state.facts.metadata["working_memory"]
    resolved_target = planner_context["targets"][0] if planner_context["targets"] else ""

    expected = select_relevant_findings_for_prompt(
        available_findings=working_memory["available_findings"],
        target=resolved_target,
        subject_hint_components=(
            state.facts.current_goal,
            state.facts.metadata.get("next_tool_hint"),
            state.facts.metadata["last_tool_result"]["parameters"],
            state.facts.metadata["tool_intent"]["focus"],
        ),
        limit=8,
    )

    assert planner_context["relevant_findings"] == expected


def test_planner_relevant_findings_preserve_request_target_fallback() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=11,
            message="scan that host",
            capability="simple_tool_execution",
            metadata={
                "working_memory": {
                    "active": {"target_id": None},
                    "referents": {},
                    "available_findings": [
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.44",
                            "subject": "10.0.0.44:443/tcp",
                            "details": {"service": "https"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        }
                    ],
                }
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=["10.0.0.44"],
        message="scan that host",
        task_id=11,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert planner_context["targets"] == ["10.0.0.44"]
    assert planner_context["relevant_findings"]
    assert planner_context["relevant_findings"][0]["target"] == "10.0.0.44"


def test_planner_relevant_findings_preserve_tool_intent_target_fallback() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=12,
            message="continue",
            capability="simple_tool_execution",
            metadata={
                "tool_intent": {"target": "10.0.0.77", "focus": "https"},
                "working_memory": {
                    "active": {"target_id": None},
                    "referents": {},
                    "available_findings": [
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.77",
                            "subject": "10.0.0.77:443/tcp",
                            "details": {"service": "https"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        }
                    ],
                },
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue",
        task_id=12,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert planner_context["targets"] == ["10.0.0.77"]
    assert planner_context["relevant_findings"]
    assert planner_context["relevant_findings"][0]["target"] == "10.0.0.77"


def test_planner_relevant_findings_preserve_classifier_target_fallback() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=14,
            message="continue",
            capability="simple_tool_execution",
            metadata={
                "intent_target_resolution": {
                    "target_status": "resolved",
                    "resolved_target": "10.0.0.55",
                },
                "intent_target_continuity": {"status": "disallow"},
                "tool_intent": {"target": "10.0.0.99", "focus": "https"},
                "working_memory": {
                    "active": {"target_id": None},
                    "referents": {},
                    "available_findings": [
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.55",
                            "subject": "10.0.0.55:443/tcp",
                            "details": {"service": "https"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                        {
                            "kind": "service_detected",
                            "target": "10.0.0.99",
                            "subject": "10.0.0.99:443/tcp",
                            "details": {"service": "https"},
                            "assertion_level": "observed",
                            "confidence": 1.0,
                            "seen_at": 1_713_870_000,
                            "ttl_seconds": 9_999_999_999,
                        },
                    ],
                },
            },
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue",
        task_id=14,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)
    action = _build_action_for_planner(state, request)

    assert planner_context["targets"] == ["10.0.0.55"]
    assert action.target == "10.0.0.55"
    assert planner_context["relevant_findings"]
    assert planner_context["relevant_findings"][0]["target"] == "10.0.0.55"


def test_planner_context_omits_long_term_memory_summary_even_when_metadata_sets_it() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=9,
            message="continue",
            capability="simple_tool_execution",
            metadata={"long_term_memory_summary": "Remember prior scan found SSH on 10.0.0.5"},
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue",
        task_id=9,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert "long_term_memory_summary" not in planner_context


def test_planner_context_omits_long_term_memory_summary_when_metadata_is_none() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=10,
            message="continue",
            capability="simple_tool_execution",
            metadata={"long_term_memory_summary": None},
        )
    )
    request = ToolExecutionRequest(
        capability="simple_tool_execution",
        targets=[],
        message="continue",
        task_id=10,
        metadata=state.facts.metadata,
    )

    planner_context = _build_planner_context(state, request)

    assert "long_term_memory_summary" not in planner_context
