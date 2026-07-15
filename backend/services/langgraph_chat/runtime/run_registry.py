"""In-memory registry for task-scoped interactive run lifecycle metadata."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(slots=True)
class RunRecord:
    """Canonical in-process run record keyed by task and turn identity."""

    task_id: int
    turn_id: str
    conversation_id: Optional[str] = None
    state: str = "running"
    cancel_requested: bool = False
    cancel_reason: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None


class RunRegistry:
    """Thread-safe run registry for active and terminal lifecycle states."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_by_task: Dict[int, RunRecord] = {}

    def start(
        self,
        *,
        task_id: int,
        turn_id: str,
        conversation_id: Optional[str] = None,
    ) -> RunRecord:
        with self._lock:
            now = time.time()
            record = RunRecord(
                task_id=task_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
                state="running",
                started_at=now,
                updated_at=now,
            )
            self._active_by_task[task_id] = record
            return self._copy(record)

    def get_active(self, task_id: int) -> Optional[RunRecord]:
        with self._lock:
            record = self._active_by_task.get(task_id)
            if record is None:
                return None
            return self._copy(record)

    def request_cancel(
        self,
        *,
        task_id: int,
        turn_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        with self._lock:
            record = self._active_by_task.get(task_id)
            if record is None:
                return {
                    "cancelled": False,
                    "already_cancelled": False,
                    "active": False,
                    "turn_id": turn_id,
                }
            if turn_id and record.turn_id != turn_id:
                return {
                    "cancelled": False,
                    "already_cancelled": False,
                    "active": True,
                    "turn_id": record.turn_id,
                    "reason": "turn_id_mismatch",
                }
            if record.cancel_requested:
                return {
                    "cancelled": False,
                    "already_cancelled": True,
                    "active": True,
                    "turn_id": record.turn_id,
                }
            record.cancel_requested = True
            record.cancel_reason = (reason or "explicit_cancel").strip() or "explicit_cancel"
            record.updated_at = time.time()
            return {
                "cancelled": True,
                "already_cancelled": False,
                "active": True,
                "turn_id": record.turn_id,
                "cancel_reason": record.cancel_reason,
            }

    def is_cancel_requested(self, *, task_id: int, turn_id: Optional[str] = None) -> bool:
        with self._lock:
            record = self._active_by_task.get(task_id)
            if record is None:
                return False
            if turn_id and record.turn_id != turn_id:
                return False
            return bool(record.cancel_requested)

    def finish(self, *, task_id: int, turn_id: str, state: str) -> None:
        with self._lock:
            record = self._active_by_task.get(task_id)
            if record is None or record.turn_id != turn_id:
                return
            record.state = state
            record.updated_at = time.time()
            record.ended_at = record.updated_at
            self._active_by_task.pop(task_id, None)

    @staticmethod
    def _copy(record: RunRecord) -> RunRecord:
        return RunRecord(
            task_id=record.task_id,
            turn_id=record.turn_id,
            conversation_id=record.conversation_id,
            state=record.state,
            cancel_requested=record.cancel_requested,
            cancel_reason=record.cancel_reason,
            started_at=record.started_at,
            updated_at=record.updated_at,
            ended_at=record.ended_at,
        )


_shared_registry = RunRegistry()


def get_run_registry() -> RunRegistry:
    """Return the process-global run registry."""
    return _shared_registry


__all__ = ["RunRecord", "RunRegistry", "get_run_registry"]
