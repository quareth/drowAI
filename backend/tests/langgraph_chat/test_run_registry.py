"""Tests for task-scoped run registry lifecycle transitions."""

from __future__ import annotations

from backend.services.langgraph_chat.runtime.run_registry import RunRegistry


def test_run_registry_start_cancel_finish_flow() -> None:
    registry = RunRegistry()
    task_id = 42
    turn_id = "task-42-turn-7"

    started = registry.start(task_id=task_id, turn_id=turn_id, conversation_id="conv-42")
    assert started.state == "running"
    assert started.cancel_requested is False

    active = registry.get_active(task_id)
    assert active is not None
    assert active.turn_id == turn_id
    assert active.state == "running"

    cancelled = registry.request_cancel(task_id=task_id, turn_id=turn_id, reason="user_stop")
    assert cancelled["cancelled"] is True
    assert registry.is_cancel_requested(task_id=task_id, turn_id=turn_id) is True

    second_cancel = registry.request_cancel(task_id=task_id, turn_id=turn_id, reason="user_stop")
    assert second_cancel["cancelled"] is False
    assert second_cancel["already_cancelled"] is True

    registry.finish(task_id=task_id, turn_id=turn_id, state="cancelled")
    assert registry.get_active(task_id) is None


def test_run_registry_rejects_turn_mismatch_cancel() -> None:
    registry = RunRegistry()
    task_id = 11
    registry.start(task_id=task_id, turn_id="task-11-turn-1")

    response = registry.request_cancel(
        task_id=task_id,
        turn_id="task-11-turn-2",
        reason="wrong_turn",
    )
    assert response["cancelled"] is False
    assert response["reason"] == "turn_id_mismatch"
    assert registry.is_cancel_requested(task_id=task_id, turn_id="task-11-turn-1") is False
