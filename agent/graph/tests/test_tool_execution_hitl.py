"""Focused tests for HITL tool-execution helper behavior."""

from agent.graph.state import InteractiveInput
from agent.graph.subgraphs.tool_execution import (
    _build_skipped_tool_result,
    _get_tool_risk_level,
)
from agent.graph.tests._state_assertions import assert_no_raw_tool_output_in_state


def test_build_skipped_tool_result_sets_metadata() -> None:
    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={},
    )
    interactive = payload.to_state()

    result = _build_skipped_tool_result(
        interactive=interactive,
        tool_name="network.nmap",
        user_response={"action": "skip"},
    )

    assert result["facts"]["metadata"]["tool_skipped"] is True
    assert result["facts"]["metadata"]["skipped_tool"] == "network.nmap"
    assert_no_raw_tool_output_in_state(result["facts"]["metadata"])
    assert interactive.trace.executed_tools[-1].status == "skipped"


def test_exploitation_tools_prefix_is_high_risk() -> None:
    assert _get_tool_risk_level("exploitation_tools.metasploit.run_exploit") == "high"
