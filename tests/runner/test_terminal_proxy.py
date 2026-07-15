"""Tests for runner terminal proxy lifecycle with a fake PTY adapter."""

from __future__ import annotations

from pathlib import Path

from drowai_runner.job_store import initialize_runner_job_store
from drowai_runner.terminal_proxy import (
    ERROR_TERMINAL_JOB_NOT_ACTIVE,
    ERROR_TERMINAL_SESSION_NOT_FOUND,
    RunnerTerminalProxy,
)


class _FakePtyAdapter:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str, int, int]] = []
        self.inputs: list[tuple[str, str]] = []
        self.reads: list[tuple[str, int]] = []
        self.resizes: list[tuple[str, int, int]] = []
        self.closed: list[str] = []

    def open_session(
        self,
        *,
        container_id: str,
        session_id: str,
        cols: int,
        rows: int,
    ) -> None:
        self.opened.append((container_id, session_id, cols, rows))

    def send_input(self, *, session_id: str, data: str) -> None:
        self.inputs.append((session_id, data))

    def read_output(self, *, session_id: str, max_bytes: int) -> bytes:
        self.reads.append((session_id, max_bytes))
        return f"{session_id}:ok".encode("utf-8")

    def resize_session(self, *, session_id: str, cols: int, rows: int) -> None:
        self.resizes.append((session_id, cols, rows))

    def close_session(self, *, session_id: str) -> None:
        self.closed.append(session_id)


def test_terminal_proxy_runs_lifecycle_with_fake_adapter(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "runner-jobs.sqlite")
    store.start_job(
        runtime_job_id="job-tty",
        tenant_id="tenant-a",
        task_id="22",
        workspace_id="task-22",
        image="runtime:test",
        container_id="cid-22",
    )
    adapter = _FakePtyAdapter()
    proxy = RunnerTerminalProxy(job_store=store, pty_adapter=adapter)

    opened = proxy.open_terminal_session(runtime_job_id="job-tty", session_name="shell")
    assert opened.accepted is True
    session_id = str((opened.metadata or {})["session_id"])

    sent = proxy.send_terminal_input(session_id=session_id, data="ls -la\n")
    read = proxy.read_terminal_output(session_id=session_id, max_bytes=1024)
    resized = proxy.resize_terminal_session(session_id=session_id, cols=140, rows=35)
    closed = proxy.close_terminal_session(session_id=session_id)
    read_after_close = proxy.read_terminal_output(session_id=session_id)

    assert sent.accepted is True
    assert read.accepted is True
    assert read.metadata is not None
    assert read.metadata["output"] == f"{session_id}:ok"
    assert resized.accepted is True
    assert closed.accepted is True
    assert read_after_close.accepted is False
    assert read_after_close.error_code == ERROR_TERMINAL_SESSION_NOT_FOUND

    assert adapter.opened[0][0] == "cid-22"
    assert adapter.inputs == [(session_id, "ls -la\n")]
    assert adapter.reads == [(session_id, 1024)]
    assert adapter.resizes == [(session_id, 140, 35)]
    assert adapter.closed == [session_id]


def test_terminal_proxy_rejects_inactive_job_and_unknown_session(tmp_path: Path) -> None:
    store = initialize_runner_job_store(tmp_path / "runner-jobs.sqlite")
    store.start_job(
        runtime_job_id="job-stop",
        tenant_id="tenant-a",
        task_id="44",
        workspace_id="task-44",
        image="runtime:test",
        container_id="cid-44",
    )
    store.mark_stopped("job-stop")
    proxy = RunnerTerminalProxy(job_store=store, pty_adapter=_FakePtyAdapter())

    open_stopped = proxy.open_terminal_session(runtime_job_id="job-stop")
    read_unknown = proxy.read_terminal_output(session_id="missing-session")

    assert open_stopped.accepted is False
    assert open_stopped.error_code == ERROR_TERMINAL_JOB_NOT_ACTIVE
    assert read_unknown.accepted is False
    assert read_unknown.error_code == ERROR_TERMINAL_SESSION_NOT_FOUND
