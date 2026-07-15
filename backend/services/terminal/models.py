"""Terminal session data model.

Responsibilities:
- Represent a live PTY-backed terminal session.
- Own session-local read/write helpers, listener state, and replay metadata.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, Optional, Set

from ...core.time_utils import utc_now

@dataclass
class TerminalSession:
    """Represents an active terminal session."""

    session_id: str
    task_id: int
    user_id: int
    container_name: str
    connection_type: str  # 'docker_exec' or 'ssh'
    process: Optional[subprocess.Popen] = None
    exec_id: Optional[str] = None
    runtime_job_id: Optional[str] = None
    runtime_call_scope: str = "product_task"
    stream_mode: bool = False
    output_cursor: int = -1
    socket: Optional[Any] = None
    created_at: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    is_active: bool = True
    listeners: Set[Any] = None
    output_buffer: Deque[bytes] = None
    buffer_bytes: int = 0
    max_buffer_bytes: int = 256 * 1024
    reader_task: Optional[asyncio.Task] = None
    session_type: str = "user"
    command_history: list = None
    last_command_timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = utc_now()
        if self.last_activity is None:
            self.last_activity = utc_now()
        if self.listeners is None:
            self.listeners = set()
        if self.output_buffer is None:
            self.output_buffer = deque()
        if self.command_history is None:
            self.command_history = []

    def update_activity(self) -> None:
        """Update the last activity timestamp."""
        self.last_activity = utc_now()

    async def write(self, data: bytes) -> None:
        """Reject direct PTY writes; callers must use TerminalSessionManager."""
        _ = data
        raise RuntimeError("TerminalSession is passive; use TerminalSessionManager.send_input().")

    async def read(self, size: int = 4096) -> bytes:
        """Reject direct PTY reads; callers must use TerminalSessionManager."""
        _ = size
        raise RuntimeError("TerminalSession is passive; use TerminalSessionManager.read_output().")

    def to_dict(self) -> Dict[str, Any]:
        """Convert the session to a JSON-safe dictionary."""
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "user_id": self.user_id,
            "container_name": self.container_name,
            "connection_type": self.connection_type,
            "runtime_job_id": self.runtime_job_id,
            "runtime_call_scope": self.runtime_call_scope,
            "stream_mode": self.stream_mode,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "is_active": self.is_active,
        }
