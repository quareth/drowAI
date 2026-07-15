import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.graph.adapters.tool_interface import normalize_tool_arguments


def test_normalize_tool_arguments_handles_nested_structures():
    raw = {
        "target": "127.0.0.1",
        "ports": {80, 443},
        "options": ("-sV", "--top-ports=100"),
        "metadata": {"timeout": 30},
    }

    normalized = normalize_tool_arguments(raw)

    assert normalized["target"] == "127.0.0.1"
    assert isinstance(normalized["ports"], list)
    assert "-sV" in normalized["options"]
    assert normalized["metadata"]["timeout"] == 30
