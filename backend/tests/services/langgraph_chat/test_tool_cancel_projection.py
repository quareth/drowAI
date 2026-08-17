"""Regression tests for supported and unsupported chat tool cancellation projection."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.services.langgraph_chat.runtime.tool_cancel_service import (
    ChatToolCancelProjectionService,
)
from backend.services.langgraph_chat.runtime.tool_cancel_stream_projection import (
    ChatToolCancelStreamProjectionService,
    StreamToolIdentity,
)


def _unsupported_row() -> SimpleNamespace:
    return SimpleNamespace(
        id="execution-1",
        task_id=12,
        turn_id="task-12-turn-1",
        tool_call_id="tool-call-1",
        tool_name="shell.exec",
        command_id="command-1",
        conversation_id="conversation-1",
        turn_sequence=1,
        duration_ms=None,
        exit_code=None,
        execution_metadata={
            "cancellation": {
                "cancel_requested": True,
                "process_state": "orphaned_until_terminal",
                "runtime_kill_attempted": False,
                "runtime_kill_supported": False,
            }
        },
    )


def test_unsupported_cancel_does_not_persist_terminal_lifecycle_event() -> None:
    db = Mock()
    service = ChatToolCancelProjectionService(db, repository=Mock())
    row = _unsupported_row()

    with patch(
        "backend.services.langgraph_chat.runtime.tool_cancel_service.ChatTurnEventService"
    ) as event_service:
        service._persist_canonical_terminal_events(
            rows=[row],
            runtime_metadata={
                "process_state": "orphaned_until_terminal",
                "runtime_kill_attempted": False,
                "runtime_kill_supported": False,
            },
        )

    event_service.return_value.append_terminal_tool_lifecycle_event.assert_not_called()
    assert "canonical_turn_event_persisted" not in row.execution_metadata["cancellation"]


def test_unsupported_cancel_live_event_preserves_nonterminal_runtime_facts() -> None:
    event = ChatToolCancelStreamProjectionService._tool_end_event(
        row=_unsupported_row(),
        fallback_turn_id="task-12-turn-1",
    )

    assert event["content"] == "Tool cancellation requested"
    metadata = event["metadata"]
    assert metadata["status"] == "cancel_requested"
    assert metadata["process_state"] == "orphaned_until_terminal"
    assert metadata["runtime_kill_attempted"] is False
    assert metadata["runtime_kill_supported"] is False
    assert "process_status" not in metadata
    assert "session_status" not in metadata
    assert "interaction_boundary" not in metadata
    assert "compact_tool_result" not in metadata


def test_unsupported_cancel_batch_preserves_nonterminal_runtime_facts() -> None:
    row = _unsupported_row()
    identity = StreamToolIdentity(
        tool_call_id=row.tool_call_id,
        tool_batch_id="batch-1",
        tool_name=row.tool_name,
        conversation_id=row.conversation_id,
        turn_sequence=row.turn_sequence,
    )

    event = ChatToolCancelStreamProjectionService._tool_batch_end_event(
        batch_id="batch-1",
        grouped_rows=[(row, identity)],
        batch_identity=None,
        terminal_tool_statuses={},
        fallback_turn_id=row.turn_id,
    )

    assert event["content"] == "Tool batch cancellation requested"
    metadata = event["metadata"]
    assert metadata["status"] == "cancel_requested"
    assert metadata["results"][0]["status"] == "cancel_requested"
    assert metadata["calls"][0]["status"] == "cancel_requested"
    assert "process_status" not in metadata
    assert "session_status" not in metadata
    assert "interaction_boundary" not in metadata
