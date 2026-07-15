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
