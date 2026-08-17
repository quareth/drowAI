"""Tests for canonical compact tool-result construction at chat boundaries."""

from backend.services.chat.compact_tool_result import (
    build_terminal_shell_compact_result,
    normalize_compact_tool_result,
)


def test_normalize_compact_tool_result_preserves_shell_lifecycle_extensions() -> None:
    result = normalize_compact_tool_result(
        tool="shell.utility",
        status="success",
        exit_code=0,
        summary={"summary": "done"},
        error=None,
        compact_tool_result={
            "process_status": "completed",
            "session_status": "closed",
            "interaction_boundary": "terminal",
            "session_id": "shs-1",
        },
    )

    assert result["summary"] == "done"
    assert result["key_findings"] == []
    assert result["errors"] == []
    assert result["process_status"] == "completed"
    assert result["session_status"] == "closed"
    assert result["interaction_boundary"] == "terminal"
    assert result["session_id"] == "shs-1"


def test_build_terminal_shell_compact_result_emits_complete_cancel_schema() -> None:
    result = build_terminal_shell_compact_result(
        tool="shell.exec",
        status="cancelled",
        process_status="terminated",
        session_id="shs-2",
        summary="Tool stopped",
        error="user_cancelled",
        close_reason="chat_stop",
        lifecycle_event="shell_session_terminal",
        failure_category="user_cancelled",
        output_persistence="transient",
    )

    assert result["summary"] == "Tool stopped"
    assert result["errors"] == ["user_cancelled"]
    assert result["key_findings"] == []
    assert result["report_recommendations"] == []
    assert result["structured_signals"] == []
    assert result["decision_evidence"] == []
    assert result["process_status"] == "terminated"
    assert result["session_status"] == "closed"
    assert result["interaction_boundary"] == "terminal"
    assert result["lifecycle_event"] == "shell_session_terminal"
