import copy

from agent.graph.state import (
    FactsState,
    InteractiveInput,
    ToolExecutionRecord,
)


def test_facts_state_defaults_include_tool_fields() -> None:
    facts = FactsState(task_id=123, message="scan target")

    assert facts.tool_candidates == []
    assert facts.selected_tool is None
    assert facts.tool_parameters == {}


def test_interactive_input_transfers_tool_metadata() -> None:
    metadata = {
        "tool_candidates": ["information_gathering.network_discovery.nmap"],
        "selected_tool": "information_gathering.network_discovery.nmap",
        "tool_parameters": {"target": "127.0.0.1"},
        "intent_hints": {"tool_hints": ["network_scan"]},
    }
    original = copy.deepcopy(metadata)

    interactive_input = InteractiveInput(task_id=456, message="Please scan", metadata=metadata)
    state = interactive_input.to_state()

    assert state.facts.tool_candidates == original["tool_candidates"]
    assert state.facts.selected_tool == original["selected_tool"]
    assert state.facts.tool_parameters == original["tool_parameters"]

    graph_update = state.as_graph_update()
    assert graph_update["facts"]["tool_candidates"] == original["tool_candidates"]
    assert graph_update["facts"]["selected_tool"] == original["selected_tool"]
    assert graph_update["facts"]["tool_parameters"] == original["tool_parameters"]

    # Ensure original metadata was not mutated during conversion
    assert metadata == original


def test_tool_execution_record_serializes_approval_metadata() -> None:
    record = ToolExecutionRecord(
        tool_id="information_gathering.network_discovery.nmap",
        args={"target": "127.0.0.1"},
        reasoning="Initial network sweep",
        approval_granted=True,
        approval_reason="auto-approved",
        approval_metadata={"approver": "system"},
    )

    dumped = record.model_dump()

    assert dumped["reasoning"] == "Initial network sweep"
    assert dumped["approval_granted"] is True
    assert dumped["approval_reason"] == "auto-approved"
    assert dumped["approval_metadata"] == {"approver": "system"}
