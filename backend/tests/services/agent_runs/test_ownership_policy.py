"""Tests for deterministic subagent ownership routing policy."""

from __future__ import annotations

from dataclasses import replace

from backend.services.agent_runs.ownership_policy import (
    SubagentRoutingDecision,
    normalize_agent_handoff_entries,
    resolve_subagent_handoff,
)
from backend.services.agent_runs.dispatch_plan import build_assignment
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from backend.services.langgraph_chat.contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from core.prompts.builders.post_tool.evidence import register_runtime_compact_evidence


def _metadata(
    *,
    raw_capabilities: list[str],
    targets: list[str] | None = None,
    classifier_label: str = "direct_executor",
    agent_handoffs: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "intent_classifier_label": classifier_label,
        "intent_classifier_raw_response": {
            "suggested_capabilities": raw_capabilities,
            "agent_handoffs": agent_handoffs
            if agent_handoffs is not None
            else [
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": "Map exposed services on the approved target.",
                }
            ],
        },
        "intent_hints": {
            "classifier_label": classifier_label,
            "targets": targets if targets is not None else ["10.0.0.10"],
        },
    }


def _runtime_config_for_assignment(
    *,
    subagent_routing: dict[str, object],
    agent_mode: AgentMode = AgentMode.FULL_ACCESS,
    plan_mode: bool = False,
    reserved_message_id: int | None = None,
    extra_metadata: dict[str, object] | None = None,
) -> LangGraphRuntimeConfig:
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=3,
        message="Fallback message that must not replace the handoff objective.",
        conversation_id="conv-42",
        history=[],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        agent_mode=agent_mode,
        plan_mode=plan_mode,
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata={
            "tenant_id": 7,
            "graph_thread_id": "00000000000040008000000000000042",
            "runtime_placement_mode": "runner",
            "workspace_id": "task-42",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "runner_id": "runner-1",
            "execution_site_id": "site-1",
            "turn_sequence": 5,
            "plan_review_required": plan_mode,
            "reserved_message_id": reserved_message_id,
            "intent_classifier_label": "direct_executor",
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
            "subagent_routing": subagent_routing,
            "feature_flags": {"simple_tool_enabled": True},
            **(extra_metadata or {}),
        },
    )


def _routing_metadata(decision: SubagentRoutingDecision) -> dict[str, object]:
    return {
        "should_delegate": decision.should_delegate,
        "reason": decision.reason,
        "agent_id": decision.agent_id,
        "agent_kind": decision.agent_kind,
        "dispatch_branch": decision.dispatch_branch,
        "capabilities": list(decision.capabilities),
        "targets": list(decision.targets),
        "objective": decision.objective,
        "handoffs": [
            {
                "agent_id": handoff.agent_id,
                "agent_kind": handoff.agent_kind,
                "dispatch_branch": handoff.dispatch_branch,
                "reason": decision.reason,
                "capabilities": list(handoff.capabilities),
                "targets": list(handoff.targets),
                "objective": handoff.objective,
            }
            for handoff in decision.handoffs
        ],
    }


def test_shared_normalizer_accepts_single_three_field_mapping() -> None:
    entries = normalize_agent_handoff_entries(
        {
            "agent_handoff": " Required ",
            "subagent": " PathFinder ",
            "objective": "  Follow up on unresolved HTTP evidence.  ",
            "ignored": "not part of the contract",
        }
    )

    assert entries == (
        {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": "Follow up on unresolved HTTP evidence.",
        },
    )
    assert set(entries[0]) == {"agent_handoff", "subagent", "objective"}


def test_policy_accepts_direct_executor_recon_capabilities_with_targets() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["host discovery", "port scanning", "service enum"])
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.agent_id == "pathfinder"
    assert decision.capabilities == (
        "host_discovery",
        "port_scanning",
        "service_enumeration",
    )
    assert decision.targets == ("10.0.0.10",)
    assert decision.objective == "Map exposed services on the approved target."


def test_assignment_construction_preserves_classifier_authored_objective() -> None:
    authored_objective = (
        "Enumerate only approved services and report unresolved hosts explicitly."
    )
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scan"],
            agent_handoffs=[
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": f"  {authored_objective}  ",
                }
            ],
        )
    )
    assert decision.should_delegate is True

    assignment = build_assignment(
        _runtime_config_for_assignment(
            subagent_routing=_routing_metadata(decision)
        ),
        parent_turn_id="task-42-turn-5",
    )

    assert assignment.objective == authored_objective
    assert (
        assignment.objective
        != "Fallback message that must not replace the handoff objective."
    )
    assert assignment.targets == ("10.0.0.10",)
    assert assignment.suggested_capabilities == ("port_scanning",)
    assert assignment.relevant_context["ownership_reason"] == "pathfinder_owned"


