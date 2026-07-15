"""Resolve LangGraph turn identity from reserved ChatMessage rows.

This module isolates the best-effort cross-session lookup used by
start/resume/error flows when only a reserved assistant message id is known.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage

logger = logging.getLogger("backend.services.turn_identity_resolver")


def resolve_turn_identity_from_reserved_message(
    db: Session,
    *,
    task_id: int,
    reserved_message_id: Optional[int],
) -> tuple[Optional[str], Optional[int]]:
    """Resolve canonical turn_id and turn_sequence from reserved message id."""
    if reserved_message_id is None:
        return None, None
    message = db.get(ChatMessage, reserved_message_id)
    turn_sequence = getattr(message, "turn_number", None) if message else None
    if turn_sequence is None:
        turn_sequence = reserved_message_id
    return f"task-{task_id}-turn-{turn_sequence}", turn_sequence


def resolve_turn_identity_from_reserved_message_best_effort(
    *,
    task_id: int,
    reserved_message_id: Optional[int],
    session_factory: Optional[Callable[[], Session]] = None,
) -> tuple[Optional[str], Optional[int]]:
    """Best-effort lookup of turn identity from reserved assistant message id."""
    if reserved_message_id is None:
        return None, None
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db_session = session_factory()
    try:
        return resolve_turn_identity_from_reserved_message(
            db_session,
            task_id=task_id,
            reserved_message_id=reserved_message_id,
        )
    except Exception:
        logger.debug(
            "Failed to resolve turn identity from reserved message (task=%s message=%s)",
            task_id,
            reserved_message_id,
            exc_info=True,
        )
        return None, None
    finally:
        try:
            db_session.close()
        except Exception:
            pass
