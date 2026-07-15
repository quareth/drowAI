"""In-memory terminal session registry.

Responsibilities:
- Own the task-local session map for terminal sessions.
- Own timeout policy and background stale-session cleanup lifecycle.
- Provide lookup helpers for user/task-scoped active sessions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta

from ...core.time_utils import utc_now
from .models import TerminalSession

logger = logging.getLogger(__name__)


class TerminalSessionRegistry:
    """Store live terminal sessions and clean up stale entries."""

    def __init__(
        self,
        *,
        session_timeout: int = 3600,
        agent_session_timeout: int = 7200,
        cleanup_interval: int = 300,
    ) -> None:
        self.sessions: dict[str, TerminalSession] = {}
        self.session_timeout = session_timeout
        self.agent_session_timeout = agent_session_timeout
        self.cleanup_interval = cleanup_interval
        self.cleanup_task: asyncio.Task | None = None

    def start_cleanup_loop(
        self,
        close_session_callback: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Start the background stale-session cleanup loop."""
        if self.cleanup_task is None or self.cleanup_task.done():
            self.cleanup_task = asyncio.create_task(
                self._cleanup_sessions_loop(close_session_callback)
            )

    async def stop_cleanup_loop(self) -> None:
        """Cancel the background stale-session cleanup loop."""
        task = self.cleanup_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self.cleanup_task = None

    async def _cleanup_sessions_loop(
        self,
        close_session_callback: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Sleep, scan for stale sessions, and close them via the manager callback."""
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval)
                for session_id in self.iter_stale_session_ids():
                    session = self.sessions.get(session_id)
                    if session is None:
                        continue
                    logger.info(
                        "Cleaning up stale %s session: %s",
                        session.session_type,
                        session_id,
                    )
                    try:
                        await close_session_callback(session_id)
                    except Exception as exc:
                        logger.error("Error cleaning stale terminal session %s: %s", session_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error in terminal session cleanup loop: %s", exc)

    def get(self, session_id: str) -> TerminalSession | None:
        """Return a session by id."""
        return self.sessions.get(session_id)

    def set(self, session: TerminalSession) -> None:
        """Store or replace a session."""
        self.sessions[session.session_id] = session

    def remove(self, session_id: str) -> TerminalSession | None:
        """Remove and return a session if it exists."""
        return self.sessions.pop(session_id, None)

    def get_user_sessions(self, user_id: int) -> list[TerminalSession]:
        """Return active sessions owned by the user."""
        return [
            session
            for session in self.sessions.values()
            if session.user_id == user_id and session.is_active
        ]

    def get_task_sessions(self, task_id: int) -> list[TerminalSession]:
        """Return active sessions for the task."""
        return [
            session
            for session in self.sessions.values()
            if session.task_id == task_id and session.is_active
        ]

    def iter_stale_session_ids(self) -> Iterable[str]:
        """Yield ids of sessions whose activity exceeds their timeout."""
        current_time = utc_now()
        for session_id, session in list(self.sessions.items()):
            timeout = (
                self.agent_session_timeout
                if session.session_type == "agent"
                else self.session_timeout
            )
            if (
                session.last_activity is not None
                and current_time - session.last_activity > timedelta(seconds=timeout)
            ):
                yield session_id
