"""Unit tests for post-tool intent-contract extraction."""

from agent.graph.nodes.post_tool_reasoning.policies.intent_contract.extraction import (
    _extract_expected_targets,
)
from agent.graph.state import FactsState, InteractiveState, TraceState


def test_tool_identifier_is_not_treated_as_an_expected_target() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=7,
            message="Use shell.utility against 127.0.0.1",
            capability="simple_tool_execution",
            conversation_id="conv-1",
        ),
        trace=TraceState(),
    )

    assert _extract_expected_targets(state) == ["127.0.0.1"]
