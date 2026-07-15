"""Unit tests for chain-aware post-tool intent-contract matching.

These tests lock the direct-executor bugfix that allows the intent contract to
consider earlier same-turn structured executions before declaring a mismatch on
the latest helper step.
"""

from __future__ import annotations

from typing import Any, Dict

from agent.graph.nodes.post_tool_reasoning.policies.intent_contract.matching import (
    _evaluate_simple_tool_intent_contract,
)
from agent.graph.state import FactsState, InteractiveState, TraceState


def _planner_plan(tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tool_batch": {
            "tool_batch_id": "tb_test",
            "requested_execution_strategy": "sequential",
            "tool_calls": [
                {
                    "tool_call_id": "tc_test",
                    "tool_id": tool_id,
                    "parameters": params,
                }
            ],
        }
    }


def _make_state(
    *,
    message: str,
    selected_tool: str | None,
    tool_parameters: Dict[str, Any] | None = None,
    metadata: Dict[str, Any] | None = None,
    intent_hints: Dict[str, Any] | None = None,
) -> InteractiveState:
    facts = FactsState(
        task_id=7,
        message=message,
        capability="simple_tool_execution",
        conversation_id="conv-1",
        selected_tool=selected_tool,
        tool_parameters=tool_parameters or {},
        metadata=metadata or {},
        intent_hints=intent_hints or {},
    )
    return InteractiveState(facts=facts, trace=TraceState())


def test_intent_contract_matches_current_step() -> None:
    selected_tool = "information_gathering.network_discovery.nmap"
    state = _make_state(
        message="scan 127.0.0.1 with nmap for port 5000",
        selected_tool=selected_tool,
        metadata={
            "turn_sequence": 3,
            "planner_plan": _planner_plan(
                selected_tool,
                {"target": "127.0.0.1", "ports": "5000"},
            ),
        },
    )

    contract = _evaluate_simple_tool_intent_contract(state)

    assert contract["satisfied"] is True
    assert contract["matched_via"] == "current_step"
    assert contract["executed_tool"] == "nmap"
    assert contract["executed_targets"] == ["127.0.0.1"]
    assert contract["executed_ports"] == ["5000"]


def test_intent_contract_matches_prior_same_turn_step() -> None:
    state = _make_state(
        message="Then lets try to find this redirect thing again on 10.129.31.138",
        selected_tool="filesystem.read_file",
        tool_parameters={"filesystem.read_file": {"path": "artifacts/redirect.txt"}},
        intent_hints={"targets": ["10.129.31.138"]},
        metadata={
            "turn_sequence": 11,
            "planner_plan": _planner_plan(
                "filesystem.read_file",
                {"path": "artifacts/redirect.txt"},
            ),
            "action_history": [
                {
                    "tool_id": "information_gathering.web_enumeration.http_request",
                    "params": {"target": "http://10.129.31.138/capture"},
                    "turn_sequence": 11,
                }
            ],
        },
    )

    contract = _evaluate_simple_tool_intent_contract(state)

    assert contract["satisfied"] is True
    assert contract["matched_via"] == "prior_step"
    assert contract["executed_tool"] == "http_request"
    assert contract["executed_targets"] == ["10.129.31.138"]
    assert contract["mismatches"] == []


def test_intent_contract_ignores_prior_step_from_different_turn() -> None:
    state = _make_state(
        message="Then lets try to find this redirect thing again on 10.129.31.138",
        selected_tool="filesystem.read_file",
        tool_parameters={"filesystem.read_file": {"path": "artifacts/redirect.txt"}},
        intent_hints={"targets": ["10.129.31.138"]},
        metadata={
            "turn_sequence": 11,
            "planner_plan": _planner_plan(
                "filesystem.read_file",
                {"path": "artifacts/redirect.txt"},
            ),
            "action_history": [
                {
                    "tool_id": "information_gathering.web_enumeration.http_request",
                    "params": {"target": "http://10.129.31.138/capture"},
                    "turn_sequence": 10,
                }
            ],
        },
    )

    contract = _evaluate_simple_tool_intent_contract(state)

    assert contract["satisfied"] is False
    assert contract["matched_via"] is None
    assert contract["target_match"] is False
    assert "target mismatch" in contract["mismatches"]


def test_intent_contract_matches_prior_step_ports() -> None:
    state = _make_state(
        message="scan 127.0.0.1 with nmap for port 5000",
        selected_tool="filesystem.read_file",
        tool_parameters={"filesystem.read_file": {"path": "artifacts/ports.txt"}},
        metadata={
            "turn_sequence": 4,
            "action_history": [
                {
                    "tool_id": "information_gathering.network_discovery.nmap",
                    "params": {"target": "127.0.0.1", "ports": "5000"},
                    "turn_sequence": 4,
                }
            ],
        },
    )

    contract = _evaluate_simple_tool_intent_contract(state)

    assert contract["satisfied"] is True
    assert contract["matched_via"] == "prior_step"
    assert contract["ports_match"] is True
    assert contract["executed_ports"] == ["5000"]
