"""Coordinate process-local parent-handoff graph approval continuations.

The broker keeps the original parent finalizer and coordinator suspended while
the task resume worker continues the same checkpointed graph. It owns only the
in-process rendezvous and shared turn state container; graph execution,
checkpoint persistence, handoff claims, and parent routing remain with their
existing services.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable

from backend.services.langgraph_chat.checkpoint.thread_identity import (
    format_graph_thread_id,
)
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer


CancellationChecker = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ParentHandoffContinuationSession:
    """One pending parent graph continuation owned by an active parent turn."""

    task_id: int
    thread_id: str
    state_container: ChatStateContainer
    result_future: asyncio.Future[Any]


class ParentHandoffContinuationBroker:
    """Rendezvous parent graph resume workers with their original finalizer."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[int, str], ParentHandoffContinuationSession] = {}
        self._lock = Lock()

    def open(
        self,
        *,
        task_id: int,
        thread_id: str,
        state_container: ChatStateContainer,
    ) -> ParentHandoffContinuationSession:
        """Register the only pending parent continuation for a task thread."""
        key = self._key(task_id=task_id, thread_id=thread_id)
        session = ParentHandoffContinuationSession(
            task_id=int(task_id),
            thread_id=key[1],
            state_container=state_container,
            result_future=asyncio.get_running_loop().create_future(),
        )
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.result_future.done():
                raise RuntimeError(
                    "Parent handoff continuation is already pending for "
                    f"task {task_id}"
                )
            self._sessions[key] = session
        return session

    def require(
        self,
        *,
        task_id: int,
        graph_thread_id: str | None,
    ) -> ParentHandoffContinuationSession:
        """Return the live continuation session addressed by resume identity."""
        key = self._key(task_id=task_id, thread_id=graph_thread_id)
        with self._lock:
            session = self._sessions.get(key)
        if session is None or session.result_future.done():
            raise RuntimeError(
                "Parent handoff approval cannot resume because its process-local "
                "continuation is no longer active"
            )
        return session

    async def wait(
        self,
        session: ParentHandoffContinuationSession,
        *,
        should_cancel: CancellationChecker,
    ) -> Any:
        """Wait for a resume worker while continuing to observe parent cancellation."""
        while True:
            if should_cancel():
                raise asyncio.CancelledError
            try:
                return await asyncio.wait_for(
                    asyncio.shield(session.result_future),
                    timeout=0.25,
                )
            except asyncio.TimeoutError:
                continue

    def deliver(
        self,
        session: ParentHandoffContinuationSession,
        execution_result: Any,
    ) -> None:
        """Deliver one completed resume execution to the waiting parent finalizer."""
        if session.result_future.done():
            raise RuntimeError("Parent handoff continuation is no longer waiting")
        session.result_future.set_result(execution_result)

    def fail(
        self,
        *,
        task_id: int,
        graph_thread_id: str | None,
        error: BaseException,
    ) -> bool:
        """Wake the original parent with a resume failure, when it is still active."""
        try:
            session = self.require(
                task_id=task_id,
                graph_thread_id=graph_thread_id,
            )
        except RuntimeError:
            return False
        session.result_future.set_exception(error)
        return True

    def close(self, session: ParentHandoffContinuationSession) -> None:
        """Remove a completed/cancelled parent continuation session."""
        key = self._key(task_id=session.task_id, thread_id=session.thread_id)
        with self._lock:
            current = self._sessions.get(key)
            if current is session:
                self._sessions.pop(key, None)
        if not session.result_future.done():
            session.result_future.cancel()

    @staticmethod
    def _key(*, task_id: int, thread_id: object) -> tuple[int, str]:
        normalized = str(thread_id).strip().lower()
        if normalized.startswith("graph-"):
            normalized = normalized.removeprefix("graph-")
        if not normalized:
            raise ValueError("Parent handoff continuation requires thread identity")
        return int(task_id), format_graph_thread_id(normalized, task_id=task_id)


__all__ = [
    "ParentHandoffContinuationBroker",
    "ParentHandoffContinuationSession",
]
