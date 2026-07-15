"""
Unit tests for chat transcript read-model query service.

Verifies deterministic cursor paging and compact item mapping from ChatMessage
and ToolCall persistence models without requiring a live database.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from backend.services.chat.transcript_query_service import ChatTranscriptQueryService, TranscriptCursor


def _make_message_execute_result(rows):
    result = Mock()
    result.scalars.return_value.unique.return_value.all.return_value = rows
    return result


def _make_scalar_execute_result(rows):
    result = Mock()
    result.scalars.return_value.all.return_value = rows
    return result


def _make_first_execute_result(row):
    result = Mock()
    result.first.return_value = row
    return result


def _make_turn_event(
    *,
    chat_message_id: int,
    phase_sequence: int,
    kind: str,
    content: str,
    sub_turn_index: int | None = None,
    tool_call_id: str | None = None,
    event_metadata: dict | None = None,
):
    row = Mock()
    row.chat_message_id = chat_message_id
    row.phase_sequence = phase_sequence
    row.kind = kind
    row.content = content
    row.sub_turn_index = sub_turn_index
    row.tool_call_id = tool_call_id
    row.event_metadata = event_metadata
    return row


def _make_message(
    *,
    message_id: int,
    conversation_id: str,
    turn_number: int,
    message_type: str,
    message: str,
    task_id: int = 1,
    reasoning_tokens: str | None = None,
    observation_tokens: str | None = None,
    tool_calls=None,
):
    row = Mock()
    row.id = message_id
    row.task_id = task_id
    row.conversation_id = conversation_id
    row.turn_number = turn_number
    row.message_type = message_type
    row.message = message
    row.reasoning_tokens = reasoning_tokens
    row.observation_tokens = observation_tokens
    row.tool_calls = tool_calls or []
    return row


def _make_tool_call(
    *,
    tool_id: int,
    chat_message_id: int,
    tool_call_id: str,
    tool_name: str,
    tool_result: str,
    turn_index: int = 0,
):
    row = Mock()
    row.id = tool_id
    row.chat_message_id = chat_message_id
    row.tool_call_id = tool_call_id
    row.tool_name = tool_name
    row.tool_arguments = {"query": "x"}
    row.tool_result = tool_result
    row.turn_index = turn_index
    row.parent_tool_call_id = None
    return row


def _make_tool_execution(
    *,
    execution_id: str,
    task_id: int,
    conversation_id: str,
    turn_id: str,
    tool_call_id: str,
    tool_name: str = "shell.exec",
    status: str = "cancel_requested",
    execution_metadata: dict | None = None,
):
    row = Mock()
    row.id = execution_id
    row.task_id = task_id
    row.conversation_id = conversation_id
    row.turn_id = turn_id
    row.tool_call_id = tool_call_id
    row.tool_name = tool_name
    row.status = status
    row.execution_metadata = execution_metadata or {}
    return row


def test_list_latest_transcript_page_maps_user_assistant_and_reasoning() -> None:
    conv_id = "conv-mapping"
    tool_call = _make_tool_call(
        tool_id=20,
        chat_message_id=11,
        tool_call_id="tc-11",
        tool_name="shell",
        tool_result="tool output",
    )
    user_message = _make_message(
        message_id=10,
        conversation_id=conv_id,
        turn_number=1,
        message_type="user",
        message="hello",
    )
    assistant_message = _make_message(
        message_id=11,
        conversation_id=conv_id,
        turn_number=1,
        message_type="assistant",
        message="working on it",
        reasoning_tokens="reasoning block",
        observation_tokens="observation block",
        tool_calls=[tool_call],
    )
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([1]),
        _make_message_execute_result([user_message, assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    assert page.conversation_id == conv_id
    assert page.has_more_older is False
    assert page.next_before is None
    assert [item.kind for item in page.items] == [
        "user",
        "assistant",
        "reasoning",
    ]
    assert page.items[0].content == "hello"
    assert page.items[1].content == "working on it"


def test_list_latest_transcript_page_splits_observation_sections_and_interleaves_with_tools() -> None:
    conv_id = "conv-observation-split"
    user_message = _make_message(
        message_id=20,
        conversation_id=conv_id,
        turn_number=2,
        message_type="user",
        message="run scan",
    )
    assistant_message = _make_message(
        message_id=21,
        conversation_id=conv_id,
        turn_number=2,
        message_type="assistant",
        message="working",
        observation_tokens='[{"content":"obs-a","sub_turn_index":0},{"content":"obs-b","sub_turn_index":1}]',
        tool_calls=[
            _make_tool_call(
                tool_id=31,
                chat_message_id=21,
                tool_call_id="tc-a",
                tool_name="nmap",
                tool_result="tool-a",
                turn_index=0,
            ),
            _make_tool_call(
                tool_id=32,
                chat_message_id=21,
                tool_call_id="tc-b",
                tool_name="nmap",
                tool_result="tool-b",
                turn_index=1,
            ),
        ],
    )
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([2]),
        _make_message_execute_result([user_message, assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    kinds = [item.kind for item in page.items]
    contents = [item.content for item in page.items]
    assert kinds == ["user", "assistant"]
    assert contents == ["run scan", "working"]


def test_list_latest_transcript_page_uses_canonical_turn_event_rows_when_present() -> None:
    """Canonical rows are authority for mixed tool/observation phase order."""
    conv_id = "conv-mixed-cycle-collision"
    assistant_message = _make_message(
        message_id=51,
        conversation_id=conv_id,
        turn_number=5,
        message_type="assistant",
        message="done",
        # Canonical target order should follow persisted phase_sequence:
        # tool-a(0), obs-a(1), tool-b(2), obs-b(3).
        # Existing mapper ignores phase_sequence and clusters by family on index collision.
        observation_tokens=(
            '[{"content":"obs-a","sub_turn_index":0,"phase_sequence":1},'
            '{"content":"obs-b","sub_turn_index":1,"phase_sequence":3}]'
        ),
        tool_calls=[
            _make_tool_call(
                tool_id=61,
                chat_message_id=51,
                tool_call_id="tc-a",
                tool_name="shell",
                tool_result="tool-a",
                turn_index=0,
            ),
            _make_tool_call(
                tool_id=62,
                chat_message_id=51,
                tool_call_id="tc-b",
                tool_name="shell",
                tool_result="tool-b",
                turn_index=0,
            ),
        ],
    )
    canonical_events = [
        _make_turn_event(
            chat_message_id=51,
            phase_sequence=0,
            kind="tool",
            content="tool-a",
            sub_turn_index=0,
            tool_call_id="tc-a",
            event_metadata={"tool_name": "shell"},
        ),
        _make_turn_event(
            chat_message_id=51,
            phase_sequence=1,
            kind="observation",
            content="obs-a",
            sub_turn_index=0,
            event_metadata={"source": "stream"},
        ),
        _make_turn_event(
            chat_message_id=51,
            phase_sequence=2,
            kind="tool",
            content="tool-b",
            sub_turn_index=1,
            tool_call_id="tc-b",
            event_metadata={"tool_name": "shell"},
        ),
        _make_turn_event(
            chat_message_id=51,
            phase_sequence=3,
            kind="observation",
            content="obs-b",
            sub_turn_index=1,
            event_metadata={"source": "stream"},
        ),
    ]
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([5]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result(canonical_events),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    assert [item.kind for item in page.items] == [
        "assistant",
        "tool",
        "observation",
        "tool",
        "observation",
    ]
    assert [item.content for item in page.items[1:]] == [
        "tool-a",
        "obs-a",
        "tool-b",
        "obs-b",
    ]
    assert [item.metadata["sequence"] for item in page.items[1:]] == [0, 1, 2, 3]


def test_list_latest_transcript_page_omits_turn_details_without_canonical_events() -> None:
    conv_id = "conv-index-base-normalization"
    assistant_message = _make_message(
        message_id=31,
        conversation_id=conv_id,
        turn_number=3,
        message_type="assistant",
        message="working",
        observation_tokens='[{"content":"obs-1","sub_turn_index":1},{"content":"obs-2","sub_turn_index":2}]',
        tool_calls=[
            _make_tool_call(
                tool_id=41,
                chat_message_id=31,
                tool_call_id="tc-1",
                tool_name="nmap",
                tool_result="tool-1",
                turn_index=0,
            ),
            _make_tool_call(
                tool_id=42,
                chat_message_id=31,
                tool_call_id="tc-2",
                tool_name="nmap",
                tool_result="tool-2",
                turn_index=1,
            ),
        ],
    )
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([3]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    assert [item.kind for item in page.items] == ["assistant"]
    assert [item.content for item in page.items] == ["working"]


def test_list_older_transcript_page_is_cursor_deterministic_by_turn_identity() -> None:
    conv_id = "conv-pagination"
    m1 = _make_message(message_id=1, conversation_id=conv_id, turn_number=1, message_type="user", message="user-1")
    m2 = _make_message(message_id=2, conversation_id=conv_id, turn_number=2, message_type="user", message="user-2")
    m3 = _make_message(message_id=3, conversation_id=conv_id, turn_number=3, message_type="user", message="user-3")
    m4 = _make_message(message_id=4, conversation_id=conv_id, turn_number=4, message_type="user", message="user-4")
    m5 = _make_message(message_id=5, conversation_id=conv_id, turn_number=5, message_type="user", message="user-5")
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([5, 4, 3]),
        _make_message_execute_result([m4, m5]),
        _make_scalar_execute_result([3, 2, 1]),
        _make_message_execute_result([m2, m3]),
        _make_scalar_execute_result([1]),
        _make_message_execute_result([m1]),
    ]

    service = ChatTranscriptQueryService(db)
    latest = service.list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=2,
    )
    assert latest.has_more_older is True
    assert latest.next_before is not None
    assert [item.content for item in latest.items] == ["user-4", "user-5"]

    older = service.list_older_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        before=latest.next_before,
        limit=2,
    )
    assert older.has_more_older is True
    assert older.next_before is not None
    assert [item.content for item in older.items] == ["user-2", "user-3"]

    oldest = service.list_older_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        before=older.next_before,
        limit=2,
    )
    assert oldest.has_more_older is False
    assert oldest.next_before is None
    assert [item.content for item in oldest.items] == ["user-1"]


def test_list_older_transcript_page_requires_positive_limit() -> None:
    service = ChatTranscriptQueryService(Mock())
    with pytest.raises(ValueError, match="limit must be > 0"):
        service.list_older_transcript_page(
            task_id=1,
            requested_conversation_id="conv-limit",
            before=TranscriptCursor(turn_number=1),
            limit=0,
        )


def _build_workflow_mock(
    *,
    reserved_message_id: int,
    state: str,
    graph_name: str = "simple_tool",
    turn_id: str | None = None,
    turn_sequence: int | None = None,
    checkpoint_id: str | None = None,
    interrupt_type: str | None = None,
    workflow_metadata: dict | None = None,
) -> Mock:
    workflow = Mock()
    workflow.reserved_message_id = reserved_message_id
    workflow.state = state
    workflow.graph_name = graph_name
    workflow.turn_id = turn_id
    workflow.turn_sequence = turn_sequence
    workflow.checkpoint_id = checkpoint_id
    workflow.interrupt_type = interrupt_type
    workflow.workflow_metadata = workflow_metadata or {}
    return workflow


def test_list_latest_transcript_page_failed_retryable_workflow_shows_retry_cta_when_budget_remaining() -> None:
    conv_id = "conv-retryable"
    assistant_message = _make_message(
        message_id=71,
        conversation_id=conv_id,
        turn_number=7,
        message_type="assistant",
        message="[Error] Retry me",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=71,
        state="FAILED",
        graph_name="simple_tool",
        turn_id="task-7-turn-7",
        turn_sequence=7,
        # Checkpoint retry requires a stable checkpoint_id; without one
        # the projection now fails safe and disables the CTA.
        checkpoint_id="ckpt-retryable-7",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "error": "provider_structured_output_parse",
            "error_message": "Retry from checkpoint",
            "retry_attempt_count": 0,
            "retry_max_attempts": 2,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([7]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=7,
        requested_conversation_id=conv_id,
        limit=10,
    )

    assert len(page.items) == 1
    metadata = page.items[0].metadata
    assert metadata["status"] == "error"
    assert metadata["retry_state"] == "failed"
    # Retry budget still has room (0 < 2), so retryable surfaces as True.
    assert metadata["retryable"] is True
    assert metadata["another_retry_allowed"] is True
    assert metadata["retry_exhausted"] is False
    assert metadata["retry_mode"] == "checkpoint"
    assert metadata["error_code"] == "provider_structured_output_parse"
    assert metadata["error_message"] == "Retry from checkpoint"
    assert metadata["graph_name"] == "simple_tool"
    assert metadata["turn_id"] == "task-7-turn-7"
    assert metadata["id"] == "task-7-turn-7"
    assert metadata["retry_attempt"] == 0
    assert metadata["retry_max_attempts"] == 2
    assert metadata["checkpoint_id"] == "ckpt-retryable-7"


def test_list_latest_transcript_page_projects_provider_refusal_before_failed_error() -> None:
    conv_id = "conv-refusal"
    assistant_message = _make_message(
        message_id=72,
        conversation_id=conv_id,
        turn_number=8,
        message_type="assistant",
        message="Partial answer",
    )
    refusal = {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "category": "cyber",
        "summary": "The provider declined this request under its cyber safety policy.",
        "explanation": "Blocked by policy.",
        "response_id": "msg_123",
        "partial": True,
    }
    workflow = _build_workflow_mock(
        reserved_message_id=72,
        state="FAILED",
        turn_id="task-8-turn-8",
        turn_sequence=8,
        workflow_metadata={
            "outcome_type": "provider_refusal",
            "retryable": False,
            "refusal": refusal,
        },
    )
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([8]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=8,
        requested_conversation_id=conv_id,
        limit=10,
    )

    metadata = page.items[0].metadata
    assert metadata["status"] == "declined"
    assert metadata["stop_reason"] == "refusal"
    assert metadata["outcome_type"] == "provider_refusal"
    assert metadata["retryable"] is False
    assert metadata["refusal"] == refusal
    assert "error_code" not in metadata


def test_list_latest_transcript_page_cancelled_retry_disables_retry_cta() -> None:
    conv_id = "conv-cancelled-retry"
    assistant_message = _make_message(
        message_id=77,
        task_id=12,
        conversation_id=conv_id,
        turn_number=12,
        message_type="assistant",
        message="[Stopped]",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=77,
        state="FAILED",
        graph_name="simple_tool",
        turn_id="task-12-turn-12",
        turn_sequence=12,
        checkpoint_id="ckpt-cancelled-12",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "error": "run_cancelled",
            "retry_state": "cancelled",
            "terminal_status": "cancelled",
            "cancel_requested": True,
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([12]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=12,
        requested_conversation_id=conv_id,
        limit=10,
    )

    assert page.items[0].content == "[Stopped]"
    metadata = page.items[0].metadata
    assert metadata["status"] == "cancelled"
    assert metadata["retry_state"] == "cancelled"
    assert metadata["retryable"] is False
    assert metadata["another_retry_allowed"] is False
    assert metadata["retry_exhausted"] is False
    assert metadata["active_retry"] is False
    assert metadata["error_code"] == "run_cancelled"
    assert metadata["retry_attempt"] == 1
    assert metadata["retry_max_attempts"] == 2
    assert metadata["checkpoint_id"] == "ckpt-cancelled-12"


def test_cancelled_transcript_projects_cancelled_tool_execution_rows() -> None:
    conv_id = "conv-cancelled-tool-projection"
    turn_id = "task-12-turn-15"
    assistant_message = _make_message(
        message_id=88,
        task_id=12,
        conversation_id=conv_id,
        turn_number=15,
        message_type="assistant",
        message="[Stopped]",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=88,
        state="FAILED",
        graph_name="simple_tool",
        turn_id=turn_id,
        turn_sequence=15,
        workflow_metadata={
            "error": "run_cancelled",
            "terminal_status": "cancelled",
            "cancel_requested": True,
        },
    )
    execution = _make_tool_execution(
        execution_id="exec-stop-1",
        task_id=12,
        conversation_id=conv_id,
        turn_id=turn_id,
        tool_call_id="tool-call-stop-1",
        tool_name="shell.exec",
        status="completed",
        execution_metadata={
            "cancellation": {
                "cancel_requested": True,
                "process_state": "orphaned_until_terminal",
                "runtime_kill_attempted": False,
                "runtime_kill_supported": False,
            }
        },
    )
    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([15]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
        _make_scalar_execute_result([execution]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=12,
        requested_conversation_id=conv_id,
        limit=10,
    )

    assert [item.kind for item in page.items] == ["assistant", "tool"]
    tool_item = page.items[1]
    assert tool_item.content == "Tool stopped"
    assert tool_item.metadata["tool_call_id"] == "tool-call-stop-1"
    assert tool_item.metadata["tool_name"] == "shell.exec"
    assert tool_item.metadata["status"] == "cancelled"
    assert tool_item.metadata["cancellation_source"] == "chat_stop"
    assert tool_item.metadata["process_state"] == "orphaned_until_terminal"


def test_list_latest_transcript_page_failed_retryable_workflow_hides_cta_when_checkpoint_id_missing() -> None:
    """Regression: FAILED + retryable=True must not advertise retry without a checkpoint_id.

    Checkpoint retry needs a stable ``checkpoint_id`` to resume from. A
    legacy/invalid retryable row without one cannot be retried — the
    worker has nothing to continue from. The projection must fail safe
    and disable the CTA.
    """
    conv_id = "conv-no-checkpoint"
    assistant_message = _make_message(
        message_id=76,
        conversation_id=conv_id,
        turn_number=11,
        message_type="assistant",
        message="[Error] Retry me",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=76,
        state="FAILED",
        graph_name="simple_tool",
        turn_id="task-11-turn-11",
        turn_sequence=11,
        # checkpoint_id intentionally missing — this is the legacy / invalid case.
        checkpoint_id=None,
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "error": "provider_structured_output_parse",
            "error_message": "Retry from checkpoint",
            "retry_attempt_count": 0,
            "retry_max_attempts": 2,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([11]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=11,
        requested_conversation_id=conv_id,
        limit=10,
    )

    metadata = page.items[0].metadata
    assert metadata["status"] == "error"
    # Even with retryable=True and budget remaining, the missing
    # checkpoint_id forces the CTA to be disabled.
    assert metadata["retryable"] is False
    assert metadata["another_retry_allowed"] is False


def test_list_latest_transcript_page_failed_retryable_workflow_hides_cta_when_exhausted() -> None:
    conv_id = "conv-exhausted"
    assistant_message = _make_message(
        message_id=72,
        conversation_id=conv_id,
        turn_number=7,
        message_type="assistant",
        message="[Error] Retry me",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=72,
        state="FAILED",
        turn_id="task-7-turn-7",
        turn_sequence=7,
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "error": "provider_structured_output_parse",
            "retry_attempt_count": 2,
            "retry_max_attempts": 2,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([7]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=7,
        requested_conversation_id=conv_id,
        limit=10,
    )

    metadata = page.items[0].metadata
    # Exhausted: budget consumed (2 == 2). CTA must not appear.
    assert metadata["status"] == "error"
    assert metadata["retryable"] is False
    assert metadata["another_retry_allowed"] is False
    assert metadata["retry_exhausted"] is True
    assert metadata["retry_attempt"] == 2
    assert metadata["retry_max_attempts"] == 2


def test_list_latest_transcript_page_retrying_workflow_emits_retrying_status_and_disables_cta() -> None:
    conv_id = "conv-retrying"
    assistant_message = _make_message(
        message_id=73,
        conversation_id=conv_id,
        turn_number=8,
        message_type="assistant",
        message="[Error] Will retry",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=73,
        state="RETRYING",
        graph_name="simple_tool",
        turn_id="task-8-turn-8",
        turn_sequence=8,
        checkpoint_id="ckpt-abc",
        workflow_metadata={
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )
    canonical_event = _make_turn_event(
        chat_message_id=73,
        phase_sequence=0,
        kind="tool",
        content="stale tool result",
        sub_turn_index=0,
        tool_call_id="tc-stale",
        event_metadata={"tool_name": "shell"},
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([8]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([canonical_event]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=8,
        requested_conversation_id=conv_id,
        limit=10,
    )

    metadata = page.items[0].metadata
    assert metadata["status"] == "retrying"
    assert metadata["retry_state"] == "retrying"
    assert metadata["retryable"] is False
    assert metadata["another_retry_allowed"] is False
    assert metadata["active_retry"] is True
    assert metadata["retry_attempt"] == 1
    assert metadata["retry_max_attempts"] == 2
    assert metadata["retry_mode"] == "checkpoint"
    assert metadata["checkpoint_id"] == "ckpt-abc"
    # Detail rows from the previous attempt must not bleed into the active
    # transcript view while a retry is in flight.
    kinds = [item.kind for item in page.items]
    assert kinds == ["assistant"]


def test_list_latest_transcript_page_completed_workflow_does_not_inherit_retryable_overlay() -> None:
    conv_id = "conv-completed"
    assistant_message = _make_message(
        message_id=74,
        conversation_id=conv_id,
        turn_number=9,
        message_type="assistant",
        message="all done",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=74,
        state="COMPLETED",
        turn_id="task-9-turn-9",
        turn_sequence=9,
        workflow_metadata={
            # Simulate a row that was retried successfully — `retryable=True`
            # is residual from the prior FAILED attempt and must NOT bleed
            # into the completed transcript.
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([9]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=9,
        requested_conversation_id=conv_id,
        limit=10,
    )

    metadata = page.items[0].metadata
    assert metadata["status"] == "completed"
    assert metadata["retry_state"] == "completed"
    assert metadata["retryable"] is False
    assert metadata["another_retry_allowed"] is False
    assert metadata["active_retry"] is False
    assert metadata["retry_attempt"] == 1


def test_list_latest_transcript_page_waiting_for_human_keeps_retry_disabled() -> None:
    conv_id = "conv-waiting"
    assistant_message = _make_message(
        message_id=75,
        conversation_id=conv_id,
        turn_number=10,
        message_type="assistant",
        message="awaiting input",
    )
    workflow = _build_workflow_mock(
        reserved_message_id=75,
        state="WAITING_FOR_HUMAN",
        turn_id="task-10-turn-10",
        turn_sequence=10,
        interrupt_type="user_input",
        workflow_metadata={
            "retry_mode": "checkpoint",
            "retry_attempt_count": 0,
            "retry_max_attempts": 2,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([10]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),
        _make_scalar_execute_result([workflow]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=10,
        requested_conversation_id=conv_id,
        limit=10,
    )

    metadata = page.items[0].metadata
    assert metadata["status"] == "waiting_for_human"
    assert metadata["retry_state"] == "waiting_for_human"
    assert metadata["retryable"] is False
    assert metadata["another_retry_allowed"] is False
    assert metadata["interrupt_type"] == "user_input"


def test_resolve_existing_conversation_id_prefers_requested_value() -> None:
    db = Mock()
    service = ChatTranscriptQueryService(db)

    resolved = service.resolve_existing_conversation_id(
        task_id=8,
        requested_conversation_id="conv-requested",
    )

    assert resolved == "conv-requested"
    db.execute.assert_not_called()


def test_resolve_existing_conversation_id_reads_latest_from_db_when_missing_request() -> None:
    db = Mock()
    db.execute.return_value = _make_first_execute_result(("conv-latest",))
    service = ChatTranscriptQueryService(db)

    resolved = service.resolve_existing_conversation_id(
        task_id=9,
        requested_conversation_id=None,
    )

    assert resolved == "conv-latest"


def test_list_latest_transcript_page_returns_empty_when_no_conversation_exists() -> None:
    db = Mock()
    db.execute.return_value = _make_first_execute_result(None)
    service = ChatTranscriptQueryService(db)

    page = service.list_latest_transcript_page(
        task_id=11,
        requested_conversation_id=None,
        limit=25,
    )

    assert page.conversation_id == ""
    assert page.items == []
    assert page.has_more_older is False
    assert page.next_before is None


# --- Canonical reasoning row preference tests ---


def test_transcript_prefers_canonical_reasoning_rows_over_blob() -> None:
    """When canonical reasoning rows exist, the legacy blob is not emitted."""
    conv_id = "conv-canonical-reasoning"
    assistant_message = _make_message(
        message_id=80,
        conversation_id=conv_id,
        turn_number=8,
        message_type="assistant",
        message="answer text",
        reasoning_tokens="legacy blob content",
    )
    reasoning_event = _make_turn_event(
        chat_message_id=80,
        phase_sequence=0,
        kind="reasoning",
        content="canonical section 1",
        sub_turn_index=0,
        event_metadata={"section_name": "intent"},
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([8]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([reasoning_event]),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    kinds = [item.kind for item in page.items]
    reasoning_items = [item for item in page.items if item.kind == "reasoning"]
    assert kinds == ["assistant", "reasoning"]
    assert len(reasoning_items) == 1
    # Canonical row content wins; blob is not emitted
    assert reasoning_items[0].content == "canonical section 1"


def test_transcript_falls_back_to_reasoning_blob_when_no_canonical_rows() -> None:
    """Old messages without canonical reasoning rows still use the blob."""
    conv_id = "conv-legacy-reasoning"
    assistant_message = _make_message(
        message_id=81,
        conversation_id=conv_id,
        turn_number=9,
        message_type="assistant",
        message="old answer",
        reasoning_tokens="legacy reasoning text",
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([9]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([]),  # no canonical events
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    reasoning_items = [item for item in page.items if item.kind == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0].content == "legacy reasoning text"
    assert reasoning_items[0].metadata["sequence_authority"] == "legacy_reasoning_blob"
    assert reasoning_items[0].metadata["phase_sequence"] == 0
    assert reasoning_items[0].metadata["reasoning_section_id"] == "msg-81-reasoning-0"


def test_transcript_canonical_reasoning_uses_ind_zero() -> None:
    """Canonical reasoning items use ind=0 (reasoning phase index)."""
    conv_id = "conv-reasoning-ind"
    assistant_message = _make_message(
        message_id=82,
        conversation_id=conv_id,
        turn_number=10,
        message_type="assistant",
        message="answer",
    )
    reasoning_event = _make_turn_event(
        chat_message_id=82,
        phase_sequence=0,
        kind="reasoning",
        content="thinking",
        sub_turn_index=0,
        event_metadata={
            "section_name": "planner",
            "started_at": 100.0,
            "ended_at": 108.4,
        },
    )

    db = Mock()
    db.execute.side_effect = [
        _make_scalar_execute_result([10]),
        _make_message_execute_result([assistant_message]),
        _make_scalar_execute_result([reasoning_event]),
        _make_scalar_execute_result([]),
    ]

    page = ChatTranscriptQueryService(db).list_latest_transcript_page(
        task_id=1,
        requested_conversation_id=conv_id,
        limit=10,
    )

    reasoning_items = [item for item in page.items if item.kind == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0].metadata["ind"] == 0
    assert reasoning_items[0].metadata["sequence_authority"] == "canonical_detail"
    assert reasoning_items[0].metadata["phase_sequence"] == 0
    assert reasoning_items[0].metadata["reasoning_section_id"] == "msg-82-reasoning-0"
    assert reasoning_items[0].metadata["started_at"] == 100.0
    assert reasoning_items[0].metadata["ended_at"] == 108.4
