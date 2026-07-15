"""Shared runner tool-command status and result envelopes.

This module keeps file-comm and PTY tool execution transports aligned on one
runner-local contract so provider polling does not depend on transport details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from runtime_shared.tool_command_transport import TRANSPORT_FILE_COMM, TRANSPORT_PTY


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMED_OUT})


@dataclass(frozen=True, slots=True)
class RunnerToolCommandResult:
    """Transport-neutral status/result for one runner tool command."""

    command_id: str
    status: str
    success: bool | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    artifacts: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    transport: str | None = None

    @property
    def terminal(self) -> bool:
        """Return whether this command has reached a terminal state."""
        return self.status in TERMINAL_STATUSES

    @classmethod
    def queued(cls, *, command_id: str, transport: str | None = None) -> "RunnerToolCommandResult":
        """Build a queued command status."""
        return cls(command_id=command_id, status=STATUS_QUEUED, transport=transport)

    @classmethod
    def running(cls, *, command_id: str, transport: str | None = None) -> "RunnerToolCommandResult":
        """Build a running command status."""
        return cls(command_id=command_id, status=STATUS_RUNNING, transport=transport)

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-safe payload used by runner operation responses."""
        payload: dict[str, Any] = {
            "command_id": self.command_id,
            "status": self.status,
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "artifacts": list(self.artifacts),
            "metadata": dict(self.metadata),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }
        if self.transport:
            payload["transport"] = self.transport
        return payload


__all__ = [
    "RunnerToolCommandResult",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_TIMED_OUT",
    "TERMINAL_STATUSES",
    "TRANSPORT_FILE_COMM",
    "TRANSPORT_PTY",
]
