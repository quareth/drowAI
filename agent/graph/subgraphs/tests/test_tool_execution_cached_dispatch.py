"""Tests for cached tool-dispatch replay metadata.

These tests lock in the structured action-history contract used by the
chain-aware post-tool intent matcher. Cached dispatch replay must preserve the
same ``action_history`` shape as live projection, including ``turn_sequence``,
without changing the separate loop-detection fields.
"""

from __future__ import annotations

from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution_runtime.approval_and_idempotency import (
    apply_cached_dispatch_result,
    store_dispatch_cache_result,
)


def _make_state() -> InteractiveState:
    facts = FactsState(
        task_id=1,
        message="test",
        capability="simple_tool_execution",
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


def test_store_dispatch_cache_result_preserves_action_record_turn_sequence() -> None:
    interactive = _make_state()
    action_record = {
        "tool_id": "information_gathering.web_enumeration.http_request",
        "params": {"target": "http://10.129.31.138/capture"},
        "turn_sequence": 11,
    }

    store_dispatch_cache_result(
        facts=interactive.facts,
        tool_dispatch_cache_key="tool_dispatch_cache",
        tool_call_id="tc-1",
        compact_result_dict={"summary": "redirect captured"},
        result_for_metadata={"status": "success"},
        graph_metadata={"tool": "information_gathering.web_enumeration.http_request"},
        action_record=action_record,
        observation_text="Captured redirect response.",
        reasoning_additions=["HTTP request completed"],
        outcome_parameters={"target": "http://10.129.31.138/capture"},
        outcome_success=True,
        outcome_summary="redirect captured",
        approval_granted=True,
        approval_reason="approve",
        approval_metadata={},
    )

    cached = interactive.facts.metadata["tool_dispatch_cache"]["tc-1"]
    assert cached["action_record"] == action_record
    assert cached["action_record"]["turn_sequence"] == 11


def test_apply_cached_dispatch_result_replays_turn_sequence_into_action_history() -> None:
    interactive = _make_state()
    cached = {
        "last_tool_result_compact": {"summary": "redirect captured"},
        "last_tool_result": {"status": "success"},
        "action_record": {
            "tool_id": "information_gathering.web_enumeration.http_request",
            "params": {"target": "http://10.129.31.138/capture"},
            "turn_sequence": 11,
        },
        "tool_execution_history": [],
        "observation_text": "Captured redirect response.",
        "reasoning_additions": ["HTTP request completed"],
        "exec_record": {
            "args": {"target": "http://10.129.31.138/capture"},
            "status": "success",
            "observation": "Captured redirect response.",
            "reasoning": "redirect captured",
            "approval_granted": True,
            "approval_reason": "approve",
            "approval_metadata": {},
        },
    }

    apply_cached_dispatch_result(
        interactive,
        cached,
        "information_gathering.web_enumeration.http_request",
    )

    assert interactive.facts.metadata["action_history"] == [
        {
            "tool_id": "information_gathering.web_enumeration.http_request",
            "params": {"target": "http://10.129.31.138/capture"},
            "turn_sequence": 11,
        }
    ]
    assert interactive.trace.executed_tools[-1].tool_id == (
        "information_gathering.web_enumeration.http_request"
    )
