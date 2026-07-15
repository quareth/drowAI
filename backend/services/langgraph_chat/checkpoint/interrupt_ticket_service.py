"""Transactional persistence service for durable interrupt tickets."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.hitl import InterruptTicket, InterruptTicketState
from backend.core.time_utils import utc_now
from backend.services.langgraph_chat.streaming.status_events import (
    emit_interrupt_state_event,
)

logger = logging.getLogger("backend.services.langgraph_chat.interrupt_ticket_service")


def _utcnow() -> datetime:
    return utc_now()


def _normalize_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


class InterruptTicketError(RuntimeError):
    """Base class for interrupt ticket state errors."""


class InterruptTicketNotFoundError(InterruptTicketError):
    """Raised when a ticket cannot be found for the provided identity."""


class InterruptTicketClaimConflictError(InterruptTicketError):
    """Raised when a ticket cannot be claimed because it is no longer pending."""


class InterruptTicketService:
    """Narrow typed API for interrupt ticket lifecycle transitions."""

    _OBSERVED_PENDING_ALLOWED_FROM = {
        InterruptTicketState.PENDING,
    }

    def _recover_after_pending_write_conflict(
        self,
        *,
        interrupt_id: str,
        task_id: int,
    ) -> InterruptTicket:
        """Recover deterministically after pending-write races.

        Cases handled:
        - Concurrent create for same interrupt_id (return canonical row).
        - Task-level pending uniqueness conflict (return authoritative pending row).
        """
        row = (
            self.db.query(InterruptTicket)
            .filter(
                InterruptTicket.interrupt_id == interrupt_id,
                InterruptTicket.task_id == task_id,
            )
            .first()
        )
        if row is not None:
            return row

        pending = (
            self.db.query(InterruptTicket)
            .filter(
                InterruptTicket.task_id == task_id,
                InterruptTicket.state == InterruptTicketState.PENDING,
            )
            .order_by(InterruptTicket.updated_at.desc(), InterruptTicket.id.desc())
            .first()
        )
        if pending is not None:
            return pending

        raise InterruptTicketClaimConflictError(
            f"Pending write conflict for interrupt_id={interrupt_id!r}, task_id={task_id}"
        )

    def __init__(self, db: Session) -> None:
        self.db = db

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(
                f"Cannot resolve tenant for interrupt ticket write without task ownership: task_id={task_id}"
            )
        return int(tenant_id)

    @staticmethod
    def _is_transition_allowed(
        *,
        current_state: InterruptTicketState,
        target_state: InterruptTicketState,
        allowed_from: set[InterruptTicketState],
    ) -> bool:
        if current_state == target_state:
            return True
        return current_state in allowed_from

    @staticmethod
    def _emit_interrupt_state(row: InterruptTicket) -> None:
        state_value = (
            row.state.value
            if isinstance(row.state, InterruptTicketState)
            else str(row.state)
        )
        emit_interrupt_state_event(
            task_id=int(row.task_id),
            interrupt_id=row.interrupt_id,
            state=state_value,
            interrupt_type=row.interrupt_type,
            graph_name=row.graph_name,
            checkpoint_id=row.checkpoint_id,
            thread_id=row.thread_id,
            turn_id=row.turn_id,
            turn_sequence=row.turn_sequence,
            created_at=(row.created_at.isoformat() if row.created_at else None),
            updated_at=(row.updated_at.isoformat() if row.updated_at else None),
        )

    def create_or_update_pending(
        self,
        *,
        interrupt_id: str,
        task_id: int,
        graph_name: str,
        interrupt_type: str,
        checkpoint_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        tool_call_id: Optional[str] = None,
        payload_snapshot: Optional[Dict[str, Any]] = None,
    ) -> InterruptTicket:
        normalized_interrupt_id = _normalize_str(interrupt_id)
        normalized_graph_name = _normalize_str(graph_name)
        normalized_interrupt_type = _normalize_str(interrupt_type)
        if not normalized_interrupt_id:
            raise ValueError("interrupt_id is required")
        if task_id <= 0:
            raise ValueError("task_id must be positive")
        if not normalized_graph_name:
            raise ValueError("graph_name is required")
        if not normalized_interrupt_type:
            raise ValueError("interrupt_type is required")

        row = (
            self.db.query(InterruptTicket)
            .filter(InterruptTicket.interrupt_id == normalized_interrupt_id)
            .first()
        )
        if row is None:
            row = InterruptTicket(
                interrupt_id=normalized_interrupt_id,
                task_id=task_id,
                tenant_id=self._resolve_task_tenant_id(task_id),
                graph_name=normalized_graph_name,
                interrupt_type=normalized_interrupt_type,
                checkpoint_id=_normalize_str(checkpoint_id),
                thread_id=_normalize_str(thread_id),
                turn_id=_normalize_str(turn_id),
                turn_sequence=turn_sequence,
                tool_call_id=_normalize_str(tool_call_id),
                state=InterruptTicketState.PENDING,
                payload_snapshot=payload_snapshot or None,
            )
            self.db.add(row)
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                return self._recover_after_pending_write_conflict(
                    interrupt_id=normalized_interrupt_id,
                    task_id=task_id,
                )
            self.db.refresh(row)
            self._emit_interrupt_state(row)
            return row

        if row.task_id != task_id:
            raise ValueError("interrupt_id belongs to a different task")

        # Observed interrupt upsert must never regress lifecycle state.
        if not self._is_transition_allowed(
            current_state=row.state,
            target_state=InterruptTicketState.PENDING,
            allowed_from=self._OBSERVED_PENDING_ALLOWED_FROM,
        ):
            return row

        row.graph_name = normalized_graph_name
        row.interrupt_type = normalized_interrupt_type
        if checkpoint_id is not None:
            row.checkpoint_id = _normalize_str(checkpoint_id)
        if thread_id is not None:
            row.thread_id = _normalize_str(thread_id)
        if turn_id is not None:
            row.turn_id = _normalize_str(turn_id)
        if turn_sequence is not None:
            row.turn_sequence = turn_sequence
        if tool_call_id is not None:
            row.tool_call_id = _normalize_str(tool_call_id)
        if payload_snapshot is not None:
            row.payload_snapshot = payload_snapshot
        row.state = InterruptTicketState.PENDING
        row.updated_at = _utcnow()
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return self._recover_after_pending_write_conflict(
                interrupt_id=normalized_interrupt_id,
                task_id=task_id,
            )
        self.db.refresh(row)
        self._emit_interrupt_state(row)
        return row

    def claim_for_resume(
        self,
        *,
        interrupt_id: str,
        task_id: int,
        user_id: Optional[int] = None,  # Reserved for future access checks.
    ) -> InterruptTicket:
        normalized_interrupt_id = _normalize_str(interrupt_id)
        if not normalized_interrupt_id:
            raise ValueError("interrupt_id is required")
        if task_id <= 0:
            raise ValueError("task_id must be positive")

        now = _utcnow()
        updated_count = (
            self.db.query(InterruptTicket)
            .filter(
                InterruptTicket.interrupt_id == normalized_interrupt_id,
                InterruptTicket.task_id == task_id,
                InterruptTicket.state == InterruptTicketState.PENDING,
            )
            .update(
                {
                    InterruptTicket.state: InterruptTicketState.RESUMING,
                    InterruptTicket.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated_count != 1:
            self.db.rollback()
            existing = (
                self.db.query(InterruptTicket)
                .filter(
                    InterruptTicket.interrupt_id == normalized_interrupt_id,
                    InterruptTicket.task_id == task_id,
                )
                .first()
            )
            if existing is None:
                raise InterruptTicketNotFoundError(
                    f"Interrupt ticket not found for interrupt_id={normalized_interrupt_id!r}"
                )
            raise InterruptTicketClaimConflictError(
                f"Interrupt ticket {normalized_interrupt_id!r} is in state {existing.state.value}"
            )

        claimed = (
            self.db.query(InterruptTicket)
            .filter(
                InterruptTicket.interrupt_id == normalized_interrupt_id,
                InterruptTicket.task_id == task_id,
            )
            .first()
        )
        if claimed is None:
            self.db.rollback()
            raise InterruptTicketNotFoundError(
                f"Interrupt ticket not found after claim for interrupt_id={normalized_interrupt_id!r}"
            )
        self.db.commit()
        self.db.refresh(claimed)
        self._emit_interrupt_state(claimed)
        return claimed

    def mark_resumed(self, *, interrupt_id: str, task_id: int) -> InterruptTicket:
        return self._transition_state(
            interrupt_id=interrupt_id,
            task_id=task_id,
            allowed_from={InterruptTicketState.RESUMING, InterruptTicketState.RESUMED},
            target_state=InterruptTicketState.RESUMED,
        )

    def mark_pending(self, *, interrupt_id: str, task_id: int) -> InterruptTicket:
        return self._transition_state(
            interrupt_id=interrupt_id,
            task_id=task_id,
            allowed_from={
                InterruptTicketState.PENDING,
                InterruptTicketState.RESUMING,
            },
            target_state=InterruptTicketState.PENDING,
        )

    def mark_completed(self, *, interrupt_id: str, task_id: int) -> InterruptTicket:
        return self._transition_state(
            interrupt_id=interrupt_id,
            task_id=task_id,
            allowed_from={
                InterruptTicketState.RESUMED,
                InterruptTicketState.RESUMING,
                InterruptTicketState.COMPLETED,
            },
            target_state=InterruptTicketState.COMPLETED,
        )

    def mark_failed(self, *, interrupt_id: str, task_id: int) -> InterruptTicket:
        return self._transition_state(
            interrupt_id=interrupt_id,
            task_id=task_id,
            allowed_from={
                InterruptTicketState.PENDING,
                InterruptTicketState.RESUMING,
                InterruptTicketState.RESUMED,
                InterruptTicketState.FAILED,
            },
            target_state=InterruptTicketState.FAILED,
        )

    def expire_stale(
        self, *, stale_before: datetime, task_id: Optional[int] = None
    ) -> int:
        query = self.db.query(InterruptTicket).filter(
            InterruptTicket.updated_at < stale_before,
            InterruptTicket.state.in_(
                [
                    InterruptTicketState.PENDING,
                    InterruptTicketState.RESUMING,
                ]
            ),
        )
        if task_id is not None:
            query = query.filter(InterruptTicket.task_id == task_id)
        updated_count = query.update(
            {
                InterruptTicket.state: InterruptTicketState.EXPIRED,
                InterruptTicket.updated_at: _utcnow(),
            },
            synchronize_session=False,
        )
        self.db.commit()
        return int(updated_count or 0)

    def _transition_state(
        self,
        *,
        interrupt_id: str,
        task_id: int,
        allowed_from: set[InterruptTicketState],
        target_state: InterruptTicketState,
    ) -> InterruptTicket:
        normalized_interrupt_id = _normalize_str(interrupt_id)
        if not normalized_interrupt_id:
            raise ValueError("interrupt_id is required")

        row = (
            self.db.query(InterruptTicket)
            .filter(
                InterruptTicket.interrupt_id == normalized_interrupt_id,
                InterruptTicket.task_id == task_id,
            )
            .first()
        )
        if row is None:
            raise InterruptTicketNotFoundError(
                f"Interrupt ticket not found for interrupt_id={normalized_interrupt_id!r}"
            )

        if not self._is_transition_allowed(
            current_state=row.state,
            target_state=target_state,
            allowed_from=allowed_from,
        ):
            raise InterruptTicketClaimConflictError(
                f"Invalid transition from {row.state.value} to {target_state.value}"
            )
        if row.state == target_state:
            return row

        row.state = target_state
        row.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(row)
        self._emit_interrupt_state(row)
        return row


def mark_interrupt_ticket_resumed_best_effort(
    *,
    task_id: int,
    interrupt_id: Optional[str],
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """Best-effort transition for a claimed interrupt ticket."""
    if not isinstance(interrupt_id, str) or not interrupt_id.strip():
        return
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        ticket_service = InterruptTicketService(db)
        ticket_service.mark_resumed(interrupt_id=interrupt_id, task_id=task_id)
    except (InterruptTicketClaimConflictError, InterruptTicketNotFoundError):
        logger.debug(
            "Interrupt ticket resume transition skipped (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
    except Exception:
        logger.warning(
            "Failed to mark interrupt ticket resumed (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_interrupt_ticket_completed_best_effort(
    *,
    task_id: int,
    interrupt_id: Optional[str],
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """Best-effort transition for completed interrupt tickets."""
    if not isinstance(interrupt_id, str) or not interrupt_id.strip():
        return
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        ticket_service = InterruptTicketService(db)
        ticket_service.mark_completed(interrupt_id=interrupt_id, task_id=task_id)
    except (InterruptTicketClaimConflictError, InterruptTicketNotFoundError):
        logger.debug(
            "Interrupt ticket completion transition skipped (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
    except Exception:
        logger.warning(
            "Failed to mark interrupt ticket completed (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_interrupt_ticket_failed_best_effort(
    *,
    task_id: int,
    interrupt_id: Optional[str],
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """Best-effort transition for failed interrupt tickets."""
    if not isinstance(interrupt_id, str) or not interrupt_id.strip():
        return
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        ticket_service = InterruptTicketService(db)
        ticket_service.mark_failed(interrupt_id=interrupt_id, task_id=task_id)
    except (InterruptTicketClaimConflictError, InterruptTicketNotFoundError):
        logger.debug(
            "Interrupt ticket failure transition skipped (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
    except Exception:
        logger.warning(
            "Failed to mark interrupt ticket failed (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def resolve_interrupt_tool_call_id_best_effort(
    *,
    task_id: int,
    interrupt_id: Optional[str],
    session_factory: Optional[Callable[[], Session]] = None,
) -> Optional[str]:
    """Best-effort lookup of interrupt tool_call_id for HITL correlation."""
    if not isinstance(interrupt_id, str) or not interrupt_id.strip():
        return None
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        row = (
            db.query(InterruptTicket)
            .filter(
                InterruptTicket.task_id == task_id,
                InterruptTicket.interrupt_id == interrupt_id.strip(),
            )
            .first()
        )
        if row is None:
            return None
        tool_call_id = getattr(row, "tool_call_id", None)
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            return tool_call_id.strip()
        return None
    except Exception:
        logger.debug(
            "Failed to resolve interrupt tool_call_id (task=%s, interrupt_id=%s)",
            task_id,
            interrupt_id,
            exc_info=True,
        )
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


__all__ = [
    "InterruptTicketService",
    "InterruptTicketError",
    "InterruptTicketNotFoundError",
    "InterruptTicketClaimConflictError",
    "mark_interrupt_ticket_resumed_best_effort",
    "mark_interrupt_ticket_completed_best_effort",
    "mark_interrupt_ticket_failed_best_effort",
    "resolve_interrupt_tool_call_id_best_effort",
]
