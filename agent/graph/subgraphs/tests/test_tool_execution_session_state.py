"""Tests for exact process-local shell interaction transcript materialization."""

from agent.graph.subgraphs.tool_execution_session_state import (
    materialize_terminal_session_output,
)


def _aggregate() -> dict:
    return {
        "results": [
            {
                "compact_tool_result": {
                    "stdout": "two",
                    "stderr": "",
                    "truncated": False,
                }
            }
        ]
    }


def test_materialize_restores_separator_between_complete_output_windows() -> None:
    materialized = materialize_terminal_session_output(
        _aggregate(),
        transcript={
            "entries": [
                {"stdout": "one", "stdout_ends_with_newline": True},
                {"stdout": "two", "stdout_ends_with_newline": True},
            ]
        },
    )

    compact = materialized["results"][-1]["compact_tool_result"]
    assert compact["stdout"] == "one\ntwo"


def test_materialize_does_not_separate_partial_output_windows() -> None:
    materialized = materialize_terminal_session_output(
        _aggregate(),
        transcript={
            "entries": [
                {"stdout": "one ", "stdout_ends_with_newline": False},
                {"stdout": "continued", "stdout_ends_with_newline": False},
            ]
        },
    )

    compact = materialized["results"][-1]["compact_tool_result"]
    assert compact["stdout"] == "one continued"
