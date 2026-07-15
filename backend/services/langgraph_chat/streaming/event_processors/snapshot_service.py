"""Persist immediate tool-call snapshots created during ``tool_end`` handling.

Responsibilities:
- own the short-lived database session lifecycle for snapshot writes
- persist best-effort tool call rows through ``ToolCallRepository``
- isolate database error handling from the hot-path tool event processor

This module does not decide *when* a snapshot should be written. That gating
logic stays in the tool event processor so the persistence policy remains close
to tool lifecycle handling.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.database import SessionLocal
from backend.services.chat.tool_call_repository import ToolCallRepository

logger = logging.getLogger("backend.services.langgraph_chat.streaming_adapter")


class ToolCallSnapshotService:
    """Best-effort persistence for tool_end snapshots."""

    def persist_snapshot(
        self,
        *,
        reserved_message_id: int,
        tool_call_info: dict[str, Any],
    ) -> None:
        """Persist a single tool call row immediately on tool_end."""
        if not tool_call_info:
            return

        db = SessionLocal()
        try:
            ToolCallRepository(db).create_tool_calls(reserved_message_id, [tool_call_info])
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning(
                "[STREAM_ADAPTER] Failed to persist tool_call snapshot "
                "(message_id=%s tool_call_id=%s): %s",
                reserved_message_id,
                tool_call_info.get("tool_call_id"),
                exc,
                exc_info=True,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass


__all__ = ["ToolCallSnapshotService"]
