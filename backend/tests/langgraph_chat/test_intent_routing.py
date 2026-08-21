"""Unit tests for LangGraph facade branch selection helpers."""

from __future__ import annotations

import pytest

from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.subagents.registry import get_subagent_registry
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.routing.selectors import (
    ChatBranch,
    resolve_branch,
    resolve_late_subagent_handoff,
    select_branch,
)


def _runtime_config(mode: ExecutionMode) -> LangGraphRuntimeConfig:
    return LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=1,
            user_id=1,
            message="test",
            conversation_id="conv-1",
            history=[],
        ),
        execution_mode=mode,
        metadata={},
    )


def test_select_branch_normal_chat() -> None:
    assert select_branch(_runtime_config(ExecutionMode.NORMAL_CHAT)) is ChatBranch.NORMAL_CHAT


def test_select_branch_deep_reasoning() -> None:
    assert select_branch(_runtime_config(ExecutionMode.DEEP_REASONING)) is ChatBranch.DEEP_REASONING


def test_select_branch_simple_tool() -> None:
    assert select_branch(_runtime_config(ExecutionMode.SIMPLE_TOOL)) is ChatBranch.SIMPLE_TOOL


def test_resolve_branch_routes_pathfinder_owned_direct_executor_by_default() -> None:
    registry = get_subagent_registry()
    pathfinder_name = registry.classifier_catalog()[0]["name"]
    assert pathfinder_name == "pathfinder"

    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata.update(
        {
            "intent_classifier_label": "direct_executor",
            "intent_classifier_raw_response": {
                "suggested_capabilities": ["port scanning", "service enumeration"],
                "agent_handoffs": [
                    {
                        "agent_handoff": "required",
                        "subagent": pathfinder_name,
                        "objective": "Scan ports and enumerate services on 10.0.0.10.",
                        "skill_ids": [],
                    }
                ],
            },
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
        }
    )

    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
        )
        is ChatBranch.SUBAGENT
    )
    assert config.metadata["subagent_routing"]["agent_id"] == "pathfinder"
    assert config.metadata["subagent_routing"]["capabilities"] == [
        "port_scanning",
        "service_enumeration",
    ]
    assert (
        config.metadata["subagent_routing"]["objective"]
        == "Scan ports and enumerate services on 10.0.0.10."
    )


def test_resolve_branch_uses_handoff_instead_of_capability_vocabulary() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata.update(
        {
            "intent_classifier_label": "direct_executor",
            "intent_classifier_raw_response": {
                "suggested_capabilities": ["port scanning", "report"],
                "agent_handoffs": [
                    {
                        "agent_handoff": "required",
                        "subagent": "pathfinder",
                        "objective": "Scan ports on 10.0.0.10.",
                        "skill_ids": [],
                    }
                ],
            },
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
        }
    )

    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
        )
        is ChatBranch.SUBAGENT
    )
    assert config.metadata["subagent_routing"]["reason"] == "pathfinder_owned"


def test_resolve_branch_rejects_active_local_pathfinder_run() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata.update(
        {
            "intent_classifier_label": "direct_executor",
            "intent_classifier_raw_response": {
                "suggested_capabilities": ["port scanning"],
                "agent_handoffs": [
                    {
                        "agent_handoff": "required",
                        "subagent": "pathfinder",
                        "objective": "Scan ports on 10.0.0.10.",
                        "skill_ids": [],
                    }
                ],
            },
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
        }
    )

    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
            active_subagent_run_counts={"pathfinder": 1},
        )
        is ChatBranch.SIMPLE_TOOL
    )
    assert config.metadata["subagent_routing"]["reason"] == "subagent_unavailable"


def _late_handoff_state(*, action: str = "delegate_subagent") -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=1,
            message="delegate this work",
            capability="simple_tool_execution",
            metadata={
                "intent_classifier_label": "direct_executor",
                "intent_hints": {
                    "classifier_label": "direct_executor",
                    "targets": ["10.0.0.10"],
                },
                "runtime_budgets": {"remaining_tool_calls": 4},
                "router_outcome": {
                    "action": action,
                    "candidate_id": "ptr-delegate-1",
                    "agent_handoff": {
                        "agent_handoff": "required",
                        "subagent": "pathfinder",
                        "objective": "Scan ports on 10.0.0.10.",
                        "skill_ids": [],
                    },
                },
            },
        ),
        trace=TraceState(reasoning=["PTR selected Pathfinder."]),
    )


def test_resolve_late_handoff_reuses_canonical_subagent_routing() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata["turn_id"] = "task-1-turn-1"

    routed = resolve_late_subagent_handoff(
        config,
        _late_handoff_state(),
        subagent_registry=get_subagent_registry(),
    )

    assert routed is not None
    assert routed.metadata["subagent_routing"]["should_delegate"] is True
    assert routed.metadata["subagent_routing"]["agent_id"] == "pathfinder"
    assert routed.metadata["subagent_routing"]["objective"] == (
        "Scan ports on 10.0.0.10."
    )
    assert routed.metadata["subagent_routing"]["delegation_source"] == "ptr"
    assert routed.metadata["runtime_budgets"] == {"remaining_tool_calls": 4}
    assert "subagent_routing" not in config.metadata


def test_resolve_late_handoff_ignores_non_delegation_outcome() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata["turn_id"] = "task-1-turn-1"

    assert (
        resolve_late_subagent_handoff(
            config,
            _late_handoff_state(action="finalize"),
            subagent_registry=get_subagent_registry(),
        )
        is None
    )


def test_resolve_late_handoff_fails_closed_when_subagent_is_unavailable() -> None:
    config = _runtime_config(ExecutionMode.SIMPLE_TOOL)
    config.metadata["turn_id"] = "task-1-turn-1"

    with pytest.raises(RuntimeError, match="subagent_unavailable"):
        resolve_late_subagent_handoff(
            config,
            _late_handoff_state(),
            active_subagent_run_counts={"pathfinder": 1},
            subagent_registry=get_subagent_registry(),
        )
