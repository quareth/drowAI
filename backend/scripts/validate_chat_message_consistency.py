""": Data consistency validation (ChatMessage-only).

Checks:
1. Incomplete assistant messages older than a safety threshold (reserved but never updated)
2. Tool call integrity: assistant ChatMessages and their tool_calls

Run from repo root with DATABASE_URL set:
 python backend/scripts/validate_chat_message_consistency.py

Exit code: 0 if consistent, 1 if issues found or DB unavailable."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

# Repo root on path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def get_session():
    """Return DB session or None if DATABASE_URL not set."""
    if not os.getenv("DATABASE_URL"):
        return None
    from backend.database import SessionLocal
    return SessionLocal()


def check_incomplete_assistant_messages(
    db,
    *,
    min_age_minutes: int = 10,
    limit: int = 50,
) -> List[Tuple[int, int, str]]:
    """Find assistant messages that were reserved but never updated.

    Returns list of (task_id, message_id, created_at_iso) for assistant
    messages with empty content and no error, older than min_age_minutes.
    """
    from backend.models.chat import ChatMessage

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.message_type == "ASSISTANT",
            ChatMessage.created_at < cutoff,
            (ChatMessage.message.is_(None) | (ChatMessage.message == "")),
            ChatMessage.error.is_(None),
        )
        .order_by(ChatMessage.created_at.asc())
        .limit(limit)
        .all()
    )
    return [
        (row.task_id, row.id, (row.created_at or cutoff).isoformat())
        for row in rows
    ]


def check_tool_call_integrity(db, limit: int = 100) -> List[dict]:
    """
    For assistant ChatMessages, report id, message snippet, tool_call count.
    Returns list of dicts for inspection (no strict consistency rule).
    """
    from sqlalchemy import func
    from backend.models.chat import ChatMessage, ToolCall

    # Assistant messages with tool call count
    stmt = (
        db.query(
            ChatMessage.id,
            ChatMessage.message,
            func.count(ToolCall.id).label("tool_call_count"),
        )
        .outerjoin(ToolCall, ToolCall.chat_message_id == ChatMessage.id)
        .filter(ChatMessage.message_type == "ASSISTANT")
        .group_by(ChatMessage.id, ChatMessage.message)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "id": r.id,
            "message_preview": (r.message or "")[:80],
            "tool_call_count": r.tool_call_count,
        }
        for r in rows
    ]


def main() -> int:
    db = get_session()
    if not db:
        print("SKIP: DATABASE_URL not set.")
        return 0
    try:
        # 1. Incomplete assistant messages
        incomplete = check_incomplete_assistant_messages(db)
        if incomplete:
            print("Incomplete assistant messages (reserved but never updated):")
            for task_id, msg_id, created_at in incomplete:
                print(f"  task_id={task_id}  message_id={msg_id}  created_at={created_at}")
        else:
            print("No incomplete assistant messages found.")

        # 2. Tool call integrity (sample)
        samples = check_tool_call_integrity(db, limit=20)
        print("\nTool call integrity (sample assistant messages):")
        for s in samples[:10]:
            preview = (s["message_preview"] or "")[:40]
            if len((s["message_preview"] or "")) > 40:
                preview += "..."
            print(f"  chat_message_id={s['id']}  tool_calls={s['tool_call_count']}  msg={preview}")
        if len(samples) > 10:
            print(f"  ... and {len(samples) - 10} more")

        if incomplete:
            return 1
        print("\nConsistency check completed. No incomplete assistant messages found.")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
