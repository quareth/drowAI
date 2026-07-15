"""Terminal session + frame-publisher DTOs. Data only.

Holds the runner-side terminal session identity and the background
frame-publisher handle. No logic, no I/O, no protocol behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True, slots=True)
class _ActiveTerminalSession:
    """Runner terminal session identity used for background frame publication."""

    runtime_job_id: str
    task_id: int


@dataclass(slots=True)
class _TerminalFramePublisher:
    """Background publisher for one active cloud terminal stream."""

    stop_event: threading.Event
    thread: threading.Thread
