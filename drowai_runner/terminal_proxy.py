"""Runner-local terminal proxy for task-scoped PTY interactions.

This module provides a backend-free terminal adapter that binds terminal
session lifecycle to runner job ownership and enforces fail-closed checks for
inactive or unassigned runtime jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from drowai_runner.job_store import ACTIVE_JOB_STATUSES, RunnerJobStore
from runtime_shared.terminal_contracts import build_named_agent_session_id

ERROR_TERMINAL_SESSION_NOT_FOUND = "RUNNER_TERMINAL_SESSION_NOT_FOUND"
ERROR_TERMINAL_JOB_NOT_FOUND = "RUNNER_TERMINAL_JOB_NOT_FOUND"
ERROR_TERMINAL_JOB_NOT_ACTIVE = "RUNNER_TERMINAL_JOB_NOT_ACTIVE"
ERROR_TERMINAL_CONTAINER_NOT_ASSIGNED = "RUNNER_TERMINAL_CONTAINER_NOT_ASSIGNED"


@dataclass(frozen=True, slots=True)
class TerminalProxyResponse:
    """Backend-free response envelope for terminal proxy operations."""

    accepted: bool
    status: str
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, object] | None = None


class PtyAdapter(Protocol):
    """PTY adapter protocol used by runner terminal proxy."""

    def open_session(
        self,
        *,
        container_id: str,
        session_id: str,
        cols: int,
        rows: int,
    ) -> None: ...

    def send_input(self, *, session_id: str, data: str) -> None: ...

    def read_output(self, *, session_id: str, max_bytes: int) -> bytes: ...

    def resize_session(self, *, session_id: str, cols: int, rows: int) -> None: ...

    def close_session(self, *, session_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TerminalSessionBinding:
    """In-memory binding between a terminal session and runner job identity."""

    runtime_job_id: str
    task_id: str
    container_id: str


class RunnerTerminalProxy:
    """Runner-local terminal lifecycle manager with task/job scope enforcement."""

    def __init__(self, *, job_store: RunnerJobStore, pty_adapter: PtyAdapter) -> None:
        self._job_store = job_store
        self._pty_adapter = pty_adapter
        self._sessions: dict[str, TerminalSessionBinding] = {}

    def open_terminal_session(
        self,
        *,
        runtime_job_id: str,
        session_name: str = "terminal",
        cols: int = 120,
        rows: int = 30,
    ) -> TerminalProxyResponse:
        """Create a terminal session bound to one active runner runtime job."""
        job_check = self._validate_active_job(runtime_job_id)
        if job_check is not None:
            return job_check
        job = self._job_store.get_job(runtime_job_id)
        session_id = build_named_agent_session_id(int(job.task_id), session_name)
        if session_id in self._sessions:
            suffix = len(self._sessions) + 1
            session_id = build_named_agent_session_id(int(job.task_id), f"{session_name}_{suffix}")
        assert job.container_id is not None
        self._pty_adapter.open_session(
            container_id=job.container_id,
            session_id=session_id,
            cols=max(cols, 20),
            rows=max(rows, 10),
        )
        self._sessions[session_id] = TerminalSessionBinding(
            runtime_job_id=runtime_job_id,
            task_id=job.task_id,
            container_id=job.container_id,
        )
        return TerminalProxyResponse(
            accepted=True,
            status="succeeded",
            metadata={
                "runtime_job_id": runtime_job_id,
                "task_id": job.task_id,
                "session_id": session_id,
            },
        )

    def send_terminal_input(self, *, session_id: str, data: str) -> TerminalProxyResponse:
        """Send input to one scoped terminal session if job is still active."""
        session_check = self._validate_session(session_id)
        if session_check is not None:
            return session_check
        binding = self._sessions[session_id]
        job_check = self._validate_active_job(binding.runtime_job_id)
        if job_check is not None:
            return job_check
        self._pty_adapter.send_input(session_id=session_id, data=data)
        return TerminalProxyResponse(accepted=True, status="succeeded")

    def read_terminal_output(
        self,
        *,
        session_id: str,
        max_bytes: int = 32768,
    ) -> TerminalProxyResponse:
        """Read terminal output for one scoped session if job is still active."""
        session_check = self._validate_session(session_id)
        if session_check is not None:
            return session_check
        binding = self._sessions[session_id]
        job_check = self._validate_active_job(binding.runtime_job_id)
        if job_check is not None:
            return job_check
        payload = self._pty_adapter.read_output(session_id=session_id, max_bytes=max(1, max_bytes))
        return TerminalProxyResponse(
            accepted=True,
            status="succeeded",
            metadata={"session_id": session_id, "output": payload.decode("utf-8", errors="replace")},
        )

    def resize_terminal_session(
        self,
        *,
        session_id: str,
        cols: int,
        rows: int,
    ) -> TerminalProxyResponse:
        """Resize one scoped terminal session."""
        session_check = self._validate_session(session_id)
        if session_check is not None:
            return session_check
        self._pty_adapter.resize_session(
            session_id=session_id,
            cols=max(cols, 20),
            rows=max(rows, 10),
        )
        return TerminalProxyResponse(accepted=True, status="succeeded")

    def close_terminal_session(self, *, session_id: str) -> TerminalProxyResponse:
        """Close one scoped terminal session."""
        session_check = self._validate_session(session_id)
        if session_check is not None:
            return session_check
        self._pty_adapter.close_session(session_id=session_id)
        self._sessions.pop(session_id, None)
        return TerminalProxyResponse(accepted=True, status="succeeded")

    def _validate_session(self, session_id: str) -> TerminalProxyResponse | None:
        if session_id in self._sessions:
            return None
        return TerminalProxyResponse(
            accepted=False,
            status="failed",
            error_code=ERROR_TERMINAL_SESSION_NOT_FOUND,
            error_message=f"Unknown terminal session: {session_id}",
        )

    def _validate_active_job(self, runtime_job_id: str) -> TerminalProxyResponse | None:
        job = self._job_store.find_job(runtime_job_id)
        if job is None:
            return TerminalProxyResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_TERMINAL_JOB_NOT_FOUND,
                error_message=f"Unknown runtime job: {runtime_job_id}",
            )
        if job.status not in ACTIVE_JOB_STATUSES:
            return TerminalProxyResponse(
                accepted=False,
                status="rejected",
                error_code=ERROR_TERMINAL_JOB_NOT_ACTIVE,
                error_message=f"Runtime job `{runtime_job_id}` is not active.",
                metadata={"runtime_job_id": runtime_job_id, "job_status": job.status},
            )
        if not job.container_id:
            return TerminalProxyResponse(
                accepted=False,
                status="failed",
                error_code=ERROR_TERMINAL_CONTAINER_NOT_ASSIGNED,
                error_message=f"Runtime job `{runtime_job_id}` has no assigned container.",
            )
        return None