def test_assignment_construction_preserves_parent_approval_policy() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["network_scan"])
    )
    assert decision.should_delegate is True

    assignment = build_assignment(
        _runtime_config_for_assignment(
            subagent_routing=_routing_metadata(decision),
            agent_mode=AgentMode.AGENT,
            plan_mode=True,
            reserved_message_id=815,
        ),
        parent_turn_id="task-42-turn-5",
    )

    assert assignment.relevant_context["agent_mode"] == "agent"
    assert assignment.relevant_context["reserved_message_id"] == 815


def test_assignment_carries_latest_compact_tool_outcome_with_invocation() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["network_scan"])
    )
    assert decision.should_delegate is True
    register_runtime_compact_evidence(
        {
            "tool_batch_id": "tb-parent-shell",
            "status": "failed",
            "success": False,
            "results": [
                {
                    "tool_call_id": "tc-parent-shell",
                    "tool_id": "shell.utility",
                    "intent": "Start the required calculator.",
                    "status": "failed",
                    "success": False,
                    "failure_category": "missing_dependency",
                    "compact_tool_result": {
                        "summary": "Command failed with exit code 127.",
                        "exit_code": 127,
                        "errors": ["bc: command not found"],
                    },
                }
            ],
        }
    )

    assignment = build_assignment(
        _runtime_config_for_assignment(
            subagent_routing=_routing_metadata(decision),
            extra_metadata={
                "planner_plan": {
                    "tool_batch": {
                        "tool_calls": [
                            {
                                "tool_call_id": "tc-parent-shell",
                                "tool_id": "shell.utility",
                                "parameters": {
                                    "command": "bc",
                                    "cwd": "/workspace",
                                    "interactive": True,
                                    "yield_time_ms": 1000,
                                },
                                "intent": "Start the required calculator.",
                            }
                        ]
                    }
                },
                "tool_batch_id": "tb-parent-shell",
            },
        ),
        parent_turn_id="task-42-turn-5",
    )

    assert assignment.relevant_context["prior_tool_outcomes"] == (
        {
            "status": "failed",
            "success": False,
            "calls": (
                {
                    "tool": "shell.utility",
                    "intent": "Start the required calculator.",
                    "invocation": {
                        "command": "bc",
                        "cwd": "/workspace",
                        "interactive": True,
                    },
                    "status": "failed",
                    "success": False,
                    "failure_category": "missing_dependency",
                    "summary": "Command failed with exit code 127.",
                    "exit_code": 127,
                    "errors": ("bc: command not found",),
                },
            ),
        },
    )


def test_followup_handoff_uses_shared_policy_and_assignment_builder() -> None:
    authored_objective = "Check only the unresolved HTTPS service evidence."
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["service_discovery"],
            classifier_label="normal_chat",
        ),
        handoff_entries={
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": authored_objective,
        },
        require_direct_executor=False,
    )
    assert decision.should_delegate is True

    assignment = build_assignment(
        _runtime_config_for_assignment(
            subagent_routing=_routing_metadata(decision)
        ),
        parent_turn_id="task-42-turn-5",
    )

    assert assignment.agent_id == "pathfinder"
    assert assignment.objective == authored_objective
    assert assignment.targets == ("10.0.0.10",)
    assert assignment.suggested_capabilities == ("service_enumeration",)
    assert assignment.relevant_context["ownership_reason"] == "pathfinder_owned"


def test_generic_policy_resolves_name_and_dispatch_from_injected_registry() -> None:
    probe = replace(
        get_subagent_registry().require("pathfinder"),
        id="probe",
        display_name="Probe",
    )
    metadata = _metadata(
        raw_capabilities=["port_scan"],
        agent_handoffs=[
            {
                "agent_handoff": "required",
                "subagent": "probe",
                "objective": "Probe the approved target.",
            }
        ],
    )

    decision = resolve_subagent_handoff(
        metadata,
        registry=SubagentRegistry((probe,)),
    )

    assert decision.should_delegate is True
    assert decision.agent_id == "probe"
    assert decision.agent_kind == "recon"
    assert decision.dispatch_branch == "subagent"
    assert decision.reason == "probe_owned"


