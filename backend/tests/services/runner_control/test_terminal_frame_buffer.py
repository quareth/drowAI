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


def test_read_frames_reports_when_bounded_history_dropped_unread_frames() -> None:
    buffer = RunnerTerminalFrameBuffer(max_frames_per_session=2)
    for sequence in range(3):
        assert buffer.append_frame(
            tenant_id=7,
            task_id=11,
            runtime_job_id="job-open",
            session_id="runner-session-1",
            sequence=sequence,
            stream="stdout",
            data=str(sequence),
        )

    stale_reader = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        after_sequence=-1,
    )
    current_reader = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-open",
        session_id="runner-session-1",
        after_sequence=0,
    )

    assert stale_reader["data"] == "12"
    assert stale_reader["truncated"] is True
    assert current_reader["truncated"] is False


def test_bound_route_read_returns_delayed_frame_after_prior_cursor() -> None:
    buffer = RunnerTerminalFrameBuffer()
    assert buffer.bind_terminal_session(
        tenant_id=7,
        task_id=11,
        runtime_job_id="task-runtime-job",
        session_id="runner-session-1",
    )
    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="task-runtime-job",
        session_id="runner-session-1",
        sequence=0,
        stream="stdout",
        data="baseline\n",
    )
    baseline = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="task-runtime-job",
        session_id="runner-session-1",
        after_sequence=-1,
    )
    assert baseline["next_sequence"] == 0

    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="task-runtime-job",
        session_id="runner-session-1",
        sequence=1,
        stream="stdout",
        data="delayed frame from runner\n",
    )
    delayed = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="task-runtime-job",
        session_id="runner-session-1",
        after_sequence=int(baseline["next_sequence"]),
    )

    assert "delayed frame from runner" in delayed["data"]
    assert delayed["next_sequence"] == 1


def test_frame_buffer_exposes_exit_only_after_prior_data_sequence() -> None:
    buffer = RunnerTerminalFrameBuffer()
    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-shell",
        session_id="runner-shell",
        sequence=0,
        stream="stdout",
        data="tail\n",
    )
    assert buffer.append_frame(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-shell",
        session_id="runner-shell",
        sequence=1,
        stream="stdout",
        data="",
        eof=True,
        process_status="completed",
        exit_code=0,
    )

    data = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-shell",
        session_id="runner-shell",
        after_sequence=-1,
        max_bytes=5,
        max_frames=1,
    )
    terminal = buffer.read_frames(
        tenant_id=7,
        task_id=11,
        runtime_job_id="job-shell",
        session_id="runner-shell",
        after_sequence=int(data["next_sequence"]),
    )

    assert data["data"] == "tail\n"
    assert data["eof"] is False
    assert terminal["data"] == ""
    assert terminal["eof"] is True
    assert terminal["process_status"] == "completed"
    assert terminal["exit_code"] == 0
