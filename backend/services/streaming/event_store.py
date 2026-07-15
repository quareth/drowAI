"""
StreamEvent persistence service.

Stores normalized stream packets for replay across refreshes and reconnects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.streaming import StreamEvent

logger = logging.getLogger("backend.services.stream_event_store")


class StreamEventTaskMissingError(Exception):
    """Raised when stream event persistence references a missing task row."""


@dataclass(frozen=True)
class StreamBootstrapPage:
    """Bootstrap page payload for stream-authoritative chat startup."""

    rows: List[StreamEvent]
    next_after: int
    has_more: bool


class StreamEventStore:
    """CRUD helpers for StreamEvent rows."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def append_packet(self, task_id: int, packet: Dict[str, Any]) -> Optional[StreamEvent]:
        """Persist a normalized stream packet.

        Idempotent on (task_id, sequence). Returns the row if inserted.
        """
        sequence = packet.get("sequence")
        if not isinstance(sequence, int):
            logger.warning("StreamEvent append skipped (missing sequence) task_id=%s", task_id)
            return None

        conversation_id = packet.get("conversation_id")
        if isinstance(conversation_id, str) and not conversation_id.strip():
            conversation_id = None

        turn_id = packet.get("turn_id")
        if isinstance(turn_id, str) and not turn_id.strip():
            turn_id = None

        event_type: Optional[str] = None
        obj = packet.get("obj")
        if isinstance(obj, dict):
            event_type = obj.get("type")
            if isinstance(event_type, str) and not event_type.strip():
                event_type = None

        row = StreamEvent(
            task_id=task_id,
            tenant_id=self._resolve_task_tenant_id(task_id),
            sequence=sequence,
            event_type=event_type,
            conversation_id=conversation_id,
            turn_id=turn_id,
            payload=packet,
        )
        try:
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
            return row
        except IntegrityError as exc:
            self.db.rollback()
            error_message = str(exc.orig)
            if "stream_events_task_id_fkey" in error_message:
                raise StreamEventTaskMissingError(
                    f"Task {task_id} no longer exists for stream event persistence"
                ) from exc
            if (
                "ux_stream_events_task_sequence" in error_message
                or "stream_events.task_id, stream_events.sequence" in error_message
            ):
                # Idempotent duplicate write on (task_id, sequence).
                return None
            raise
        except Exception:
            self.db.rollback()
            logger.exception("Failed to persist stream event task_id=%s seq=%s", task_id, sequence)
            raise

    def list_after(
        self,
        task_id: int,
        after: int,
        limit: Optional[int] = 200,
        *,
        conversation_id: Optional[str] = None,
    ) -> List[StreamEvent]:
        stmt = select(StreamEvent).where(
            StreamEvent.task_id == task_id,
            StreamEvent.sequence > after,
        )
        if conversation_id:
            stmt = stmt.where(
                or_(
                    StreamEvent.conversation_id == conversation_id,
                    StreamEvent.conversation_id.is_(None),
                )
            )
        stmt = stmt.order_by(StreamEvent.sequence.asc())
        if isinstance(limit, int) and limit > 0:
            stmt = stmt.limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def list_before(
        self,
        task_id: int,
        before: int,
        limit: Optional[int] = 200,
        *,
        conversation_id: Optional[str] = None,
    ) -> List[StreamEvent]:
        stmt = select(StreamEvent).where(
            StreamEvent.task_id == task_id,
            StreamEvent.sequence < before,
        )
        if conversation_id:
            stmt = stmt.where(
                or_(
                    StreamEvent.conversation_id == conversation_id,
                    StreamEvent.conversation_id.is_(None),
                )
            )
        stmt = stmt.order_by(StreamEvent.sequence.desc())
        if isinstance(limit, int) and limit > 0:
            stmt = stmt.limit(limit)
        rows = list(self.db.execute(stmt).scalars().all())
        return list(reversed(rows))

    def get_latest_sequence(self, task_id: int) -> int:
        result = self.db.execute(
            select(func.max(StreamEvent.sequence)).where(StreamEvent.task_id == task_id)
        ).scalar()
        return int(result or 0)

    def has_events(self, task_id: int) -> bool:
        result = self.db.execute(
            select(func.count(StreamEvent.id)).where(StreamEvent.task_id == task_id)
        ).scalar()
        return int(result or 0) > 0

    def list_bootstrap_page(
        self,
        task_id: int,
        *,
        conversation_id: Optional[str] = None,
        limit: int = 200,
        after: int = 0,
    ) -> StreamBootstrapPage:
        """Return one startup page from stream events in ascending sequence order."""
        safe_limit = max(1, int(limit))
        rows = self.list_after(
            task_id=task_id,
            after=max(0, int(after)),
            limit=safe_limit + 1,
            conversation_id=conversation_id,
        )
        has_more = len(rows) > safe_limit
        if has_more:
            rows = rows[:safe_limit]
        next_after = rows[-1].sequence if rows else max(0, int(after))
        return StreamBootstrapPage(rows=rows, next_after=next_after, has_more=has_more)

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise StreamEventTaskMissingError(
                f"Cannot resolve tenant for stream event write without task ownership: task_id={task_id}"
            )
        return int(tenant_id)
