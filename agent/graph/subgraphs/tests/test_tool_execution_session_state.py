"""Tests for exact process-local shell interaction transcript materialization."""

from core.prompts.builders.post_tool.evidence import EvidenceView

from agent.graph.subgraphs import tool_execution_session_state as session_state
from agent.graph.subgraphs.tool_execution_session_state import (
    abort_execution_session_state,
    append_shell_interaction_transcript,
    begin_execution_session_state,
    materialize_terminal_session_output,
    read_shell_interaction_transcript,
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


def _append_output(sequence_id: str, *, call_id: str, stdout: str) -> None:
    row = {
        "tool_id": "shell.utility",
        "tool_call_id": call_id,
        "compact_tool_result": {
            "stdout": stdout,
            "stderr": "",
            "process_status": "running",
        },
    }
    append_shell_interaction_transcript(
        sequence_id=sequence_id,
        evidence=EvidenceView(
            source="batch",
            status="completed",
            success=True,
            rows=(row,),
            successful_rows=(row,),
        ),
        metadata={},
    )


def _transcript_chars(transcript: dict) -> int:
    total = len(str(transcript.get("originating_command") or ""))
    for entry in transcript.get("entries", []):
        total += len(str(entry.get("input") or ""))
        total += len(str(entry.get("stdout") or ""))
        total += len(str(entry.get("stderr") or ""))
    return total


def test_single_oversized_entry_is_bounded_and_keeps_latest_output() -> None:
    sequence_id = "oversized-single-entry"
    latest_output = "LATEST_PROMPT>"
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "python3 noisy.py"},
    )
    try:
        _append_output(
            sequence_id,
            call_id="call-single",
            stdout=("x" * 128_000) + latest_output,
        )

        transcript = read_shell_interaction_transcript(sequence_id)

        assert transcript is not None
        assert _transcript_chars(dict(transcript)) <= session_state._TRANSCRIPT_MAX_CHARS
        assert transcript["originating_command"] == "python3 noisy.py"
        assert transcript["entries"][0]["stdout"].endswith(latest_output)
        assert transcript["entries"][0]["truncated"] is True
        assert transcript["compacted"] is True
    finally:
        abort_execution_session_state(sequence_id)


def test_two_oversized_recent_entries_are_bounded_without_dropping_them() -> None:
    sequence_id = "oversized-two-entries"
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "python3 interactive.py"},
    )
    try:
        _append_output(
            sequence_id,
            call_id="call-older",
            stdout=("a" * 8_000) + "OLDER_TAIL",
        )
        _append_output(
            sequence_id,
            call_id="call-newest",
            stdout=("b" * 8_000) + "NEWEST_PROMPT>",
        )

        transcript = read_shell_interaction_transcript(sequence_id)

        assert transcript is not None
        assert len(transcript["entries"]) == 2
        assert _transcript_chars(dict(transcript)) <= session_state._TRANSCRIPT_MAX_CHARS
        assert transcript["originating_command"] == "python3 interactive.py"
        assert transcript["entries"][0]["stdout"].endswith("OLDER_TAIL")
        assert transcript["entries"][1]["stdout"].endswith("NEWEST_PROMPT>")
        assert any(entry["truncated"] for entry in transcript["entries"])
        assert transcript["compacted"] is True
    finally:
        abort_execution_session_state(sequence_id)


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
