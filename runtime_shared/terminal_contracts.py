"""Runtime-safe terminal session contracts and helpers.

This module defines deterministic terminal prompt markers, session id builders,
and lightweight DTOs that can be shared by backend adapters, runner code, and
runtime-image modules without importing backend-owned terminal services.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

AGENT_PROMPT_MARKER = "__DROWAI_PROMPT__> "
AGENT_PROMPT_ENV = "__DROWAI_PROMPT__>"

AGENT_SESSION_TYPE = "agent"


def build_agent_session_id(task_id: int) -> str:
    """Return the canonical agent PTY session id for a task."""
    return f"agent_task_{task_id}"


def build_named_agent_session_id(task_id: int, session_name: str) -> str:
    """Return the canonical named agent PTY session id for a task."""
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", session_name.lower())
    return f"{build_agent_session_id(task_id)}_{safe_name}"


@dataclass(frozen=True, slots=True)
class TerminalSessionIdentity:
    """Backend-free identity for one task-scoped terminal session."""

    task_id: int
    session_name: str
    session_id: str
    session_type: str = AGENT_SESSION_TYPE


@dataclass(frozen=True, slots=True)
class TerminalSessionSnapshot:
    """Serializable terminal session projection shared across adapters."""

    task_id: int
    session_id: str
    session_name: str
    runtime_job_id: str | None = None
    container_id: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalReadResult:
    """Typed result for provider terminal reads."""

    ok: bool
    data: bytes = b""
    error_code: str | None = None
    truncated: bool = False
    eof: bool = False
    process_status: str | None = None
    exit_code: int | None = None


@dataclass(slots=True)
class DedicatedExecDrainState:
    """Delay terminal publication briefly while a stopped exec drains its PTY tail."""

    stopped_at: float | None = None

    def observe(
        self,
        *,
        running: bool,
        socket_eof: bool,
        exit_code: int | None,
        now: float,
        drain_grace_seconds: float,
    ) -> TerminalReadResult:
        """Return running until EOF or a stopped exec's bounded drain window closes."""
        if running:
            self.stopped_at = None
            return TerminalReadResult(ok=True, process_status="running")
        if socket_eof:
            return self._terminal_result(exit_code)
        if self.stopped_at is None:
            self.stopped_at = now
            return TerminalReadResult(ok=True, process_status="running")
        if now - self.stopped_at < max(0.0, drain_grace_seconds):
            return TerminalReadResult(ok=True, process_status="running")
        return self._terminal_result(exit_code)

    @staticmethod
    def _terminal_result(exit_code: int | None) -> TerminalReadResult:
        return TerminalReadResult(
            ok=True,
            eof=True,
            process_status="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
        )
