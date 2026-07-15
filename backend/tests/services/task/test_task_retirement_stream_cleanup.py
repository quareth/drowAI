"""Tests for task retirement stream cleanup hooks.

Scope:
- Ensure task retirement cleanup closes terminal sessions for the retired task.
- Ensure runner terminal frame buffers are cleared for the retired task only.
"""

from __future__ import annotations

import pytest

from backend.services.runner_control.terminal_frame_buffer import get_runner_terminal_frame_buffer
from backend.services.task.retirement_service import TaskRetirementService
from backend.services.terminal.manager import terminal_session_manager
from backend.services.terminal.models import TerminalSession


class _FakeHub:
    def __init__(self) -> None:
        self.removed_task_ids: list[int] = []

    async def remove_task(self, task_id: int) -> None:
        self.removed_task_ids.append(task_id)


@pytest.mark.asyncio
async def test_cleanup_runtime_stream_state_closes_task_sessions_and_clears_task_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_buffer = get_runner_terminal_frame_buffer()
    frame_buffer.reset()

    frame_buffer.append_frame(
        tenant_id=10,
        task_id=41,
        runtime_job_id="runner-job-41",
        session_id="runner-session-41",
        sequence=1,
        stream="stdout",
        data="task-41-output",
    )
    frame_buffer.append_frame(
        tenant_id=10,
        task_id=42,
        runtime_job_id="runner-job-42",
        session_id="runner-session-42",
        sequence=1,
        stream="stdout",
        data="task-42-output",
    )

    terminal_session_manager.sessions["session-task-41"] = TerminalSession(
        session_id="session-task-41",
        task_id=41,
        user_id=1,
        container_name="task-41",
        connection_type="docker_exec",
    )
    terminal_session_manager.sessions["session-task-42"] = TerminalSession(
        session_id="session-task-42",
        task_id=42,
        user_id=1,
        container_name="task-42",
        connection_type="docker_exec",
    )

    fake_hub = _FakeHub()
    monkeypatch.setattr(
        "backend.services.task.retirement_service.get_in_memory_stream_hub",
        lambda: fake_hub,
    )

    await TaskRetirementService.cleanup_runtime_stream_state(task_id=41)

    assert terminal_session_manager.get_session("session-task-41") is None
    assert terminal_session_manager.get_session("session-task-42") is not None
    retired_task_frames = frame_buffer.read_frames(
        tenant_id=10,
        task_id=41,
        runtime_job_id="runner-job-41",
        session_id="runner-session-41",
        after_sequence=-1,
    )
    active_task_frames = frame_buffer.read_frames(
        tenant_id=10,
        task_id=42,
        runtime_job_id="runner-job-42",
        session_id="runner-session-42",
        after_sequence=-1,
    )
    assert retired_task_frames["frames"] == []
    assert active_task_frames["frames"] != []
    assert fake_hub.removed_task_ids == [41]

    await terminal_session_manager.close_session("session-task-42")
    frame_buffer.reset()
