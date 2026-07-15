"""Agent reasoning persistence service:
- Append reasoning steps to DB with per-task monotonic sequence
- Provide helpers for replay queries

This module is a thin service to centralize DB writes/reads for reasoning,
enabling dual-write from file tail in and a later switch to DB-stream."""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.streaming import SystemLog

logger = logging.getLogger("backend.services.agent_reasoning_store")


class AgentReasoningTaskMissingError(ValueError):
    """Raised when reasoning persistence cannot resolve authoritative task ownership."""


class AgentReasoningStore:
    """Appends reasoning steps to SystemLog (Phase 1)."""

    def __init__(self, db: Session):
        self.db = db

    def append_step(self, task_id: int, step: Dict[str, Any]) -> Optional[SystemLog]:
        """Append a reasoning step with a per-task monotonic sequence to SystemLog.

        Sequence strategy (simple, transactional):
        - get current max sequence for task in system_logs
        - assign next = (max or 0) + 1
        - insert row; rely on unique(task_id, sequence) to guard; retry on conflict
        """
        retries = 3
        for attempt in range(retries):
            try:
                max_seq = self.db.execute(
                    select(func.max(SystemLog.sequence)).where(SystemLog.task_id == task_id)
                ).scalar()
                next_seq = int(max_seq or 0) + 1
                tenant_id = self._resolve_task_tenant_id(task_id)

                row = SystemLog(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    sequence=next_seq,
                    type=str(step.get("type", "reasoning"))[:50],
                    content=step.get("content", ""),
                    log_metadata=step.get("metadata", {}),
                )
                self.db.add(row)
                self.db.commit()
                self.db.refresh(row)
                return row
            except Exception as e:
                if isinstance(e, AgentReasoningTaskMissingError):
                    self.db.rollback()
                    raise
                logger.warning("AgentReasoningStore.append_step conflict/err (attempt %s): %s", attempt + 1, e)
                self.db.rollback()
        return None

    def list_after(self, task_id: int, after: int, limit: int = 200) -> List[SystemLog]:
        """List system log events after a given sequence number.

        Args:
            task_id: The task ID
            after: Return events with sequence > after
            limit: Maximum number of events to return

        Returns:
            List of SystemLog instances ordered by sequence ascending
        """
        stmt = (
            select(SystemLog)
            .where(SystemLog.task_id == task_id, SystemLog.sequence > after)
            .order_by(SystemLog.sequence.asc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def list_before(self, task_id: int, before: int, limit: int = 200) -> List[SystemLog]:
        """List system log events before a given sequence number.

        Args:
            task_id: The task ID
            before: Return events with sequence < before
            limit: Maximum number of events to return

        Returns:
            List of SystemLog instances ordered by sequence ascending
        """
        stmt = (
            select(SystemLog)
            .where(SystemLog.task_id == task_id, SystemLog.sequence < before)
            .order_by(SystemLog.sequence.desc())
            .limit(limit)
        )
        rows = list(self.db.execute(stmt).scalars().all())
        return list(reversed(rows))

    def get_latest_sequence(self, task_id: int) -> int:
        """Get the latest sequence number for a task (system_logs).

        Args:
            task_id: The task ID

        Returns:
            The latest sequence number, or 0 if no events exist
        """
        result = self.db.execute(
            select(func.max(SystemLog.sequence)).where(SystemLog.task_id == task_id)
        ).scalar()
        return int(result or 0)

    def poll_new_events(self, task_id: int, after: int, limit: int = 100) -> List[SystemLog]:
        """Poll for new events after a given sequence number.

        This method is optimized for polling operations and uses the same logic
        as list_after but with a smaller default limit for efficiency.

        Args:
            task_id: The task ID
            after: Return events with sequence > after
            limit: Maximum number of events to return (default 100 for polling)

        Returns:
            List of SystemLog instances ordered by sequence ascending
        """
        return self.list_after(task_id, after, limit)

    def batch_replay(self, task_id: int, after: int, before: int, limit: int = 500) -> List[SystemLog]:
        """Replay system log events in a batch for efficient pagination.

        Args:
            task_id: The task ID
            after: Return events with sequence > after
            before: Return events with sequence < before (optional, use None to ignore)
            limit: Maximum number of events to return

        Returns:
            List of SystemLog instances ordered by sequence ascending
        """
        if before is not None:
            stmt = (
                select(SystemLog)
                .where(
                    SystemLog.task_id == task_id,
                    SystemLog.sequence > after,
                    SystemLog.sequence < before
                )
                .order_by(SystemLog.sequence.asc())
                .limit(limit)
            )
        else:
            stmt = (
                select(SystemLog)
                .where(SystemLog.task_id == task_id, SystemLog.sequence > after)
                .order_by(SystemLog.sequence.asc())
                .limit(limit)
            )
        return list(self.db.execute(stmt).scalars().all())

    def get_event_count(self, task_id: int) -> int:
        """Get the total number of system log events for a task."""
        result = self.db.execute(
            select(func.count(SystemLog.id)).where(SystemLog.task_id == task_id)
        ).scalar()
        return int(result or 0)

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise AgentReasoningTaskMissingError(
                f"Cannot resolve tenant for reasoning write without task ownership: task_id={task_id}"
            )
        return int(tenant_id)

    def get_events_in_range(self, task_id: int, start_sequence: int, end_sequence: int) -> List[SystemLog]:
        """Get system log events within a specific sequence range."""
        stmt = (
            select(SystemLog)
            .where(
                SystemLog.task_id == task_id,
                SystemLog.sequence >= start_sequence,
                SystemLog.sequence <= end_sequence
            )
            .order_by(SystemLog.sequence.asc())
        )
        return list(self.db.execute(stmt).scalars().all())
