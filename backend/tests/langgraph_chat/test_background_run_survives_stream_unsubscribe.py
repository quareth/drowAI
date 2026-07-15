"""Regression tests for detached/background run lifecycle behavior."""

from __future__ import annotations

import pytest

from backend.services.langgraph_chat.execution.completion_callback import (
    StreamEmitter,
    run_turn_with_completion_callback,
)


@pytest.mark.asyncio
async def test_stream_unsubscribe_does_not_cancel_run() -> None:
    """Dropping stream subscribers must not cancel generation by itself."""
    connected = True

    async def mock_llm(emitter: StreamEmitter):
        await emitter.emit({"type": "message_start", "content": ""})
        await emitter.emit({"type": "message_delta", "content": "A"})
        await emitter.emit({"type": "message_delta", "content": "B"})
        return "AB"

    events = []
    async for event in run_turn_with_completion_callback(
        turn_id="task-1-turn-1",
        turn_number=1,
        task_id=1,
        conversation_id="conv-1",
        llm_func=mock_llm,
        is_connected=lambda: connected,
    ):
        events.append(event)
        if len(events) == 1:
            connected = False

    assert [event["type"] for event in events] == [
        "message_start",
        "message_delta",
        "message_delta",
    ]
