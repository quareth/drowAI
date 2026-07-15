"""Regression tests for runner terminal frame buffering semantics.

Scope:
- Enforce monotonic frame sequence checks per tenant/task/session across runtime-job keys.
- Ensure close-style session cleanup removes all runtime-job buckets and resets session sequence state.
"""

from __future__ import annotations

from backend.services.runner_control.terminal_frame_buffer import RunnerTerminalFrameBuffer
from runtime_shared.runner_protocol import RUNNER_TERMINAL_FRAME_MAX_BYTES


def test_append_frame_rejects_stale_sequence_across_runtime_job_buckets() -> None:
    buffer = RunnerTerminalFrameBuffer()

    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        sequence=5,
        stream="stdout",
        data="open-frame",
    )
    assert not buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-input",
        session_id="runner-session-1",
        sequence=5,
        stream="stdout",
        data="replayed-frame",
    )
    assert not buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-input",
        session_id="runner-session-1",
        sequence=4,
        stream="stdout",
        data="stale-frame",
    )
    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-input",
        session_id="runner-session-1",
        sequence=6,
        stream="stdout",
        data="fresh-frame",
    )

    response = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-input",
        session_id="runner-session-1",
        after_sequence=-1,
    )

    assert [frame["sequence"] for frame in response["frames"]] == [6]
    assert response["data"] == "fresh-frame"


def test_clear_terminal_session_removes_all_runtime_job_buckets_and_resets_sequence() -> None:
    buffer = RunnerTerminalFrameBuffer()

    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        sequence=1,
        stream="stdout",
        data="open-output",
    )
    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-input",
        session_id="runner-session-1",
        sequence=2,
        stream="stdout",
        data="input-output",
    )

    buffer.clear_terminal_session(
        tenant_id=7,
        task_id=11,
        session_id="runner-session-1",
    )

    open_frames = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        after_sequence=-1,
    )
    input_frames = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-input",
        session_id="runner-session-1",
        after_sequence=-1,
    )

    assert open_frames["frames"] == []
    assert input_frames["frames"] == []

    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-reopen",
        session_id="runner-session-1",
        sequence=0,
        stream="stdout",
        data="new-session-output",
    )


def test_append_frame_rejects_oversized_payload() -> None:
    buffer = RunnerTerminalFrameBuffer()

    accepted = buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        sequence=1,
        stream="stdout",
        data="x" * (RUNNER_TERMINAL_FRAME_MAX_BYTES + 1),
    )

    assert accepted is False
    response = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        after_sequence=-1,
    )
    assert response["frames"] == []


def test_read_frames_respects_max_bytes_for_first_eligible_frame() -> None:
    buffer = RunnerTerminalFrameBuffer()
    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        sequence=1,
        stream="stdout",
        data="welcome",
    )

    response = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        after_sequence=-1,
        max_bytes=3,
    )

    assert response["frames"] == []
    assert response["data"] == ""
    assert response["next_sequence"] == -1
