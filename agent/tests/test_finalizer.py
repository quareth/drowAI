import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.graph.nodes.finalizer import finalize_turn
from agent.graph.state import FactsState, InteractiveState, TraceState


def test_finalize_turn_prefers_tool_summary():
    summaries = [
        {"tool": "nmap", "summary": "Scan complete with 1 open port."}
    ]
    state = InteractiveState(
        facts=FactsState(
            task_id=22,
            message="Fallback message",
            metadata={"tool_summaries": summaries},
        ),
        trace=TraceState(),
    )

    result = finalize_turn(state)

    assert result["trace"]["final_text"] == "Scan complete with 1 open port."


def test_finalize_turn_defaults_to_message_when_no_summary():
    state = InteractiveState(
        facts=FactsState(task_id=23, message="Hello world"),
        trace=TraceState(),
    )

    result = finalize_turn(state)

    assert result["trace"]["final_text"] == "Hello world"
