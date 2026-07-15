"""Turn number assignment service for sequential turn tracking.

This module provides the TurnNumberService, which assigns sequential
turn numbers to turns within a task. Turn numbers are essential for
grouping events and maintaining conversation ordering.

Design Principles:
1. Sequential Assignment - Turn numbers start at 1 and increment
2. Thread Safety - Uses dedicated task_turn_counter table with atomic
   INSERT ... ON CONFLICT DO UPDATE so concurrent turns get unique numbers
3. Task Scoped - Turn numbers are unique within a task
4. Retry on constraint violation - Guarantees uniqueness under concurrency

Usage:
    from backend.services.chat.turn_number_service import get_turn_number_service

    service = get_turn_number_service()
    turn_number = service.get_next_turn_number(task_id=123, conversation_id="conv-1")
    # Returns 1 for first turn, 2 for second, etc.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger("backend.services.turn_number_service")

# Max retries on constraint violation (rare with atomic upsert)
MAX_RETRIES = 3


class TurnNumberService:
    """Assign sequential turn numbers per task.

    This service is the single source of truth for turn number assignment.
    Turn numbers are sequential integers starting at 1, scoped to a task.

    Thread Safety:
    - Uses dedicated task_turn_counter table with one row per task
    - Atomic INSERT ... ON CONFLICT DO UPDATE ... RETURNING next_turn
    - Retry on IntegrityError (constraint violation) to guarantee uniqueness

    Attributes:
        _db_factory: Callable that returns DB session generator
    """

    def __init__(self, db_session_factory=None) -> None:
        """Initialize turn number service.

        Args:
            db_session_factory: Optional callable that returns DB session.
                               If None, uses default from get_db.
        """
        self._db_factory = db_session_factory

    def _get_db_session(self):
        """Get database session from factory or default."""
        if self._db_factory:
            return self._db_factory()
        # Use default database session
        from backend.database import get_db
        return next(get_db())

    def get_next_turn_number(
        self,
        task_id: int,
        conversation_id: Optional[str] = None,
    ) -> int:
        """Get next turn number for task (serialized per task, unique under concurrency).

        Uses task_turn_counter table: atomic upsert so concurrent calls
        get distinct turn numbers. Retries on constraint violation.

        Args:
            task_id: Task ID to get next turn number for
            conversation_id: Optional conversation context (for logging only)

        Returns:
            Next sequential turn number (1 for first turn, 2 for second, etc.)

        Raises:
            Exception: If database query fails after retries
        """
        db = self._get_db_session()
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    next_turn = self._allocate_turn(db, task_id)
                    logger.info(
                        "[TURN_NUMBER] Assigned turn %s to task %s (conversation=%s)",
                        next_turn,
                        task_id,
                        conversation_id or "none",
                    )
                    return next_turn
                except IntegrityError as exc:
                    db.rollback()
                    if attempt >= MAX_RETRIES:
                        logger.error(
                            "[TURN_NUMBER] Constraint violation after %s retries for task %s: %s",
                            MAX_RETRIES,
                            task_id,
                            exc,
                            exc_info=True,
                        )
                        raise
                    logger.warning(
                        "[TURN_NUMBER] Retry %s/%s for task %s after constraint violation: %s",
                        attempt,
                        MAX_RETRIES,
                        task_id,
                        exc,
                    )
        except Exception as exc:
            logger.error(
                "[TURN_NUMBER] Failed to get next turn number for task %s: %s",
                task_id,
                exc,
                exc_info=True,
            )
            raise
        finally:
            db.close()

    def get_next_turn_number_in_session(
        self,
        db,
        *,
        task_id: int,
        conversation_id: Optional[str] = None,
    ) -> int:
        """Allocate the next turn number inside the caller's transaction."""
        try:
            next_turn = self._allocate_turn(db, task_id, commit=False)
            logger.info(
                "[TURN_NUMBER] Assigned turn %s to task %s (conversation=%s)",
                next_turn,
                task_id,
                conversation_id or "none",
            )
            return next_turn
        except Exception as exc:
            logger.error(
                "[TURN_NUMBER] Failed to get next turn number for task %s: %s",
                task_id,
                exc,
                exc_info=True,
            )
            raise

    def _allocate_turn(self, db, task_id: int, *, commit: bool = True) -> int:
        """Atomically allocate next turn number for task.

        Uses INSERT ... ON CONFLICT DO UPDATE ... RETURNING so one statement
        allocates and returns the new number. Serialized per task by the
        primary key on task_id. Uses raw SQL for PostgreSQL/SQLite portability.
        """
        # PostgreSQL / SQLite 3.24+: atomic upsert
        stmt = text(
            """
            INSERT INTO task_turn_counter (task_id, next_turn)
            VALUES (:task_id, 1)
            ON CONFLICT (task_id) DO UPDATE SET next_turn = task_turn_counter.next_turn + 1
            RETURNING next_turn
            """
        )
        result = db.execute(stmt, {"task_id": task_id})
        row = result.fetchone()
        if commit:
            db.commit()
        if not row:
            raise RuntimeError(f"task_turn_counter upsert returned no row for task_id={task_id}")
        return int(row[0])


# Singleton instance
_turn_number_service: Optional[TurnNumberService] = None


def get_turn_number_service() -> TurnNumberService:
    """Get singleton turn number service instance.

    Returns:
        Shared TurnNumberService instance.
    """
    global _turn_number_service
    if _turn_number_service is None:
        _turn_number_service = TurnNumberService()
    return _turn_number_service


def reset_turn_number_service() -> None:
    """Reset singleton instance (for testing)."""
    global _turn_number_service
    _turn_number_service = None


__all__ = [
    "TurnNumberService",
    "get_turn_number_service",
    "reset_turn_number_service",
]
