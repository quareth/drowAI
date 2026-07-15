"""Characterization tests for approval edit handling in tool execution.

These tests lock the pre-refactor approval-edit contract before helper
extraction. They do not introduce new approval behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.execution_strategy import ExecutionStrategy
from agent.graph.subgraphs.tool_execution_runtime.approval_and_idempotency import (
    _apply_approval_edits_to_batch,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall, ToolCallStatus


def test_invalid_approval_edit_returns_failed_row_and_removes_call(monkeypatch) -> None:
    """Invalid edited parameters fail that call before batch execution."""
    from agent.tools import parameter_validation

    batch = ToolBatch(
        tool_batch_id="tb_approval_edits",
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
        tool_calls=(
            ToolCall(
                tool_call_id="tc_shell",
                tool_id="shell.exec",
                parameters={"command": "echo original"},
            ),
            ToolCall(
                tool_call_id="tc_http",
                tool_id="http.request",
                parameters={"url": "https://example.test"},
            ),
        ),
    )

    monkeypatch.setattr(
        parameter_validation,
        "validate_tool_parameters",
        lambda *_args, **_kwargs: SimpleNamespace(
            valid=False,
            reason="missing command",
            normalized_parameters={},
        ),
    )

    edited_batch, failed_rows = _apply_approval_edits_to_batch(
        batch,
        {
            "decisions": {
                "tc_shell": {
                    "action": "edit",
                    "edited_parameters": {"command": ""},
                },
            },
        },
        logger=None,
    )

    assert [call.tool_call_id for call in edited_batch.tool_calls] == ["tc_http"]
    assert len(failed_rows) == 1
    assert failed_rows[0].tool_call_id == "tc_shell"
    assert failed_rows[0].tool_id == "shell.exec"
    assert failed_rows[0].status is ToolCallStatus.FAILED
    assert failed_rows[0].failure_category == "invalid_edited_parameters"
    assert failed_rows[0].error_message == "missing command"