def test_policy_accepts_ordered_multi_handoff_plan_from_registry() -> None:
    pathfinder = get_subagent_registry().require("pathfinder")
    probe = replace(
        pathfinder,
        id="probe",
        display_name="Probe",
    )
    metadata = _metadata(
        raw_capabilities=["port_scan"],
        agent_handoffs=[
            {
                "agent_handoff": "required",
                "subagent": "pathfinder",
                "objective": "Scan the approved target.",
            },
            {
                "agent_handoff": "required",
                "subagent": "probe",
                "objective": "Probe the approved target.",
            },
        ],
    )

    decision = resolve_subagent_handoff(
        metadata,
        registry=SubagentRegistry((pathfinder, probe)),
    )

    assert decision.should_delegate is True
    assert decision.reason == "ordered_handoff_plan"
    assert [handoff.agent_id for handoff in decision.handoffs] == [
        "pathfinder",
        "probe",
    ]
    assert [handoff.objective for handoff in decision.handoffs] == [
        "Scan the approved target.",
        "Probe the approved target.",
    ]
    assert decision.agent_id == "pathfinder"


def test_policy_rejects_invalid_handoff_inside_ordered_plan() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["port_scan"],
            agent_handoffs=[
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": "Scan the approved target.",
                },
                {
                    "agent_handoff": "required",
                    "subagent": "exploit",
                    "objective": "Exploit the target.",
                },
            ],
        )
    )

    assert decision.should_delegate is False
    assert decision.reason == "unsupported_agent_handoff"


def test_policy_accepts_classifier_network_scanning_vocabulary() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scanning", "service_discovery"],
            targets=["127.0.0.1"],
        )
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning", "service_enumeration")
    assert decision.targets == ("127.0.0.1",)


def test_policy_accepts_canonical_intent_classifier_network_scan_capability() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scan"],
            targets=["127.0.0.1"],
        )
    )

    assert decision.should_delegate is True
    assert decision.capabilities == ("port_scanning",)


def test_policy_accepts_port_enumeration_from_live_classifier_vocabulary() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["network_scanning", "port_enumeration"],
            targets=["127.0.0.1"],
        ),
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning",)
    assert decision.targets == ("127.0.0.1",)


def test_policy_ignores_generic_tool_route_hint_alongside_recon_capability() -> None:
    decision = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=["tool_call", "network_scan"],
            targets=["127.0.0.1:5432"],
        )
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning",)


def test_policy_routes_explicit_handoff_when_capabilities_are_empty() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=[], targets=["127.0.0.1:5432"]),
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ()


def test_policy_keeps_capabilities_advisory_for_explicit_handoff() -> None:
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port scanning", "report"])
    )

    assert decision.should_delegate is True
    assert decision.reason == "pathfinder_owned"
    assert decision.capabilities == ("port_scanning",)


def test_policy_rejects_missing_handoff_and_unbounded_scope() -> None:
    missing_handoff = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"], agent_handoffs=[])
    )
    missing_target = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"], targets=[])
    )

    assert missing_handoff.should_delegate is False
    assert missing_handoff.reason == "missing_agent_handoff"
    assert missing_target.should_delegate is False
    assert missing_target.reason == "invalid_assignment_scope"


def test_policy_rejects_unsupported_required_handoff() -> None:
    unsupported = resolve_subagent_handoff(
        _metadata(
            raw_capabilities=[],
            agent_handoffs=[
                {
                    "agent_handoff": "required",
                    "subagent": "exploit",
                    "objective": "Exploit the target.",
                }
            ],
        )
    )

    assert unsupported.should_delegate is False
    assert unsupported.reason == "unsupported_agent_handoff"


def test_policy_rejects_disabled_required_handoff() -> None:
    disabled = replace(get_subagent_registry().require("pathfinder"), enabled=False)
    decision = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"]),
        registry=SubagentRegistry((disabled,)),
    )

    assert decision.should_delegate is False
    assert decision.reason == "unsupported_agent_handoff"


def test_policy_rejects_non_direct_executor_and_active_local_run() -> None:
    non_direct = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"], classifier_label="plan_executor")
    )
    active = resolve_subagent_handoff(
        _metadata(raw_capabilities=["port_scan"]),
        active_runs_by_agent_id={"pathfinder": 1},
    )

    assert non_direct.should_delegate is False
    assert non_direct.reason == "classifier_not_direct_executor"
    assert active.should_delegate is False
    assert active.reason == "subagent_unavailable"
